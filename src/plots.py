"""
研报第二章图表复现。

当前实现图 4-6：不同生命周期阶段行业的未来 6 个月资产特征暴露。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

os.environ.setdefault("MPLCONFIGDIR", str(Path("/tmp") / "matplotlib-cache"))
os.environ.setdefault("XDG_CACHE_HOME", str(Path("/tmp") / "matplotlib-cache"))

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import OUTPUT_DIR, PROCESSED_DATA_DIR
from src.lifecycle import (
    LifecycleConfig,
    month_ends,
    parse_wind_date,
    prepare_quarterly_cashflow_ttm,
    prepare_industry_map,
    read_parquet_dataset,
)


LOGGER = logging.getLogger(__name__)

FACTOR_PANEL = PROCESSED_DATA_DIR / "industry_factor_panel.parquet"
LIFECYCLE_PANEL = PROCESSED_DATA_DIR / "industry_lifecycle_monthly.parquet"
STATS_OUTPUT = OUTPUT_DIR / "tables" / "lifecycle_future_factor_stats.csv"
CASHFLOW_CHANGE_STATS_OUTPUT = OUTPUT_DIR / "tables" / "cashflow_change_future_factor_stats.csv"
CASHFLOW_PMP_STATS_OUTPUT = OUTPUT_DIR / "tables" / "cashflow_change_plus_minus_plus_by_lifecycle.csv"
MATURE_CASHFLOW_CHANGE_STATS_OUTPUT = OUTPUT_DIR / "tables" / "mature_cashflow_change_future_factor_stats.csv"
BEER_CASE_OUTPUT = OUTPUT_DIR / "tables" / "beer_cashflow_roe_case.csv"

STAGE_ORDER = ["成长期", "成熟期", "停滞期", "衰退期", "转型期"]
CHANGE_PATTERN_ORDER = ["+++", "++-", "+-+", "+--", "-++", "-+-", "--+", "---"]
FIGURE_SPECS = {
    "growth_g": ("fig4_lifecycle_growth.png", "未来成长 g 水平"),
    "roe_ttm": ("fig5_lifecycle_roe.png", "未来盈利 ROE 水平"),
    "dividend_yield": ("fig6_lifecycle_dividend.png", "未来分红 D 水平"),
}
FACTOR_LABELS = {
    "growth_g": "成长 g",
    "roe_ttm": "盈利 ROE",
    "dividend_yield": "分红 D",
}
MATURE_FIGURE_SPECS = {
    "roe_ttm": ("fig9_mature_cashflow_change_roe.png", "成熟期现金流边际变化模式对未来 ROE 水平的影响"),
    "dividend_yield": ("fig12_mature_cashflow_change_dividend.png", "成熟期现金流边际变化模式对未来股息率水平的影响"),
}


def zscore_by_month(panel: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    """将行业因子转成月度横截面 z-score 暴露。"""

    panel = panel.copy()
    for col in factor_cols:
        def _zscore(series: pd.Series) -> pd.Series:
            std = series.std(skipna=True)
            if pd.isna(std) or std == 0:
                return series * float("nan")
            return (series - series.mean(skipna=True)) / std

        panel[f"{col}_exposure"] = panel.groupby("month_end", group_keys=False)[col].transform(_zscore)
    return panel


def add_forward_average(panel: pd.DataFrame, factor_cols: list[str], months: int = 6) -> pd.DataFrame:
    """计算 t+1 至 t+N 月的未来平均暴露。"""

    panel = panel.sort_values(["industry_old_code", "month_end"]).copy()
    for col in factor_cols:
        exposure_col = f"{col}_exposure"

        def _forward(series: pd.Series) -> pd.Series:
            future = [series.shift(-i) for i in range(1, months + 1)]
            return pd.concat(future, axis=1).mean(axis=1, skipna=True)

        panel[f"future_{months}m_{col}_exposure"] = panel.groupby("industry_old_code")[
            exposure_col
        ].transform(_forward)
    return panel


def compute_lifecycle_future_stats(panel: pd.DataFrame, months: int = 6) -> pd.DataFrame:
    """按生命周期统计未来 N 个月因子暴露。"""

    factor_cols = list(FIGURE_SPECS)
    panel = zscore_by_month(panel, factor_cols)
    panel = add_forward_average(panel, factor_cols, months=months)
    panel = panel[panel["lifecycle_stage_zh"].isin(STAGE_ORDER)].copy()

    rows = []
    for factor in factor_cols:
        value_col = f"future_{months}m_{factor}_exposure"
        grouped = panel.groupby("lifecycle_stage_zh")[value_col]
        for stage in STAGE_ORDER:
            values = grouped.get_group(stage) if stage in grouped.groups else pd.Series(dtype=float)
            rows.append(
                {
                    "factor": factor,
                    "stage": stage,
                    "future_months": months,
                    "count": int(values.notna().sum()),
                    "mean_exposure": values.mean(skipna=True),
                    "median_exposure": values.median(skipna=True),
                }
            )
    stats = pd.DataFrame(rows)
    stats["stage"] = pd.Categorical(stats["stage"], categories=STAGE_ORDER, ordered=True)
    return stats.sort_values(["factor", "stage"]).reset_index(drop=True)


def change_sign(series: pd.Series) -> pd.Series:
    """把现金流环比变化转为 + / - 符号；0 或缺失记为 NA。"""

    sign = pd.Series(pd.NA, index=series.index, dtype="string")
    sign[series > 0] = "+"
    sign[series < 0] = "-"
    return sign


def add_cashflow_change_pattern(panel: pd.DataFrame) -> pd.DataFrame:
    """合并行业现金流边际变化模式。

    三个符号分别代表 CFO、CFI、CFF 的 TTM 现金流较上月变化方向。
    """

    if not LIFECYCLE_PANEL.exists():
        raise FileNotFoundError(f"缺少生命周期面板，请先运行 python -m src.lifecycle：{LIFECYCLE_PANEL}")

    cashflow = pd.read_parquet(
        LIFECYCLE_PANEL,
        columns=["month_end", "industry_old_code", "cfo", "cfi", "cff"],
    ).sort_values(["industry_old_code", "month_end"])
    for col in ["cfo", "cfi", "cff"]:
        cashflow[f"d_{col}"] = cashflow.groupby("industry_old_code")[col].diff()
        cashflow[f"d_{col}_sign"] = cashflow.groupby("industry_old_code", group_keys=False)[
            f"d_{col}"
        ].transform(change_sign)
    cashflow["cashflow_change_pattern"] = (
        cashflow["d_cfo_sign"] + cashflow["d_cfi_sign"] + cashflow["d_cff_sign"]
    )
    return panel.merge(
        cashflow[
            [
                "month_end",
                "industry_old_code",
                "d_cfo",
                "d_cfi",
                "d_cff",
                "cashflow_change_pattern",
            ]
        ],
        on=["month_end", "industry_old_code"],
        how="left",
    )


def _future_exposure_panel(panel: pd.DataFrame, months: int) -> pd.DataFrame:
    """补充月度 z-score 暴露和未来 N 个月平均暴露。"""

    factor_cols = list(FIGURE_SPECS)
    panel = zscore_by_month(panel, factor_cols)
    return add_forward_average(panel, factor_cols, months=months)


def compute_cashflow_change_stats(panel: pd.DataFrame, months: int = 6) -> pd.DataFrame:
    """图 7：按现金流边际变化模式统计未来因子暴露。"""

    panel = _future_exposure_panel(add_cashflow_change_pattern(panel), months)
    panel = panel[panel["cashflow_change_pattern"].isin(CHANGE_PATTERN_ORDER)].copy()

    rows = []
    for factor in FIGURE_SPECS:
        value_col = f"future_{months}m_{factor}_exposure"
        grouped = panel.groupby("cashflow_change_pattern")[value_col]
        for pattern in CHANGE_PATTERN_ORDER:
            values = grouped.get_group(pattern) if pattern in grouped.groups else pd.Series(dtype=float)
            rows.append(
                {
                    "factor": factor,
                    "cashflow_change_pattern": pattern,
                    "future_months": months,
                    "count": int(values.notna().sum()),
                    "mean_exposure": values.mean(skipna=True),
                    "median_exposure": values.median(skipna=True),
                }
            )
    stats = pd.DataFrame(rows)
    stats["cashflow_change_pattern"] = pd.Categorical(
        stats["cashflow_change_pattern"], categories=CHANGE_PATTERN_ORDER, ordered=True
    )
    return stats.sort_values(["factor", "cashflow_change_pattern"]).reset_index(drop=True)


def compute_pmp_by_lifecycle_stats(panel: pd.DataFrame, months: int = 6) -> pd.DataFrame:
    """图 8：+-+ 模式在不同生命周期阶段的未来因子暴露。"""

    panel = _future_exposure_panel(add_cashflow_change_pattern(panel), months)
    panel = panel[
        panel["cashflow_change_pattern"].eq("+-+")
        & panel["lifecycle_stage_zh"].isin(STAGE_ORDER)
    ].copy()

    rows = []
    for factor in FIGURE_SPECS:
        value_col = f"future_{months}m_{factor}_exposure"
        grouped = panel.groupby("lifecycle_stage_zh")[value_col]
        for stage in STAGE_ORDER:
            values = grouped.get_group(stage) if stage in grouped.groups else pd.Series(dtype=float)
            rows.append(
                {
                    "factor": factor,
                    "stage": stage,
                    "cashflow_change_pattern": "+-+",
                    "future_months": months,
                    "count": int(values.notna().sum()),
                    "mean_exposure": values.mean(skipna=True),
                    "median_exposure": values.median(skipna=True),
                }
            )
    stats = pd.DataFrame(rows)
    stats["stage"] = pd.Categorical(stats["stage"], categories=STAGE_ORDER, ordered=True)
    return stats.sort_values(["factor", "stage"]).reset_index(drop=True)


def compute_mature_cashflow_change_stats(panel: pd.DataFrame, months: int = 6) -> pd.DataFrame:
    """图 9/12：成熟期中现金流边际变化模式与未来 ROE、股息率。"""

    panel = _future_exposure_panel(add_cashflow_change_pattern(panel), months)
    panel = panel[
        panel["lifecycle_stage_zh"].eq("成熟期")
        & panel["cashflow_change_pattern"].isin(CHANGE_PATTERN_ORDER)
    ].copy()

    rows = []
    for factor in MATURE_FIGURE_SPECS:
        value_col = f"future_{months}m_{factor}_exposure"
        grouped = panel.groupby("cashflow_change_pattern")[value_col]
        for pattern in CHANGE_PATTERN_ORDER:
            values = grouped.get_group(pattern) if pattern in grouped.groups else pd.Series(dtype=float)
            rows.append(
                {
                    "factor": factor,
                    "cashflow_change_pattern": pattern,
                    "lifecycle_stage_zh": "成熟期",
                    "future_months": months,
                    "count": int(values.notna().sum()),
                    "industry_count": int(
                        panel.loc[
                            panel["cashflow_change_pattern"].eq(pattern) & panel[value_col].notna(),
                            "industry_old_code",
                        ].nunique()
                    ),
                    "mean_exposure": values.mean(skipna=True),
                    "median_exposure": values.median(skipna=True),
                }
            )
    stats = pd.DataFrame(rows)
    stats["cashflow_change_pattern"] = pd.Categorical(
        stats["cashflow_change_pattern"], categories=CHANGE_PATTERN_ORDER, ordered=True
    )
    return stats.sort_values(["factor", "cashflow_change_pattern"]).reset_index(drop=True)


def _read_stock_operating_revenue_monthly() -> pd.DataFrame:
    """读取个股月末 TTM 营业收入。"""

    derivative = read_parquet_dataset(
        "stock_eod_derivative",
        columns=[
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "OPER_REV_TTM",
        ],
    )
    derivative["windcode"] = derivative["S_INFO_WINDCODE"]
    derivative["trade_date"] = parse_wind_date(derivative["TRADE_DT"])
    derivative = derivative[
        derivative["windcode"].notna()
        & derivative["trade_date"].notna()
    ].copy()
    derivative["month_end"] = derivative["trade_date"].dt.to_period("M").dt.to_timestamp("M")
    derivative = derivative.sort_values(["windcode", "month_end", "trade_date"])
    derivative = derivative.drop_duplicates(["windcode", "month_end"], keep="last")
    derivative["oper_rev_ttm"] = pd.to_numeric(derivative["OPER_REV_TTM"], errors="coerce")
    return derivative[["windcode", "month_end", "oper_rev_ttm"]].reset_index(drop=True)


def build_beer_case_panel() -> pd.DataFrame:
    """图 10/11：啤酒板块现金流占营业收入比与 ROE 案例。

    啤酒三级行业在历史中对应过不同父级行业，hybrid 行业口径会把其中一段
    合并到二级行业。案例图按“申万三级名称=啤酒”的股票历史成分重新
    聚合，避免 2020 年后案例序列断裂。
    """

    lifecycle_config = LifecycleConfig()
    membership, _, hybrid_map = prepare_industry_map(lifecycle_config)
    beer_tertiary_codes = set(
        hybrid_map.loc[hybrid_map["tertiary_name"].eq("啤酒"), "tertiary_old_code"].astype(str)
    )
    if not beer_tertiary_codes:
        LOGGER.warning("未找到申万三级啤酒行业映射，图 10/11 将输出空表。")
        return pd.DataFrame()

    beer_membership = membership[membership["sw_level3_old_code"].astype(str).isin(beer_tertiary_codes)].copy()
    beer_stocks = pd.Index(beer_membership["windcode"].dropna().unique(), name="windcode")
    if beer_stocks.empty:
        LOGGER.warning("申万三级啤酒行业无历史股票成分，图 10/11 将输出空表。")
        return pd.DataFrame()

    factor_stock = pd.read_parquet(
        PROCESSED_DATA_DIR / "stock_factor_monthly.parquet",
        columns=["windcode", "month_end", "float_mv", "total_mv", "roe_ttm"],
    )
    months = month_ends(str(factor_stock["month_end"].min().date()), str(factor_stock["month_end"].max().date()))
    grid = pd.MultiIndex.from_product([beer_stocks, months], names=["windcode", "month_end"])
    grid_df = grid.to_frame(index=False).sort_values(["month_end", "windcode"])

    cashflow = prepare_quarterly_cashflow_ttm().sort_values(["available_date", "windcode"])
    stock_cashflow = pd.merge_asof(
        grid_df,
        cashflow[["windcode", "available_date", "report_date", "cfo_ttm", "cfi_ttm", "cff_ttm"]],
        by="windcode",
        left_on="month_end",
        right_on="available_date",
        direction="backward",
        allow_exact_matches=True,
    )
    stock_cashflow = stock_cashflow.merge(
        beer_membership[
            [
                "windcode",
                "entry_date",
                "remove_date",
                "sw_level3_old_code",
            ]
        ],
        on="windcode",
        how="left",
    )
    active = (
        (stock_cashflow["entry_date"] <= stock_cashflow["month_end"])
        & (stock_cashflow["remove_date"].isna() | (stock_cashflow["remove_date"] > stock_cashflow["month_end"]))
    )
    stock_cashflow = stock_cashflow[active].drop_duplicates(["windcode", "month_end"], keep="last")

    revenue = _read_stock_operating_revenue_monthly()
    stock_panel = stock_cashflow.merge(revenue, on=["windcode", "month_end"], how="left").merge(
        factor_stock,
        on=["windcode", "month_end"],
        how="left",
    )
    stock_panel["weight"] = stock_panel["float_mv"].fillna(stock_panel["total_mv"])

    rows = []
    for month_end, group in stock_panel.groupby("month_end"):
        weights = group["weight"]
        roe_mask = group["roe_ttm"].notna() & weights.notna() & (weights > 0)
        rows.append(
            {
                "month_end": month_end,
                "industry_old_code": "beer_tertiary",
                "industry_name": "啤酒",
                "cfo": group["cfo_ttm"].sum(min_count=1),
                "cfi": group["cfi_ttm"].sum(min_count=1),
                "cff": group["cff_ttm"].sum(min_count=1),
                "roe_ttm": (group.loc[roe_mask, "roe_ttm"] * weights.loc[roe_mask]).sum()
                / weights.loc[roe_mask].sum()
                if roe_mask.any()
                else float("nan"),
                "oper_rev_ttm": group["oper_rev_ttm"].sum(min_count=1),
                "stock_count": group["windcode"].nunique(),
                "revenue_stock_count": group["oper_rev_ttm"].notna().sum(),
                "roe_stock_count": group["roe_ttm"].notna().sum(),
            }
        )
    panel = pd.DataFrame(rows).sort_values("month_end")
    for col in ["cfo", "cfi", "cff"]:
        panel[f"{col}_to_revenue"] = panel[col] / panel["oper_rev_ttm"].where(panel["oper_rev_ttm"] > 0)
    return panel.sort_values("month_end").reset_index(drop=True)


def plot_lifecycle_bars(stats: pd.DataFrame) -> None:
    """输出图 4-6：左轴柱状图为因子暴露，右轴折线图为样本数量。"""

    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    colors = ["#8ECAE6", "#219EBC", "#023047", "#FFB703", "#FB8500"]

    for factor, (filename, title) in FIGURE_SPECS.items():
        data = stats[stats["factor"].eq(factor)].set_index("stage").reindex(STAGE_ORDER)
        labels = data.index.astype(str)
        x = range(len(labels))
        fig, ax = plt.subplots(figsize=(7.6, 4.4))
        bars = ax.bar(x, data["mean_exposure"], color=colors, width=0.62, label="平均因子暴露")
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_title(title)
        ax.set_ylabel("未来 6 个月平均因子暴露（z-score）")
        ax.set_xlabel("生命周期阶段")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels)
        for idx, value in enumerate(data["mean_exposure"]):
            if pd.notna(value):
                ax.text(idx, value, f"{value:.2f}", ha="center", va="bottom" if value >= 0 else "top")

        ax_count = ax.twinx()
        ax_count.plot(
            list(x),
            data["count"],
            color="#D62828",
            marker="o",
            linewidth=1.8,
            label="样本数量",
        )
        ax_count.set_ylabel("样本数量")
        for idx, value in enumerate(data["count"]):
            if pd.notna(value):
                ax_count.text(idx, value, f"{int(value)}", color="#D62828", ha="center", va="bottom")

        handles = [bars, ax_count.lines[0]]
        labels_legend = [handle.get_label() for handle in handles]
        ax.legend(handles, labels_legend, loc="upper left", frameon=False)
        fig.tight_layout()
        fig.savefig(figure_dir / filename, dpi=200)
        plt.close(fig)


def plot_cashflow_change_patterns(stats: pd.DataFrame) -> None:
    """输出图 7：现金流边际变化模式与未来资产特征。"""

    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    data = stats.pivot(
        index="cashflow_change_pattern",
        columns="factor",
        values="mean_exposure",
    ).reindex(CHANGE_PATTERN_ORDER)
    counts = (
        stats[stats["factor"].eq("growth_g")]
        .set_index("cashflow_change_pattern")
        .reindex(CHANGE_PATTERN_ORDER)["count"]
    )

    x = list(range(len(data.index)))
    width = 0.24
    colors = {"growth_g": "#219EBC", "roe_ttm": "#023047", "dividend_yield": "#FB8500"}
    fig, ax = plt.subplots(figsize=(9.6, 4.8))
    for offset, factor in zip([-width, 0, width], FIGURE_SPECS):
        ax.bar(
            [i + offset for i in x],
            data[factor],
            width=width,
            label=FACTOR_LABELS[factor],
            color=colors[factor],
        )
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title("现金流边际变化模式与未来资产特征")
    ax.set_ylabel("未来 6 个月平均因子暴露（z-score）")
    ax.set_xlabel("现金流边际变化模式（经营 / 投资 / 筹资）")
    ax.set_xticks(x)
    ax.set_xticklabels(data.index.astype(str))
    ax.legend(loc="upper left", frameon=False)

    ax_count = ax.twinx()
    ax_count.plot(x, counts, color="#D62828", marker="o", linewidth=1.8, label="样本数量")
    ax_count.set_ylabel("样本数量")
    for idx, value in enumerate(counts):
        if pd.notna(value):
            ax_count.text(idx, value, f"{int(value)}", color="#D62828", ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(figure_dir / "fig7_cashflow_change_patterns.png", dpi=200)
    plt.close(fig)


def plot_pmp_by_lifecycle(stats: pd.DataFrame) -> None:
    """输出图 8：+-+ 模式在不同生命周期阶段的影响。"""

    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    data = stats.pivot(index="stage", columns="factor", values="mean_exposure").reindex(STAGE_ORDER)
    counts = stats[stats["factor"].eq("growth_g")].set_index("stage").reindex(STAGE_ORDER)["count"]

    x = list(range(len(data.index)))
    width = 0.24
    colors = {"growth_g": "#219EBC", "roe_ttm": "#023047", "dividend_yield": "#FB8500"}
    fig, ax = plt.subplots(figsize=(8.6, 4.6))
    for offset, factor in zip([-width, 0, width], FIGURE_SPECS):
        ax.bar(
            [i + offset for i in x],
            data[factor],
            width=width,
            label=FACTOR_LABELS[factor],
            color=colors[factor],
        )
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title("+-+ 现金流边际变化在不同生命周期阶段的影响")
    ax.set_ylabel("未来 6 个月平均因子暴露（z-score）")
    ax.set_xlabel("生命周期阶段")
    ax.set_xticks(x)
    ax.set_xticklabels(data.index.astype(str))
    ax.legend(loc="upper left", frameon=False)

    ax_count = ax.twinx()
    ax_count.plot(x, counts, color="#D62828", marker="o", linewidth=1.8, label="样本数量")
    ax_count.set_ylabel("样本数量")
    for idx, value in enumerate(counts):
        if pd.notna(value):
            ax_count.text(idx, value, f"{int(value)}", color="#D62828", ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(figure_dir / "fig8_cashflow_change_pmp_by_lifecycle.png", dpi=200)
    plt.close(fig)


def plot_mature_cashflow_change_bars(stats: pd.DataFrame) -> None:
    """输出图 9/12：成熟期现金流边际变化与未来 ROE/股息率。"""

    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    colors = ["#8ECAE6", "#219EBC", "#023047", "#FFB703", "#FB8500", "#D62828", "#6A4C93", "#2A9D8F"]

    for factor, (filename, title) in MATURE_FIGURE_SPECS.items():
        data = stats[stats["factor"].eq(factor)].set_index("cashflow_change_pattern").reindex(CHANGE_PATTERN_ORDER)
        x = list(range(len(data.index)))
        fig, ax = plt.subplots(figsize=(9.4, 4.8))
        bars = ax.bar(x, data["mean_exposure"], color=colors, width=0.62, label="平均因子暴露")
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_title(title)
        ax.set_ylabel("未来 6 个月平均因子暴露（z-score）")
        ax.set_xlabel("现金流边际变化模式（经营 / 投资 / 筹资）")
        ax.set_xticks(x)
        ax.set_xticklabels(data.index.astype(str))
        for idx, value in enumerate(data["mean_exposure"]):
            if pd.notna(value):
                ax.text(idx, value, f"{value:.2f}", ha="center", va="bottom" if value >= 0 else "top")

        ax_count = ax.twinx()
        ax_count.plot(x, data["count"], color="#D62828", marker="o", linewidth=1.8, label="样本数量")
        ax_count.set_ylabel("样本数量")
        for idx, value in enumerate(data["count"]):
            if pd.notna(value):
                ax_count.text(idx, value, f"{int(value)}", color="#D62828", ha="center", va="bottom")

        handles = [bars, ax_count.lines[0]]
        ax.legend(handles, [handle.get_label() for handle in handles], loc="upper left", frameon=False)
        fig.tight_layout()
        fig.savefig(figure_dir / filename, dpi=200)
        plt.close(fig)


def plot_beer_case(panel: pd.DataFrame) -> None:
    """输出图 10/11：啤酒板块案例。"""

    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    if panel.empty:
        return

    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["month_end"])
    fig, ax = plt.subplots(figsize=(10.0, 4.6))
    ax.plot(panel["date"], panel["cfo_to_revenue"], label="经营现金流 / 营收", color="#219EBC", linewidth=1.8)
    ax.plot(panel["date"], panel["cfi_to_revenue"], label="投资现金流 / 营收", color="#023047", linewidth=1.8)
    ax.plot(panel["date"], panel["cff_to_revenue"], label="筹资现金流 / 营收", color="#FB8500", linewidth=1.8)
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title("图10：啤酒板块现金流占营业收入比变化")
    ax.set_ylabel("现金流 / TTM 营业收入")
    ax.set_xlabel("月份")
    ax.legend(loc="upper left", frameon=False, ncol=3)
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig10_beer_cashflow_to_revenue.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10.0, 4.4))
    ax.plot(panel["date"], panel["roe_ttm"], label="ROE_TTM", color="#D62828", linewidth=2.0)
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title("图11：啤酒板块 ROE 变化")
    ax.set_ylabel("ROE_TTM（%）")
    ax.set_xlabel("月份")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig11_beer_roe.png", dpi=200)
    plt.close(fig)


def run_plots(months: int = 6) -> pd.DataFrame:
    """生成统计表和图 4-12。"""

    if not FACTOR_PANEL.exists():
        raise FileNotFoundError(f"缺少行业因子面板，请先运行 python -m src.factors：{FACTOR_PANEL}")

    panel = pd.read_parquet(FACTOR_PANEL)
    stats = compute_lifecycle_future_stats(panel, months=months)
    cashflow_change_stats = compute_cashflow_change_stats(panel, months=months)
    pmp_stats = compute_pmp_by_lifecycle_stats(panel, months=months)
    mature_cashflow_change_stats = compute_mature_cashflow_change_stats(panel, months=months)
    beer_case = build_beer_case_panel()
    STATS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    stats.to_csv(STATS_OUTPUT, index=False, encoding="utf-8-sig")
    cashflow_change_stats.to_parquet(PROCESSED_DATA_DIR / "cashflow_change_future_factor_stats.parquet", index=False)
    pmp_stats.to_parquet(PROCESSED_DATA_DIR / "cashflow_change_pmp_by_lifecycle.parquet", index=False)
    mature_cashflow_change_stats.to_parquet(PROCESSED_DATA_DIR / "mature_cashflow_change_future_factor_stats.parquet", index=False)
    beer_case.to_parquet(PROCESSED_DATA_DIR / "beer_cashflow_roe_case.parquet", index=False)
    plot_lifecycle_bars(stats)
    plot_cashflow_change_patterns(cashflow_change_stats)
    plot_pmp_by_lifecycle(pmp_stats)
    plot_mature_cashflow_change_bars(mature_cashflow_change_stats)
    plot_beer_case(beer_case)
    # SKIP_FIG_CLEANUP=1 时保留中间图（供快照刷新链路避免批量删除触发沙箱确认）
    if not os.environ.get("SKIP_FIG_CLEANUP"):
        keep = {"fig4_lifecycle_growth.png", "fig5_lifecycle_roe.png", "fig6_lifecycle_dividend.png"}
        for path in OUTPUT_DIR.joinpath("figures").glob("*.png"):
            if path.name.startswith(("fig7_", "fig8_", "fig9_", "fig10_", "fig11_", "fig12_")) and path.name not in keep:
                path.unlink()
    return stats


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复现第二章生命周期资产特征图表")
    parser.add_argument("--future-months", type=int, default=6)
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats = run_plots(months=args.future_months)
    LOGGER.info("生命周期未来因子统计：%s 行", len(stats))
    LOGGER.info("输出：%s", STATS_OUTPUT)
    LOGGER.info("输出：%s", MATURE_CASHFLOW_CHANGE_STATS_OUTPUT)
    LOGGER.info("输出：%s", BEER_CASE_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
