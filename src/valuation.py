"""
研报 2.2：生命周期收益分解。

核心公式沿用研报：

ln(1+r) = Δln(PB) + Δln(B) + ln(1 + D/P) - Δln(S)

其中 PB、B、S 先在行业整体层面构造，再做对数差分；D/P 使用除权日落在
当月的实际现金分红总额 / 行业当月总市值。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

os.environ.setdefault("MPLCONFIGDIR", str(Path("/tmp") / "matplotlib-cache"))
os.environ.setdefault("XDG_CACHE_HOME", str(Path("/tmp") / "matplotlib-cache"))

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import END_DATE, OUTPUT_DIR, PROCESSED_DATA_DIR, RAW_DATA_DIR, START_DATE
from src.factors import weighted_average
from src.lifecycle import (
    LifecycleConfig,
    attach_industry_by_month,
    parse_wind_date,
    prepare_industry_map,
    read_parquet_dataset,
)


LOGGER = logging.getLogger(__name__)

INDUSTRY_DECOMP_OUTPUT = PROCESSED_DATA_DIR / "industry_return_decomposition.parquet"
SUMMARY_OUTPUT = OUTPUT_DIR / "tables" / "lifecycle_return_decomposition.csv"
YEARLY_OUTPUT = OUTPUT_DIR / "tables" / "lifecycle_return_decomposition_by_year.csv"

STAGE_ORDER = ["成长期", "成熟期", "停滞期", "衰退期", "转型期"]
STAGE_TO_FILE = {
    "成长期": "return_decomposition_growth.png",
    "成熟期": "return_decomposition_mature.png",
    "停滞期": "return_decomposition_stagnation.png",
    "衰退期": "return_decomposition_decline.png",
    "转型期": "return_decomposition_pivoting.png",
}
CONTRIBUTION_COLS = [
    "valuation_contribution",
    "earnings_contribution",
    "dividend_contribution",
    "share_change_contribution",
]
CONTRIBUTION_LABELS = {
    "valuation_contribution": "估值",
    "earnings_contribution": "盈利",
    "dividend_contribution": "分红",
    "share_change_contribution": "股本变动",
}
CONTRIBUTION_COLORS = {
    "valuation_contribution": "#219EBC",
    "earnings_contribution": "#023047",
    "dividend_contribution": "#FFB703",
    "share_change_contribution": "#FB8500",
}
LINE_COLORS = {
    **CONTRIBUTION_COLORS,
    "total_log_return": "#D62828",
}
LINE_LABELS = {
    **CONTRIBUTION_LABELS,
    "total_log_return": "总收益",
}


@dataclass(frozen=True)
class ValuationConfig:
    """收益分解参数。"""

    start_date: str = START_DATE
    decomposition_start_date: str = "2007-03-01"
    end_date: str = END_DATE
    min_stocks: int = 3
    industry_scheme: str = "hybrid"
    industry_level: int = 4
    min_tertiary_stocks: int = 8
    target_industries: Optional[int] = None


def safe_log_ratio(current: pd.Series | float, previous: pd.Series | float) -> pd.Series | float:
    """仅在当期和上期均为正时计算 ln(current / previous)。"""

    current_s = pd.to_numeric(pd.Series(current), errors="coerce")
    previous_s = pd.to_numeric(pd.Series(previous), errors="coerce")
    result = pd.Series(np.nan, index=current_s.index, dtype="float64")
    mask = (current_s > 0) & (previous_s > 0)
    result.loc[mask] = np.log(current_s.loc[mask] / previous_s.loc[mask])
    if np.isscalar(current) and np.isscalar(previous):
        return float(result.iloc[0])
    return result


def monthly_dividend_contribution(dividend_yield_percent: pd.Series | float) -> pd.Series | float:
    """把年化股息率近似换成月度对数分红贡献。"""

    dividend_yield = pd.to_numeric(pd.Series(dividend_yield_percent), errors="coerce")
    monthly_yield = (dividend_yield / 100 / 12).where(dividend_yield >= 0)
    result = np.log1p(monthly_yield)
    if np.isscalar(dividend_yield_percent):
        return float(result.iloc[0])
    return result


def actual_dividend_contribution(
    cash_dividend: pd.Series | float,
    market_value: pd.Series | float,
) -> pd.Series | float:
    """用实际现金分红和市值计算 ln(1 + D/P)。"""

    dividend_s = pd.to_numeric(pd.Series(cash_dividend), errors="coerce")
    market_value_s = pd.to_numeric(pd.Series(market_value), errors="coerce")
    yield_s = dividend_s.where(dividend_s >= 0) / market_value_s.where(market_value_s > 0)
    result = np.log1p(yield_s)
    if np.isscalar(cash_dividend) and np.isscalar(market_value):
        return float(result.iloc[0])
    return result


def read_stock_price_monthly(config: ValuationConfig) -> pd.DataFrame:
    """读取复权收盘价并压缩为个股月末价格。"""

    df = read_parquet_dataset(
        "stock_eod_prices",
        columns=["S_INFO_WINDCODE", "TRADE_DT", "S_DQ_ADJCLOSE"],
    )
    df["windcode"] = df["S_INFO_WINDCODE"]
    df["trade_date"] = parse_wind_date(df["TRADE_DT"])
    df["adj_close"] = pd.to_numeric(df["S_DQ_ADJCLOSE"], errors="coerce")
    df = df[
        df["windcode"].notna()
        & df["trade_date"].notna()
        & (df["trade_date"] >= pd.Timestamp(config.start_date))
        & (df["trade_date"] <= pd.Timestamp(config.end_date))
    ].copy()
    df["month_end"] = df["trade_date"].dt.to_period("M").dt.to_timestamp("M")
    df = df.sort_values(["windcode", "month_end", "trade_date"])
    df = df.drop_duplicates(["windcode", "month_end"], keep="last")
    df["prev_adj_close"] = df.groupby("windcode")["adj_close"].shift(1)
    df["total_log_return"] = safe_log_ratio(df["adj_close"], df["prev_adj_close"])
    return df[["windcode", "month_end", "trade_date", "adj_close", "total_log_return"]].reset_index(drop=True)


def read_stock_valuation_monthly(config: ValuationConfig) -> pd.DataFrame:
    """读取日频估值衍生指标并压缩为个股月末值。"""

    df = read_parquet_dataset(
        "stock_eod_derivative",
        columns=[
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_MV",
            "S_VAL_MV",
            "S_VAL_PB_NEW",
            "TOT_SHR_TODAY",
            "NET_ASSETS_TODAY",
        ],
    )
    df["windcode"] = df["S_INFO_WINDCODE"]
    df["trade_date"] = parse_wind_date(df["TRADE_DT"])
    df = df[
        df["windcode"].notna()
        & df["trade_date"].notna()
        & (df["trade_date"] >= pd.Timestamp(config.start_date))
        & (df["trade_date"] <= pd.Timestamp(config.end_date))
    ].copy()
    df["month_end"] = df["trade_date"].dt.to_period("M").dt.to_timestamp("M")
    df = df.sort_values(["windcode", "month_end", "trade_date"])
    df = df.drop_duplicates(["windcode", "month_end"], keep="last")

    df["float_mv"] = pd.to_numeric(df["S_DQ_MV"], errors="coerce")
    df["total_mv"] = pd.to_numeric(df["S_VAL_MV"], errors="coerce")
    df["pb"] = pd.to_numeric(df["S_VAL_PB_NEW"], errors="coerce")
    df["book_value"] = pd.to_numeric(df["NET_ASSETS_TODAY"], errors="coerce")
    df["shares"] = pd.to_numeric(df["TOT_SHR_TODAY"], errors="coerce")

    return df[
        [
            "windcode",
            "month_end",
            "trade_date",
            "float_mv",
            "total_mv",
            "pb",
            "book_value",
            "shares",
        ]
    ].reset_index(drop=True)


def read_actual_dividend_monthly(config: ValuationConfig) -> pd.DataFrame:
    """按除权日汇总个股月内实际现金分红。

    AShareDividend 的早年分区中 `TOT_SHR` 字段全空，整表读取会触发 Arrow
    schema 合并错误。这里逐文件只读取必要字段，并在 `TOT_CASH_DVD` 缺失时
    用 `CASH_DVD_PER_SH_PRE_TAX * S_DIV_BASESHARE * 10000` 回补。
    """

    path = RAW_DATA_DIR / "dividend"
    if not path.exists():
        LOGGER.warning("缺少分红原始数据目录：%s，分红贡献将为空。", path)
        return pd.DataFrame(columns=["windcode", "month_end", "cash_dividend"])

    columns = [
        "S_INFO_WINDCODE",
        "WIND_CODE",
        "S_DIV_PROGRESS",
        "CASH_DVD_PER_SH_PRE_TAX",
        "EX_DT",
        "REPORT_PERIOD",
        "S_DIV_BASESHARE",
        "TOT_CASH_DVD",
    ]
    frames = []
    for file in sorted(path.rglob("*.parquet")):
        frames.append(pd.read_parquet(file, columns=columns))
    if not frames:
        return pd.DataFrame(columns=["windcode", "month_end", "cash_dividend"])

    df = pd.concat(frames, ignore_index=True)
    df["windcode"] = df["S_INFO_WINDCODE"].fillna(df["WIND_CODE"])
    df["ex_date"] = parse_wind_date(df["EX_DT"])
    df["report_period"] = parse_wind_date(df["REPORT_PERIOD"])
    df["cash_per_share"] = pd.to_numeric(df["CASH_DVD_PER_SH_PRE_TAX"], errors="coerce")
    df["base_share"] = pd.to_numeric(df["S_DIV_BASESHARE"], errors="coerce")
    df["cash_dividend"] = pd.to_numeric(df["TOT_CASH_DVD"], errors="coerce")
    fallback_cash = df["cash_per_share"] * df["base_share"] * 10000
    df["cash_dividend"] = df["cash_dividend"].fillna(fallback_cash)
    df = df[
        df["windcode"].notna()
        & df["ex_date"].notna()
        & (df["ex_date"] >= pd.Timestamp(config.start_date))
        & (df["ex_date"] <= pd.Timestamp(config.end_date))
        & (df["cash_dividend"] > 0)
    ].copy()
    # 3 通常代表已实施。保留缺失状态作为回退，避免数据库状态字段不完整时误删。
    progress = df["S_DIV_PROGRESS"].astype("string")
    df = df[progress.isna() | progress.eq("3")].copy()
    df = df.drop_duplicates(
        [
            "windcode",
            "ex_date",
            "report_period",
            "cash_per_share",
            "cash_dividend",
        ]
    )
    df["month_end"] = df["ex_date"].dt.to_period("M").dt.to_timestamp("M")
    return (
        df.groupby(["windcode", "month_end"], as_index=False)["cash_dividend"]
        .sum()
        .sort_values(["windcode", "month_end"])
        .reset_index(drop=True)
    )


def build_stock_return_decomposition(config: ValuationConfig) -> pd.DataFrame:
    """生成个股月度基础面板。

    这里只保留构造行业资产所需的个股月末基础字段，不在个股层面做收益拆解。
    研报公式应作用在行业整体资产上，而不是先拆个股再加权平均。
    """

    price = read_stock_price_monthly(config)
    valuation = read_stock_valuation_monthly(config)
    dividend = read_actual_dividend_monthly(config)
    panel = valuation.merge(
        price[["windcode", "month_end", "adj_close", "total_log_return"]],
        on=["windcode", "month_end"],
        how="inner",
    )
    panel = panel.merge(dividend, on=["windcode", "month_end"], how="left")
    panel["cash_dividend"] = panel["cash_dividend"].fillna(0.0)
    panel = panel.sort_values(["windcode", "month_end"]).reset_index(drop=True)

    for col in ["float_mv", "total_mv"]:
        panel[f"prev_{col}"] = panel.groupby("windcode")[col].shift(1)

    panel["weight"] = panel["prev_float_mv"].fillna(panel["prev_total_mv"])
    return panel.reset_index(drop=True)


def aggregate_to_industry(stock_panel: pd.DataFrame, config: ValuationConfig) -> pd.DataFrame:
    """先构造行业整体资产，再在行业层面做收益拆解。"""

    lifecycle_config = LifecycleConfig(
        industry_scheme=config.industry_scheme,
        industry_level=config.industry_level,
        min_stocks=config.min_stocks,
        min_tertiary_stocks=config.min_tertiary_stocks,
        target_industries=config.target_industries,
        start_date=config.start_date,
        end_date=config.end_date,
    )
    membership, industry_universe, _ = prepare_industry_map(lifecycle_config)
    stock_industry = attach_industry_by_month(stock_panel, membership, industry_universe)

    rows = []
    group_cols = ["month_end", "industry_old_code", "industry_code", "industry_name", "industry_source_level"]
    for keys, group in stock_industry.groupby(group_cols, dropna=False):
        weights = group["weight"]
        total_mv = group["total_mv"].where(group["total_mv"] > 0).sum(min_count=1)
        float_mv = group["float_mv"].where(group["float_mv"] > 0).sum(min_count=1)
        book_value = group["book_value"].where(group["book_value"] > 0).sum(min_count=1)
        shares = group["shares"].where(group["shares"] > 0).sum(min_count=1)
        row = {
            **dict(zip(group_cols, keys)),
            "stock_count": group["windcode"].nunique(),
            "weight_sum": weights[weights > 0].sum(),
            "total_mv": total_mv,
            "float_mv": float_mv,
            "book_value": book_value,
            "shares": shares,
            "cash_dividend": group["cash_dividend"].sum(),
            # S_VAL_MV 单位通常为万元，NET_ASSETS_TODAY 通常为元；乘 10000 后与
            # 个股 PB 字段数量级一致。对数差分对常数倍不敏感，但绝对 PB 便于检查。
            "pb": total_mv * 10000 / book_value if pd.notna(total_mv) and pd.notna(book_value) and book_value > 0 else np.nan,
            "stock_weighted_log_return": weighted_average(group["total_log_return"], weights),
            "stock_weighted_log_return_coverage": group["total_log_return"].notna().mean(),
            "dividend_stock_count": int((group["cash_dividend"] > 0).sum()),
        }
        rows.append(row)

    industry = pd.DataFrame(rows)
    industry = industry[industry["stock_count"] >= config.min_stocks].copy()
    industry = industry.sort_values(["industry_old_code", "month_end"]).reset_index(drop=True)
    for col in ["pb", "book_value", "shares"]:
        industry[f"prev_{col}"] = industry.groupby("industry_old_code")[col].shift(1)

    industry["valuation_contribution"] = safe_log_ratio(industry["pb"], industry["prev_pb"])
    industry["earnings_contribution"] = safe_log_ratio(industry["book_value"], industry["prev_book_value"])
    industry["market_value_yuan"] = industry["total_mv"] * 10000
    industry["dividend_yield"] = industry["cash_dividend"] / industry["market_value_yuan"].where(
        industry["market_value_yuan"] > 0
    )
    industry["dividend_contribution"] = actual_dividend_contribution(
        industry["cash_dividend"],
        industry["market_value_yuan"],
    )
    industry["share_change_contribution"] = -safe_log_ratio(industry["shares"], industry["prev_shares"])
    industry["decomposition_sum"] = industry[CONTRIBUTION_COLS].sum(axis=1, min_count=len(CONTRIBUTION_COLS))
    # 研报公式中的 ln(1+r) 就是四项贡献之和。成分股复权收益只作为校验列保留，
    # 不用于图 13-15 的总收益线，否则会混入分红近似和成分股重构造成的口径差。
    industry["total_log_return"] = industry["decomposition_sum"]
    industry["stock_weighted_return_gap"] = industry["stock_weighted_log_return"] - industry["total_log_return"]

    start = pd.Timestamp(config.decomposition_start_date).to_period("M").to_timestamp("M")
    industry = industry[industry["month_end"] >= start].copy()
    return industry.sort_values(["month_end", "industry_old_code"]).reset_index(drop=True)


def merge_lifecycle(industry_panel: pd.DataFrame) -> pd.DataFrame:
    """合并行业生命周期标签。"""

    lifecycle_path = PROCESSED_DATA_DIR / "industry_lifecycle_monthly.parquet"
    if not lifecycle_path.exists():
        raise FileNotFoundError(f"缺少生命周期面板，请先运行 python -m src.lifecycle：{lifecycle_path}")
    lifecycle = pd.read_parquet(
        lifecycle_path,
        columns=["month_end", "industry_old_code", "lifecycle_stage", "lifecycle_stage_zh", "cashflow_pattern"],
    )
    return industry_panel.merge(lifecycle, on=["month_end", "industry_old_code"], how="left")


def summarize_by_lifecycle(panel: pd.DataFrame) -> pd.DataFrame:
    """图 13：按生命周期统计月度贡献的年化均值。"""

    panel = panel[panel["lifecycle_stage_zh"].isin(STAGE_ORDER)].copy()
    rows = []
    for stage in STAGE_ORDER:
        group = panel[panel["lifecycle_stage_zh"].eq(stage)]
        row = {
            "lifecycle_stage_zh": stage,
            "sample_count": int(len(group)),
            "industry_count": int(group["industry_old_code"].nunique()),
            "month_count": int(group["month_end"].nunique()),
        }
        for col in CONTRIBUTION_COLS:
            row[col] = group[col].mean(skipna=True) * 12
        row["decomposition_sum"] = group["decomposition_sum"].mean(skipna=True) * 12
        row["total_log_return"] = group["total_log_return"].mean(skipna=True) * 12
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_by_year(panel: pd.DataFrame) -> pd.DataFrame:
    """图 14/15 扩展版：每个生命周期的年度收益拆解。"""

    panel = panel[panel["lifecycle_stage_zh"].isin(STAGE_ORDER)].copy()
    panel["year"] = panel["month_end"].dt.year

    monthly_rows = []
    for (month_end, stage), group in panel.groupby(["month_end", "lifecycle_stage_zh"]):
        row = {
            "month_end": month_end,
            "year": int(month_end.year),
            "lifecycle_stage_zh": stage,
            "sample_count": int(len(group)),
            "industry_count": int(group["industry_old_code"].nunique()),
        }
        for col in CONTRIBUTION_COLS + ["decomposition_sum", "total_log_return"]:
            row[col] = group[col].mean(skipna=True)
        monthly_rows.append(row)
    monthly_stage = pd.DataFrame(monthly_rows)

    rows = []
    for (year, stage), group in monthly_stage.groupby(["year", "lifecycle_stage_zh"]):
        row = {
            "year": int(year),
            "lifecycle_stage_zh": stage,
            "sample_count": int(group["sample_count"].sum()),
            "month_count": int(group["month_end"].nunique()),
            "avg_industry_count": group["industry_count"].mean(skipna=True),
        }
        for col in CONTRIBUTION_COLS + ["decomposition_sum", "total_log_return"]:
            row[col] = group[col].sum(skipna=True)
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary["lifecycle_stage_zh"] = pd.Categorical(summary["lifecycle_stage_zh"], STAGE_ORDER, ordered=True)
    return summary.sort_values(["lifecycle_stage_zh", "year"]).reset_index(drop=True)


def _set_plot_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _plot_stacked_bars(ax, data: pd.DataFrame, x, width: float = 0.62) -> None:
    """画正负分离的堆叠柱。"""

    positive_base = np.zeros(len(data))
    negative_base = np.zeros(len(data))
    for col in CONTRIBUTION_COLS:
        values = data[col].fillna(0).to_numpy(dtype=float)
        positive = np.where(values > 0, values, 0)
        negative = np.where(values < 0, values, 0)
        ax.bar(
            x,
            positive,
            width=width,
            bottom=positive_base,
            color=CONTRIBUTION_COLORS[col],
            label=CONTRIBUTION_LABELS[col],
        )
        ax.bar(
            x,
            negative,
            width=width,
            bottom=negative_base,
            color=CONTRIBUTION_COLORS[col],
        )
        positive_base += positive
        negative_base += negative


def plot_lifecycle_decomposition(summary: pd.DataFrame) -> None:
    """输出图 13：生命周期长期平均收益拆解。"""

    import matplotlib.pyplot as plt

    _set_plot_style()
    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    data = summary.set_index("lifecycle_stage_zh").reindex(STAGE_ORDER).reset_index()
    x = np.arange(len(data))
    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    _plot_stacked_bars(ax, data, x)
    ax.plot(x, data["total_log_return"], color="#D62828", marker="o", linewidth=1.8, label="总收益")
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title("图13：不同生命周期的长期平均收益拆解")
    ax.set_ylabel("年化对数收益贡献")
    ax.set_xlabel("生命周期阶段")
    ax.set_xticks(x)
    ax.set_xticklabels(data["lifecycle_stage_zh"])
    ax.legend(loc="upper left", frameon=False, ncol=3)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig13_lifecycle_return_decomposition.png", dpi=200)
    plt.close(fig)


def plot_yearly_decomposition(yearly: pd.DataFrame) -> None:
    """输出所有生命周期的年度收益拆解图。

    研报图 14/15 使用曲线展示不同收益来源随时间变化。这里将该口径扩展到
    五个生命周期：每张图包含估值、盈利、分红、股本变动和总收益五条线。
    """

    import matplotlib.pyplot as plt

    _set_plot_style()
    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    for stage in STAGE_ORDER:
        data = yearly[yearly["lifecycle_stage_zh"].eq(stage)].sort_values("year").copy()
        if data.empty:
            continue
        fig, ax = plt.subplots(figsize=(10.2, 4.8))
        for col in CONTRIBUTION_COLS + ["total_log_return"]:
            ax.plot(
                data["year"],
                data[col],
                color=LINE_COLORS[col],
                marker="o",
                markersize=3.6,
                linewidth=1.7 if col != "total_log_return" else 2.2,
                label=LINE_LABELS[col],
            )
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_title(f"{stage}年度收益拆解")
        ax.set_ylabel("年度对数收益贡献")
        ax.set_xlabel("年份")
        ax.set_xticks(data["year"])
        ax.set_xticklabels(data["year"].astype(str), rotation=45, ha="right")
        ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
        ax.legend(loc="upper left", frameon=False, ncol=5)
        fig.tight_layout()
        fig.savefig(figure_dir / STAGE_TO_FILE[stage], dpi=200)
        plt.close(fig)


def run_valuation(config: ValuationConfig, overwrite: bool = False) -> pd.DataFrame:
    """执行收益分解、表格和图形输出。"""

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("figures").mkdir(parents=True, exist_ok=True)

    if INDUSTRY_DECOMP_OUTPUT.exists() and not overwrite:
        LOGGER.info("收益拆解缓存已存在，跳过重算：%s", INDUSTRY_DECOMP_OUTPUT)
        industry_panel = pd.read_parquet(INDUSTRY_DECOMP_OUTPUT)
    else:
        stock_panel = build_stock_return_decomposition(config)
        industry_panel = aggregate_to_industry(stock_panel, config)
        industry_panel = merge_lifecycle(industry_panel)
        industry_panel.to_parquet(INDUSTRY_DECOMP_OUTPUT, index=False)

    summary = summarize_by_lifecycle(industry_panel)
    yearly = summarize_by_year(industry_panel)
    summary.to_csv(SUMMARY_OUTPUT, index=False, encoding="utf-8-sig")
    yearly.to_parquet(PROCESSED_DATA_DIR / "lifecycle_return_decomposition_by_year.parquet", index=False)
    plot_lifecycle_decomposition(summary)
    plot_yearly_decomposition(yearly)
    # SKIP_FIG_CLEANUP=1 时保留中间分解图（供快照刷新链路避免批量删除触发沙箱确认）
    if not os.environ.get("SKIP_FIG_CLEANUP"):
        for path in OUTPUT_DIR.joinpath("figures").glob("return_decomposition_*.png"):
            path.unlink()
    return industry_panel


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复现研报 2.2 生命周期收益分解")
    parser.add_argument("--industry-scheme", choices=["hybrid", "fixed"], default="hybrid")
    parser.add_argument("--industry-level", type=int, default=4, choices=[2, 3, 4])
    parser.add_argument("--min-stocks", type=int, default=3)
    parser.add_argument("--min-tertiary-stocks", type=int, default=8)
    parser.add_argument("--target-industries", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = ValuationConfig(
        industry_scheme=args.industry_scheme,
        industry_level=args.industry_level,
        min_stocks=args.min_stocks,
        min_tertiary_stocks=args.min_tertiary_stocks,
        target_industries=args.target_industries,
    )
    panel = run_valuation(config, overwrite=args.overwrite)
    LOGGER.info("行业收益拆解面板：%s 行", len(panel))
    LOGGER.info("输出：%s", INDUSTRY_DECOMP_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
