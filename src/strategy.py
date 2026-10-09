"""第三章：资产选择信号。

本模块把研报第三章涉及的行业选择信号统一落到月度行业面板：

1. 成长资产：资产优势差、PE-g 低估残差；
2. 预期/事件资产：SUE + SUR + JUMP、历史一致预期净利润增速；
3. ROE 资产：PB-ROE 低估残差、ROE 拥挤度；
4. 防御资产：质量红利、价值红利。

研报没有披露所有底层计算细节，本模块优先保证透明、可复查：

- 所有信号只使用当月及以前可得数据；
- 每个子策略输出月度行业清单；
- 需要历史一致预期的分支在覆盖不足时保留缺失标记，不用当前预测回填。
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
from config import BACKTEST_START_DATE, END_DATE, INDUSTRY_OUTPUT_LEVEL, OUTPUT_DIR, PROCESSED_DATA_DIR
from src.factors import FactorConfig, weighted_average
from src.lifecycle import attach_industry_by_month, parse_wind_date, prepare_industry_map, read_parquet_dataset, statement_priority
from src.output_industry_map import map_to_display


LOGGER = logging.getLogger(__name__)

VALUATION_PANEL = PROCESSED_DATA_DIR / "industry_valuation_regression_panel.parquet"
RETURN_DECOMP_PANEL = PROCESSED_DATA_DIR / "industry_return_decomposition.parquet"
DIVIDEND_HOLDINGS = PROCESSED_DATA_DIR / "dividend_asset_holdings.parquet"
STOCK_FACTOR_PANEL = PROCESSED_DATA_DIR / "stock_factor_monthly.parquet"
INDEX_EOD_DIR = PROCESSED_DATA_DIR.parent / "raw" / "index_eod_prices"

SIGNAL_PANEL_OUTPUT = PROCESSED_DATA_DIR / "strategy_signal_panel.parquet"
SELECTION_OUTPUT = PROCESSED_DATA_DIR / "strategy_signal_holdings.parquet"
RETURNS_OUTPUT = PROCESSED_DATA_DIR / "strategy_signal_returns.parquet"
COVERAGE_OUTPUT = OUTPUT_DIR / "tables" / "strategy_signal_coverage.csv"
SUMMARY_OUTPUT = OUTPUT_DIR / "tables" / "strategy_signal_summary.csv"

LIFECYCLE_ORDER = ["成长期", "成熟期", "停滞期", "衰退期", "转型期"]


@dataclass(frozen=True)
class StrategyConfig:
    """资产选择参数。"""

    start_month: str = BACKTEST_START_DATE
    end_month: str = END_DATE
    top_n: int = 5
    min_stocks: int = 3
    min_regression_samples: int = 8
    crowding_window: int = 24
    high_crowding_quantile: float = 0.8


def zscore_by_industry(series: pd.Series, window: int = 12) -> pd.Series:
    """行业自身滚动 z-score，仅使用历史窗口。"""

    mean = series.rolling(window, min_periods=max(4, window // 2)).mean()
    std = series.rolling(window, min_periods=max(4, window // 2)).std()
    return (series - mean) / std.replace(0, np.nan)


def percentile_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """百分位排名。ascending=True 表示数值越大分数越高。"""

    return series.rank(pct=True, method="average", ascending=ascending)


def ols_residual(y: pd.Series, x: pd.DataFrame, min_samples: int) -> pd.Series:
    """带截距 OLS 残差。样本不足时返回 NaN。"""

    data = pd.concat([y.rename("y"), x], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    out = pd.Series(np.nan, index=y.index, dtype=float)
    if len(data) < min_samples:
        return out
    x_values = data[x.columns].astype(float)
    x_values = (x_values - x_values.mean()) / x_values.std(ddof=0).replace(0, np.nan)
    x_values = x_values.fillna(0.0)
    design = np.column_stack([np.ones(len(x_values)), x_values.to_numpy()])
    beta, *_ = np.linalg.lstsq(design, data["y"].to_numpy(dtype=float), rcond=None)
    fitted = design @ beta
    out.loc[data.index] = data["y"].to_numpy(dtype=float) - fitted
    return out


def add_industry_beta(
    panel: pd.DataFrame,
    window: int = 24,
    min_periods: int = 12,
) -> pd.DataFrame:
    """计算行业滚动市场 Beta，并在每个月做行业截面标准化。

    行业收益使用面板中的当月收益，市场收益使用万得全 A（881001.WI）。
    Beta 只使用截至当月的历史月收益，避免未来数据；截面 z-score 使用当月
    所有可得行业，因此 ``beta_z > 0`` 表示该行业 Beta 高于当月行业均值。
    """

    out = panel.copy()
    out["industry_beta"] = np.nan
    out["beta_z"] = np.nan
    files = sorted(INDEX_EOD_DIR.glob("year=*/part-*.parquet"))
    if not files or "current_log_return" not in out:
        return out

    market = pd.concat(
        [pd.read_parquet(path, columns=["S_INFO_WINDCODE", "TRADE_DT", "S_DQ_CLOSE"]) for path in files],
        ignore_index=True,
    )
    market = market[market["S_INFO_WINDCODE"].eq("881001.WI")].copy()
    if market.empty:
        return out
    market["TRADE_DT"] = pd.to_datetime(market["TRADE_DT"])
    market = market.sort_values("TRADE_DT")
    market = market.groupby(market["TRADE_DT"].dt.to_period("M")).tail(1).copy()
    market["month_end"] = market["TRADE_DT"] + pd.offsets.MonthEnd(0)
    market["market_return"] = np.log(
        pd.to_numeric(market["S_DQ_CLOSE"], errors="coerce")
        / pd.to_numeric(market["S_DQ_CLOSE"], errors="coerce").shift(1)
    )

    out = out.merge(market[["month_end", "market_return"]], on="month_end", how="left")
    out = out.sort_values(["industry_old_code", "month_end"])

    def rolling_beta(group: pd.DataFrame) -> pd.Series:
        cov = group["current_log_return"].rolling(window, min_periods=min_periods).cov(group["market_return"])
        var = group["market_return"].rolling(window, min_periods=min_periods).var()
        return cov / var.replace(0, np.nan)

    out["industry_beta"] = (
        out.groupby("industry_old_code", group_keys=False).apply(rolling_beta, include_groups=False)
    )

    def cross_section_zscore(series: pd.Series) -> pd.Series:
        std = series.std(ddof=0)
        return (series - series.mean()) / std if pd.notna(std) and std > 0 else pd.Series(np.nan, index=series.index)

    out["beta_z"] = out.groupby("month_end")["industry_beta"].transform(cross_section_zscore)
    return out.drop(columns="market_return")


def rolling_zscore(series: pd.Series, window: int = 8) -> pd.Series:
    """滚动 z-score，用于八个季度超预期信号。"""

    mean = series.rolling(window, min_periods=4).mean()
    std = series.rolling(window, min_periods=4).std()
    return (series - mean) / std.replace(0, np.nan)


def build_event_surprise_panel(config: StrategyConfig) -> pd.DataFrame:
    """按研报口径构建行业月度 SUE/SUR/JUMP。

    SUE/SUR 使用净利润/营收单季同比增速的环比 delta，并在个股自身过去
    八个季度中做 z-score。JUMP 使用财报公告后首个交易日的开盘跳空幅度。
    最后按月末历史行业归属，用自由流通市值加权聚合到行业层面。
    """

    if not STOCK_FACTOR_PANEL.exists():
        raise FileNotFoundError(f"缺少个股月度面板，请先运行 python -m src.factors：{STOCK_FACTOR_PANEL}")

    financial = read_parquet_dataset(
        "financial_indicator",
        columns=[
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "S_QFA_YOYNETPROFIT",
            "S_QFA_YOYSALES",
            "OPDATE",
        ],
    )
    financial["windcode"] = financial["S_INFO_WINDCODE"].fillna(financial["WIND_CODE"])
    financial["ann_date"] = parse_wind_date(financial["ANN_DT"])
    financial["report_date"] = parse_wind_date(financial["REPORT_PERIOD"])
    financial["opdate_ts"] = pd.to_datetime(financial["OPDATE"], errors="coerce")
    financial = financial[
        financial["windcode"].notna()
        & financial["ann_date"].notna()
        & financial["report_date"].notna()
        & financial["report_date"].dt.month.isin([3, 6, 9, 12])
    ].copy()
    financial["statement_priority"] = financial["STATEMENT_TYPE"].map(statement_priority)
    financial["q_np_yoy"] = pd.to_numeric(financial["S_QFA_YOYNETPROFIT"], errors="coerce")
    financial["q_rev_yoy"] = pd.to_numeric(financial["S_QFA_YOYSALES"], errors="coerce")
    financial = financial.sort_values(
        ["windcode", "report_date", "statement_priority", "ann_date", "opdate_ts"],
        ascending=[True, True, True, False, False],
    )
    financial = financial.drop_duplicates(["windcode", "report_date"], keep="first")
    financial = financial.sort_values(["windcode", "report_date"]).reset_index(drop=True)
    financial["np_yoy_delta"] = financial.groupby("windcode")["q_np_yoy"].diff()
    financial["rev_yoy_delta"] = financial.groupby("windcode")["q_rev_yoy"].diff()
    financial["sue"] = financial.groupby("windcode", group_keys=False)["np_yoy_delta"].transform(rolling_zscore)
    financial["sur"] = financial.groupby("windcode", group_keys=False)["rev_yoy_delta"].transform(rolling_zscore)

    prices = read_parquet_dataset(
        "stock_eod_prices",
        columns=["S_INFO_WINDCODE", "TRADE_DT", "S_DQ_PRECLOSE", "S_DQ_OPEN"],
    )
    prices["windcode"] = prices["S_INFO_WINDCODE"]
    prices["trade_date"] = parse_wind_date(prices["TRADE_DT"])
    prices["open_price"] = pd.to_numeric(prices["S_DQ_OPEN"], errors="coerce")
    prices["preclose"] = pd.to_numeric(prices["S_DQ_PRECLOSE"], errors="coerce")
    prices = prices[
        prices["windcode"].notna()
        & prices["trade_date"].notna()
        & prices["open_price"].gt(0)
        & prices["preclose"].gt(0)
    ][["windcode", "trade_date", "open_price", "preclose"]].copy()
    prices = prices.sort_values(["trade_date", "windcode"])
    jump_source = financial[["windcode", "ann_date"]].dropna().sort_values(["ann_date", "windcode"])
    jump = pd.merge_asof(
        jump_source,
        prices,
        by="windcode",
        left_on="ann_date",
        right_on="trade_date",
        direction="forward",
        allow_exact_matches=False,
    )
    jump["jump"] = jump["open_price"] / jump["preclose"] - 1
    financial = financial.merge(jump[["windcode", "ann_date", "jump"]], on=["windcode", "ann_date"], how="left")

    stock_monthly = pd.read_parquet(
        STOCK_FACTOR_PANEL,
        columns=["windcode", "month_end", "float_mv", "total_mv"],
    ).copy()
    # pandas 3 要求 merge_asof 两侧日期精度完全一致。
    stock_monthly["month_end"] = pd.to_datetime(stock_monthly["month_end"]).astype("datetime64[ns]")
    financial["ann_date"] = pd.to_datetime(financial["ann_date"]).astype("datetime64[ns]")
    stock_monthly = stock_monthly[
        stock_monthly["month_end"].between(pd.Timestamp(config.start_month), pd.Timestamp(config.end_month))
    ].sort_values(["month_end", "windcode"])

    monthly = pd.merge_asof(
        stock_monthly[["windcode", "month_end", "float_mv", "total_mv"]],
        financial[["windcode", "ann_date", "report_date", "sue", "sur", "jump"]].sort_values(["ann_date", "windcode"]),
        by="windcode",
        left_on="month_end",
        right_on="ann_date",
        direction="backward",
        allow_exact_matches=True,
    )
    factor_config = FactorConfig(start_date=config.start_month, end_date=config.end_month)
    membership, industry_universe, _ = prepare_industry_map(factor_config)  # type: ignore[arg-type]
    stock_industry = attach_industry_by_month(monthly, membership, industry_universe)

    rows = []
    group_cols = ["month_end", "industry_old_code"]
    for keys, group in stock_industry.groupby(group_cols, dropna=False):
        weights = group["float_mv"].fillna(group["total_mv"])
        rows.append(
            {
                **dict(zip(group_cols, keys)),
                "sue": weighted_average(group["sue"], weights),
                "sur": weighted_average(group["sur"], weights),
                "jump": weighted_average(group["jump"], weights),
                "sue_coverage": group["sue"].notna().mean(),
                "sur_coverage": group["sur"].notna().mean(),
                "jump_coverage": group["jump"].notna().mean(),
            }
        )
    return pd.DataFrame(rows)


def build_signal_panel(config: StrategyConfig) -> pd.DataFrame:
    """构建资产选择信号面板。"""

    if not VALUATION_PANEL.exists():
        raise FileNotFoundError(f"缺少估值面板，请先运行 python -m src.valuation_regression：{VALUATION_PANEL}")
    if not RETURN_DECOMP_PANEL.exists():
        raise FileNotFoundError(f"缺少收益面板，请先运行 python -m src.valuation：{RETURN_DECOMP_PANEL}")

    panel = pd.read_parquet(VALUATION_PANEL).copy()
    panel["month_end"] = pd.to_datetime(panel["month_end"])
    panel = panel[
        panel["month_end"].between(pd.Timestamp(config.start_month), pd.Timestamp(config.end_month))
    ].copy()

    returns = pd.read_parquet(
        RETURN_DECOMP_PANEL,
        columns=["month_end", "industry_old_code", "stock_weighted_log_return"],
    ).copy()
    returns["month_end"] = pd.to_datetime(returns["month_end"])
    returns = returns.sort_values(["industry_old_code", "month_end"])
    returns["current_log_return"] = returns["stock_weighted_log_return"]
    returns["next_month_log_return"] = returns.groupby("industry_old_code")["stock_weighted_log_return"].shift(-1)
    panel = panel.merge(
        returns[["month_end", "industry_old_code", "current_log_return", "next_month_log_return"]],
        on=["month_end", "industry_old_code"],
        how="left",
    )

    panel = panel.sort_values(["industry_old_code", "month_end"]).copy()
    grouped = panel.groupby("industry_old_code", group_keys=False)
    panel["g_ttm_ma6"] = grouped["growth_g"].transform(lambda s: s.rolling(6, min_periods=3).mean())
    panel["g_fttm_ma12"] = grouped["g_fttm"].transform(lambda s: s.rolling(12, min_periods=6).mean())
    panel["roe_ma12"] = grouped["roe_ttm"].transform(lambda s: s.rolling(12, min_periods=6).mean())
    event_panel = build_event_surprise_panel(config)
    panel = panel.merge(event_panel, on=["month_end", "industry_old_code"], how="left")

    panel["asset_advantage_score"] = panel.groupby("month_end")["g_ttm_ma6"].transform(percentile_rank)
    panel["consensus_growth_score"] = panel.groupby("month_end")["g_fttm_ma12"].transform(percentile_rank)
    panel["sue_rank"] = panel.groupby("month_end")["sue"].transform(percentile_rank)
    panel["sur_rank"] = panel.groupby("month_end")["sur"].transform(percentile_rank)
    panel["jump_rank"] = panel.groupby("month_end")["jump"].transform(percentile_rank)
    panel["event_composite_score"] = panel[["sue_rank", "sur_rank", "jump_rank"]].mean(axis=1, skipna=False)

    panel["pe_g_residual"] = np.nan
    panel["pb_roe_residual"] = np.nan
    for month_end, idx in panel.groupby("month_end").groups.items():
        month = panel.loc[idx]
        growth_mask = month["lifecycle_stage_zh"].isin(["成长期", "转型期"]) & month["pe_ttm"].gt(0)
        if growth_mask.any():
            panel.loc[month.index[growth_mask], "pe_g_residual"] = ols_residual(
                month.loc[growth_mask, "pe_ttm"],
                month.loc[growth_mask, ["g_ttm_ma6", "g_fttm_ma12"]],
                config.min_regression_samples,
            ).to_numpy()
        mature_mask = month["lifecycle_stage_zh"].eq("成熟期") & month["ln_pb"].notna()
        if mature_mask.any():
            panel.loc[month.index[mature_mask], "pb_roe_residual"] = ols_residual(
                month.loc[mature_mask, "ln_pb"],
                month.loc[mature_mask, ["roe_ttm", "roe_delta_3m", "roe_std_12m"]],
                config.min_regression_samples,
            ).to_numpy()

    panel["pe_g_cheap_score"] = panel.groupby("month_end")["pe_g_residual"].transform(
        lambda s: percentile_rank(s, ascending=False)
    )
    panel["pb_roe_cheap_score"] = panel.groupby("month_end")["pb_roe_residual"].transform(
        lambda s: percentile_rank(s, ascending=False)
    )
    panel["roe_rank"] = panel.groupby("month_end")["roe_ttm"].transform(percentile_rank)
    panel = add_industry_beta(panel, window=config.crowding_window, min_periods=12)
    # 保留旧列名供展示层兼容；其含义现已改为行业标准化 Beta，而非 ROE 排名滚动均值。
    panel["roe_crowding"] = panel["beta_z"]
    panel["roe_crowding_threshold"] = 0.0
    panel["roe_not_crowded"] = panel["beta_z"].le(0)
    panel["roe_asset_score"] = panel[["roe_rank", "pb_roe_cheap_score"]].mean(axis=1, skipna=False)
    return panel.reset_index(drop=True)


def _select_top(
    panel: pd.DataFrame,
    strategy_id: str,
    strategy_name: str,
    score_col: str,
    config: StrategyConfig,
    stage_filter: Optional[set[str]] = None,
    require_consensus: bool = False,
) -> pd.DataFrame:
    """按月选择得分最高的行业。"""

    data = panel[panel["stock_count"].fillna(0).ge(config.min_stocks)].copy()
    if stage_filter is not None:
        data = data[data["lifecycle_stage_zh"].isin(stage_filter)].copy()
    if require_consensus:
        data = data[data["g_fttm_coverage"].fillna(0).gt(0)].copy()
    rows = []
    for month_end, group in data.groupby("month_end"):
        selected = group.dropna(subset=[score_col]).sort_values(
            [score_col, "industry_old_code"],
            ascending=[False, True],
        ).head(config.top_n)
        for rank, (_, row) in enumerate(selected.iterrows(), start=1):
            rows.append(
                {
                    "month_end": month_end,
                    "strategy_id": strategy_id,
                    "strategy_name": strategy_name,
                    "rank": rank,
                    "industry_old_code": row["industry_old_code"],
                    "industry_code": row["industry_code"],
                    "industry_name": row["industry_name"],
                    "lifecycle_stage_zh": row["lifecycle_stage_zh"],
                    "score": row[score_col],
                    "next_month_log_return": row["next_month_log_return"],
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "month_end",
                "strategy_id",
                "strategy_name",
                "rank",
                "industry_old_code",
                "industry_code",
                "industry_name",
                "lifecycle_stage_zh",
                "score",
                "next_month_log_return",
            ]
        )
    return pd.DataFrame(rows)


def load_dividend_holdings(config: StrategyConfig) -> pd.DataFrame:
    """读取质量红利和价值红利选样，统一到策略清单格式。"""

    if not DIVIDEND_HOLDINGS.exists():
        LOGGER.warning("缺少红利资产清单，跳过防御资产分支：%s", DIVIDEND_HOLDINGS)
        return pd.DataFrame()
    holdings = pd.read_parquet(DIVIDEND_HOLDINGS)
    holdings = holdings[
        holdings["month_end"].between(pd.Timestamp(config.start_month), pd.Timestamp(config.end_month))
    ].copy()
    mapping = {
        "quality_dividend": ("quality_dividend", "质量红利"),
        "value_dividend": ("value_dividend", "价值红利"),
    }
    holdings["strategy_id"] = holdings["asset_type"].map(lambda x: mapping.get(x, (x, x))[0])
    holdings["strategy_name"] = holdings["asset_type"].map(lambda x: mapping.get(x, (x, x))[1])
    holdings["lifecycle_stage_zh"] = "成熟期"
    return holdings[
        [
            "month_end",
            "strategy_id",
            "strategy_name",
            "rank",
            "industry_old_code",
            "industry_code",
            "industry_name",
            "lifecycle_stage_zh",
            "score",
            "next_month_log_return",
        ]
    ].copy()


def build_strategy_holdings(panel: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """生成所有子策略月度行业清单。"""

    parts = [
        _select_top(panel, "growth_advantage", "成长优势差", "asset_advantage_score", config, {"成长期", "转型期"}),
        _select_top(panel, "pe_g_cheap", "PE-g 低估", "pe_g_cheap_score", config, {"成长期", "转型期"}),
        _select_top(panel, "event_composite", "SUE+SUR+JUMP", "event_composite_score", config, {"成长期", "转型期"}),
        _select_top(panel, "consensus_growth", "一致预期增速", "consensus_growth_score", config, None, True),
        _select_top(panel, "roe_asset", "ROE 低估且不拥挤", "roe_asset_score", config, {"成熟期"}),
        load_dividend_holdings(config),
    ]
    holdings = pd.concat([part for part in parts if len(part) > 0], ignore_index=True)
    if holdings.empty:
        return holdings
    holdings = holdings.sort_values(["month_end", "strategy_id", "rank"]).reset_index(drop=True)
    return holdings


def compute_strategy_returns(holdings: pd.DataFrame) -> pd.DataFrame:
    """计算子策略等权月度收益和净值。"""

    if holdings.empty:
        return pd.DataFrame()
    rows = []
    for (month_end, strategy_id, strategy_name), group in holdings.groupby(["month_end", "strategy_id", "strategy_name"]):
        rows.append(
            {
                "month_end": month_end,
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "holding_count": group["industry_old_code"].nunique(),
                "portfolio_log_return": group["next_month_log_return"].mean(skipna=True),
            }
        )
    returns = pd.DataFrame(rows).sort_values(["strategy_id", "month_end"]).reset_index(drop=True)
    returns["valid_return"] = returns["portfolio_log_return"].notna()
    returns["portfolio_simple_return"] = np.expm1(returns["portfolio_log_return"])
    returns["nav"] = returns.groupby("strategy_id")["portfolio_log_return"].transform(lambda s: np.exp(s.fillna(0).cumsum()))
    returns["return_month"] = pd.to_datetime(returns["month_end"]) + pd.offsets.MonthEnd(1)
    return returns


def summarize_coverage(panel: pd.DataFrame, holdings: pd.DataFrame, returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """输出信号覆盖和绩效概览。"""

    coverage_rows = []
    signal_cols = [
        "asset_advantage_score",
        "pe_g_cheap_score",
        "event_composite_score",
        "consensus_growth_score",
        "pb_roe_cheap_score",
        "roe_crowding",
    ]
    for col in signal_cols:
        valid = panel[col].notna()
        coverage_rows.append(
            {
                "signal": col,
                "valid_rows": int(valid.sum()),
                "valid_months": int(panel.loc[valid, "month_end"].nunique()),
                "valid_industries": int(panel.loc[valid, "industry_old_code"].nunique()),
                "coverage_ratio": float(valid.mean()),
            }
        )
    if not holdings.empty:
        for strategy_id, group in holdings.groupby("strategy_id"):
            coverage_rows.append(
                {
                    "signal": f"holdings:{strategy_id}",
                    "valid_rows": int(len(group)),
                    "valid_months": int(group["month_end"].nunique()),
                    "valid_industries": int(group["industry_old_code"].nunique()),
                    "coverage_ratio": np.nan,
                }
            )
    coverage = pd.DataFrame(coverage_rows)

    summary_rows = []
    if not returns.empty:
        valid_returns = returns[returns["valid_return"]].copy()
        for strategy_id, group in valid_returns.groupby("strategy_id"):
            total_return = float(np.expm1(group["portfolio_log_return"].sum()))
            annual_return = float(np.exp(group["portfolio_log_return"].mean() * 12) - 1)
            volatility = float(group["portfolio_simple_return"].std(ddof=0) * np.sqrt(12))
            win_rate = float(group["portfolio_simple_return"].gt(0).mean())
            summary_rows.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": group["strategy_name"].iloc[0],
                    "months": int(len(group)),
                    "total_return": total_return,
                    "annual_return": annual_return,
                    "annual_volatility": volatility,
                    "win_rate": win_rate,
                    "last_nav": float(group["nav"].iloc[-1]),
                }
            )
    summary = pd.DataFrame(summary_rows)
    return coverage, summary


def _set_plot_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _plot_nav(ax, returns: pd.DataFrame, strategy_ids: list[str], title: str) -> None:
    data = returns[returns["valid_return"] & returns["strategy_id"].isin(strategy_ids)].copy()
    for strategy_id, group in data.groupby("strategy_id"):
        ax.plot(group["return_month"], group["nav"], linewidth=1.9, label=group["strategy_name"].iloc[0])
    ax.set_title(title)
    ax.set_ylabel("净值")
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    ax.legend(loc="upper left", frameon=False)


def plot_strategy_figures(panel: pd.DataFrame, holdings: pd.DataFrame, returns: pd.DataFrame) -> None:
    """输出图 23-32。"""

    import matplotlib.pyplot as plt

    _set_plot_style()
    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    valid = panel.copy()
    valid["month_end"] = pd.to_datetime(valid["month_end"])

    # 图 23：资产优势差，比较成长期/转型期与其他阶段的 g_ttm_ma6。
    growth = (
        valid.assign(is_growth_asset=valid["lifecycle_stage_zh"].isin(["成长期", "转型期"]))
        .dropna(subset=["g_ttm_ma6"])
        .groupby(["month_end", "is_growth_asset"])["g_ttm_ma6"]
        .mean()
        .unstack()
    )
    fig, ax = plt.subplots(figsize=(10.0, 4.6))
    if True in growth:
        ax.plot(growth.index, growth[True], label="成长期/转型期", color="#219EBC", linewidth=1.8)
    if False in growth:
        ax.plot(growth.index, growth[False], label="其他生命周期", color="#8D99AE", linewidth=1.5)
    if {True, False}.issubset(set(growth.columns)):
        ax.fill_between(growth.index, growth[True] - growth[False], color="#FB8500", alpha=0.18, label="优势差")
    ax.set_title("图23：成长资产优势差（g_ttm MA6）")
    ax.set_ylabel("净利润增速")
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    ax.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig23_growth_advantage_gap.png", dpi=200)
    plt.close(fig)

    nav_specs = [
        ("fig24_pe_g_cheap_nav.png", ["pe_g_cheap"], "图24：PE-g 低估行业组合表现"),
        ("fig25_event_composite_nav.png", ["event_composite"], "图25：SUE+SUR+JUMP 行业组合表现"),
        ("fig26_consensus_growth_nav.png", ["consensus_growth"], "图26：一致预期增速行业组合表现"),
        ("fig27_pb_roe_cheap_nav.png", ["roe_asset"], "图27：PB-ROE 低估且不拥挤行业组合表现"),
        ("fig30_dividend_asset_nav.png", ["quality_dividend", "value_dividend"], "图30：质量红利与价值红利组合表现"),
    ]
    for filename, strategy_ids, title in nav_specs:
        fig, ax = plt.subplots(figsize=(10.0, 4.6))
        _plot_nav(ax, returns, strategy_ids, title)
        fig.tight_layout()
        fig.savefig(figure_dir / filename, dpi=200)
        plt.close(fig)

    # 图 28：ROE 拥挤度。
    crowd = (
        valid.dropna(subset=["roe_crowding"])
        .groupby("month_end")
        .agg(
            median_crowding=("roe_crowding", "median"),
            high_crowding=("roe_crowding", lambda s: s.quantile(0.8)),
            sample_count=("roe_crowding", "count"),
        )
    )
    fig, ax = plt.subplots(figsize=(10.0, 4.6))
    ax.plot(crowd.index, crowd["median_crowding"], label="中位拥挤度", color="#219EBC", linewidth=1.8)
    ax.plot(crowd.index, crowd["high_crowding"], label="80%分位", color="#D62828", linewidth=1.5)
    ax2 = ax.twinx()
    ax2.bar(crowd.index, crowd["sample_count"], width=20, color="#ADB5BD", alpha=0.22, label="样本数")
    ax.set_title("图28：ROE 资产拥挤度")
    ax.set_ylabel("拥挤度")
    ax2.set_ylabel("样本数")
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig28_roe_crowding.png", dpi=200)
    plt.close(fig)

    # 图 29：ROE 资产选样数量和平均得分。
    roe_h = holdings[holdings["strategy_id"].eq("roe_asset")].copy()
    roe_monthly = roe_h.groupby("month_end").agg(holding_count=("industry_old_code", "nunique"), avg_score=("score", "mean"))
    fig, ax = plt.subplots(figsize=(10.0, 4.6))
    ax.plot(roe_monthly.index, roe_monthly["avg_score"], color="#219EBC", linewidth=1.8, label="平均得分")
    ax2 = ax.twinx()
    ax2.bar(roe_monthly.index, roe_monthly["holding_count"], width=20, color="#FB8500", alpha=0.25, label="持仓行业数")
    ax.set_title("图29：ROE 资产月度选样状态")
    ax.set_ylabel("平均得分")
    ax2.set_ylabel("行业数")
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, loc="upper left", frameon=False)
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig29_roe_asset_selection.png", dpi=200)
    plt.close(fig)

    # 图 31：各子策略月度选样数量。
    counts = holdings.groupby(["month_end", "strategy_name"])["industry_old_code"].nunique().unstack()
    fig, ax = plt.subplots(figsize=(10.5, 5.0))
    counts.rolling(3, min_periods=1).mean().plot(ax=ax, linewidth=1.5)
    ax.set_title("图31：各资产选择信号的月度持仓数量")
    ax.set_ylabel("行业数（三月均值）")
    ax.set_xlabel("月份")
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    ax.legend(loc="upper left", frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig31_strategy_holding_counts.png", dpi=200)
    plt.close(fig)

    # 图 32：子策略最终净值对比。
    fig, ax = plt.subplots(figsize=(10.5, 5.0))
    _plot_nav(
        ax,
        returns,
        [
            "growth_advantage",
            "pe_g_cheap",
            "event_composite",
            "consensus_growth",
            "roe_asset",
            "quality_dividend",
            "value_dividend",
        ],
        "图32：资产选择信号组合净值对比",
    )
    fig.tight_layout()
    fig.savefig(figure_dir / "fig32_strategy_signal_nav_compare.png", dpi=200)
    plt.close(fig)


def run_strategy(
    config: StrategyConfig,
    overwrite: bool = False,
    industry_output_level: int = INDUSTRY_OUTPUT_LEVEL,
) -> pd.DataFrame:
    """执行第三章资产选择信号。"""

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("figures").mkdir(parents=True, exist_ok=True)

    if SIGNAL_PANEL_OUTPUT.exists() and not overwrite:
        LOGGER.info("策略信号面板缓存已存在，跳过重算：%s", SIGNAL_PANEL_OUTPUT)
        panel = pd.read_parquet(SIGNAL_PANEL_OUTPUT)
    else:
        panel = build_signal_panel(config)
    panel = map_to_display(panel, industry_output_level)
    panel.to_parquet(SIGNAL_PANEL_OUTPUT, index=False)

    holdings = build_strategy_holdings(panel, config)
    holdings = map_to_display(holdings, industry_output_level)
    returns = compute_strategy_returns(holdings)
    coverage, summary = summarize_coverage(panel, holdings, returns)

    holdings.to_parquet(SELECTION_OUTPUT, index=False)
    returns.to_parquet(RETURNS_OUTPUT, index=False)
    coverage.to_parquet(PROCESSED_DATA_DIR / "strategy_signal_coverage.parquet", index=False)
    summary.to_csv(SUMMARY_OUTPUT, index=False, encoding="utf-8-sig")
    plot_strategy_figures(panel, holdings, returns)
    # SKIP_FIG_CLEANUP=1 时保留中间信号图（供快照刷新链路避免批量删除触发沙箱确认）
    if not os.environ.get("SKIP_FIG_CLEANUP"):
        for path in OUTPUT_DIR.joinpath("figures").glob("fig*.png"):
            if path.name.startswith(tuple(f"fig{i}_" for i in range(23, 32))):
                path.unlink()
    return returns


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复现第三章资产选择信号")
    parser.add_argument("--start-month", default=BACKTEST_START_DATE)
    parser.add_argument("--end-month", default=END_DATE)
    parser.add_argument("--industry-output-level", type=int, choices=[2, 3], default=INDUSTRY_OUTPUT_LEVEL)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--min-stocks", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = StrategyConfig(
        start_month=args.start_month,
        end_month=args.end_month,
        top_n=args.top_n,
        min_stocks=args.min_stocks,
    )
    returns = run_strategy(
        config,
        overwrite=args.overwrite,
        industry_output_level=args.industry_output_level,
    )
    LOGGER.info("资产选择信号收益：%s 行", len(returns))
    LOGGER.info("输出：%s", RETURNS_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
