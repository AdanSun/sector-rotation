"""第五章：资产比较决策树与月频回测。

回测只消费已经生成好的第三章信号，不重新计算底层因子。这样做的好处是：

- 决策树每月选择了什么资产、什么行业，都能和 `strategy_signal_holdings.parquet`
  对上；
- 月末形成组合，使用下一月申万行业指数收益，避免未来函数；
- 空仓、样本不足、交易成本都显式留痕。
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
from config import BACKTEST_START_DATE, END_DATE, INDUSTRY_OUTPUT_LEVEL, OUTPUT_DIR, PROCESSED_DATA_DIR, TRANSACTION_COST
from src.output_industry_map import map_to_display


LOGGER = logging.getLogger(__name__)

SIGNAL_PANEL = PROCESSED_DATA_DIR / "strategy_signal_panel.parquet"
SIGNAL_HOLDINGS = PROCESSED_DATA_DIR / "strategy_signal_holdings.parquet"
SIGNAL_RETURNS = PROCESSED_DATA_DIR / "strategy_signal_returns.parquet"
INDEX_EOD_DIR = PROCESSED_DATA_DIR.parent / "raw" / "index_eod_prices"
SW_INDEX_EOD_DIR = PROCESSED_DATA_DIR.parent / "raw" / "sw_index_eod_prices"
INDEX_DESCRIPTION = PROCESSED_DATA_DIR.parent / "raw" / "index_description" / "part-00000.parquet"

DECISION_PANEL_OUTPUT = PROCESSED_DATA_DIR / "asset_decision_tree_panel.parquet"
MONTHLY_POSITIONS_OUTPUT = PROCESSED_DATA_DIR / "backtest_monthly_positions.parquet"
MONTHLY_RETURNS_OUTPUT = PROCESSED_DATA_DIR / "backtest_monthly_returns.parquet"
DAILY_RETURNS_OUTPUT = PROCESSED_DATA_DIR / "backtest_daily_returns.parquet"
ANNUAL_RETURNS_OUTPUT = OUTPUT_DIR / "tables" / "backtest_annual_returns.csv"
METRICS_OUTPUT = OUTPUT_DIR / "tables" / "backtest_metrics.csv"


@dataclass(frozen=True)
class BacktestConfig:
    """回测参数。"""

    start_month: str = BACKTEST_START_DATE
    end_month: str = END_DATE
    top_n: int = 5
    transaction_cost: float = TRANSACTION_COST
    min_growth_confirmed: int = 2
    min_asset_holdings: int = 2
    min_return_coverage: float = 0.8
    min_benchmark_industries: int = 50
    trend_delta_months: int = 3


def normalize_weights(codes: list[str]) -> dict[str, float]:
    """行业等权。"""

    unique_codes = sorted(pd.unique(pd.Series(codes).dropna()))
    if not unique_codes:
        return {}
    weight = 1.0 / len(unique_codes)
    return {code: weight for code in unique_codes}


def compute_turnover(prev_weights: dict[str, float], current_weights: dict[str, float]) -> float:
    """单边换手率。"""

    keys = set(prev_weights) | set(current_weights)
    return 0.5 * sum(abs(current_weights.get(key, 0.0) - prev_weights.get(key, 0.0)) for key in keys)


def max_drawdown(nav: pd.Series) -> float:
    """最大回撤。"""

    if nav.empty:
        return np.nan
    running_max = nav.cummax()
    drawdown = nav / running_max - 1.0
    return float(drawdown.min())


def _strategy_group(holdings: pd.DataFrame, month_end: pd.Timestamp, strategy_id: str) -> pd.DataFrame:
    return holdings[
        holdings["month_end"].eq(month_end)
        & holdings["strategy_id"].eq(strategy_id)
    ].copy()


def _combine_ranked(groups: list[pd.DataFrame], top_n: int) -> pd.DataFrame:
    """多个子策略清单合并为行业平均得分。"""

    valid_groups = [g for g in groups if not g.empty]
    if not valid_groups:
        return pd.DataFrame()
    data = pd.concat(valid_groups, ignore_index=True)
    combined = (
        data.groupby(["industry_old_code", "industry_code", "industry_name"], as_index=False)
        .agg(
            score=("score", "mean"),
            next_month_log_return=("next_month_log_return", "mean"),
            source_count=("strategy_id", "nunique"),
        )
        .sort_values(["source_count", "score", "industry_old_code"], ascending=[False, False, True])
        .head(top_n)
    )
    return combined


def _spread_top_bottom(group: pd.DataFrame, col: str) -> float:
    """截面头尾五分组中位数差。"""

    data = group[["industry_old_code", col]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 20:
        return np.nan
    ranks = data[col].rank(pct=True, method="average")
    top = data.loc[ranks.gt(0.8), col]
    bottom = data.loc[ranks.le(0.2), col]
    if top.empty or bottom.empty:
        return np.nan
    return float(top.median() - bottom.median())


def _top_quintile_median(group: pd.DataFrame, col: str) -> float:
    """截面最高五分组中位数。"""

    data = group[["industry_old_code", col]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 20:
        return np.nan
    ranks = data[col].rank(pct=True, method="average")
    top = data.loc[ranks.gt(0.8), col]
    return float(top.median()) if not top.empty else np.nan


def _roe_crowd(group: pd.DataFrame) -> float:
    """成熟行业中 ROE 最高五分组的平均标准化 Beta。"""

    data = group.loc[
        group["lifecycle_stage_zh"].eq("成熟期"),
        ["industry_old_code", "roe_ttm", "beta_z"],
    ].replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 5:
        return np.nan
    roe_rank = data["roe_ttm"].rank(pct=True, method="average")
    top = data.loc[roe_rank.gt(0.8), "beta_z"]
    return float(top.mean()) if not top.empty else np.nan


def build_asset_availability(signal_panel: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    """按研报“有没有/挤不挤”口径构造每月资产可用性。"""

    panel = signal_panel.copy()
    panel["month_end"] = pd.to_datetime(panel["month_end"])
    rows = []
    for month_end, group in panel.groupby("month_end"):
        rows.append(
            {
                "month_end": month_end,
                # “有没有实际增速资产”在全行业截面判断；
                # 后续具体选股仍由 event_composite 限定在成长期+转型期。
                "actual_growth_spread": _spread_top_bottom(group, "growth_g"),
                "expected_growth_spread": _spread_top_bottom(group, "g_fttm"),
                "roe_top_median": _top_quintile_median(group, "roe_ttm"),
                "roe_market_crowding": _roe_crowd(group),
            }
        )
    availability = pd.DataFrame(rows).sort_values("month_end").reset_index(drop=True)
    availability["actual_growth_spread_ma6"] = availability["actual_growth_spread"].rolling(6, min_periods=3).mean()
    availability["expected_growth_spread_ma12"] = availability["expected_growth_spread"].rolling(12, min_periods=6).mean()
    availability["roe_top_median_ma6"] = availability["roe_top_median"].rolling(6, min_periods=3).mean()
    availability["roe_crowding_threshold"] = 0.0
    delta = config.trend_delta_months
    availability["actual_growth_trend"] = availability["actual_growth_spread_ma6"] - availability["actual_growth_spread_ma6"].shift(delta)
    availability["expected_growth_trend"] = availability["expected_growth_spread_ma12"] - availability["expected_growth_spread_ma12"].shift(delta)
    availability["roe_trend"] = availability["roe_top_median_ma6"] - availability["roe_top_median_ma6"].shift(delta)
    availability["roe_crowding_high"] = availability["roe_market_crowding"].gt(0)
    availability["actual_growth_available"] = availability["actual_growth_trend"].gt(0)
    availability["expected_growth_available"] = availability["expected_growth_trend"].gt(0)
    availability["roe_available"] = availability["roe_trend"].gt(0) & ~availability["roe_crowding_high"].fillna(False)
    return availability


def decide_month(
    month_end: pd.Timestamp,
    holdings: pd.DataFrame,
    availability: pd.DataFrame,
    config: BacktestConfig,
) -> tuple[dict[str, object], pd.DataFrame]:
    """按研报式决策树决定当月资产类别和行业持仓。"""

    actual_growth = _strategy_group(holdings, month_end, "event_composite")
    expected_growth = _strategy_group(holdings, month_end, "consensus_growth")
    roe = _strategy_group(holdings, month_end, "roe_asset")
    quality = _strategy_group(holdings, month_end, "quality_dividend")
    value = _strategy_group(holdings, month_end, "value_dividend")

    available_row = availability[availability["month_end"].eq(month_end)]
    if available_row.empty:
        available = {}
    else:
        available = available_row.iloc[0].to_dict()

    main_groups = []
    active_main_assets = []
    if bool(available.get("actual_growth_available", False)) and len(actual_growth) >= config.min_asset_holdings:
        main_groups.append(actual_growth)
        active_main_assets.append("实际增速资产")
    if bool(available.get("expected_growth_available", False)) and len(expected_growth) >= config.min_asset_holdings:
        main_groups.append(expected_growth)
        active_main_assets.append("预期增速资产")
    if bool(available.get("roe_available", False)) and len(roe) >= config.min_asset_holdings:
        main_groups.append(roe)
        active_main_assets.append("盈利能力资产")

    if main_groups:
        selected = _combine_ranked(main_groups, config.top_n * len(main_groups))
        decision_score = float(len(main_groups))
        asset_type = "main_assets"
        asset_name = "+".join(active_main_assets)
        reason = "主流资产存在，三类主流资产分别选优后整体等权"
    elif bool(available.get("roe_crowding_high", False)) and len(value) >= config.min_asset_holdings:
        selected = _combine_ranked([value], config.top_n)
        decision_score = 0.0
        asset_type = "value_dividend"
        asset_name = "价值红利"
        reason = "主流资产缺失且 ROE 拥挤度高，选择价值红利防御"
    elif len(quality) >= config.min_asset_holdings:
        selected = _combine_ranked([quality], config.top_n)
        decision_score = 0.0
        asset_type = "quality_dividend"
        asset_name = "质量红利"
        reason = "主流资产缺失且 ROE 拥挤度不高，选择质量红利防御"
    elif len(value) >= config.min_asset_holdings:
        selected = _combine_ranked([value], config.top_n)
        decision_score = 0.0
        asset_type = "value_dividend"
        asset_name = "价值红利"
        reason = "主流资产缺失，质量红利不可用，选择价值红利防御"
    else:
        decision_score, asset_type, asset_name, reason, selected = 0.0, "cash", "现金", "无可用资产信号", pd.DataFrame()

    state = {
        "month_end": month_end,
        "asset_type": asset_type,
        "asset_name": asset_name,
        "reason": reason,
        "decision_score": decision_score,
        "actual_growth_available": bool(available.get("actual_growth_available", False)),
        "expected_growth_available": bool(available.get("expected_growth_available", False)),
        "roe_available": bool(available.get("roe_available", False)),
        "roe_crowding_high": bool(available.get("roe_crowding_high", False)),
        "actual_growth_trend": available.get("actual_growth_trend", np.nan),
        "expected_growth_trend": available.get("expected_growth_trend", np.nan),
        "roe_trend": available.get("roe_trend", np.nan),
        "actual_growth_count": int(actual_growth["industry_old_code"].nunique()),
        "expected_growth_count": int(expected_growth["industry_old_code"].nunique()),
        "roe_asset_count": int(roe["industry_old_code"].nunique()),
        "quality_dividend_count": int(quality["industry_old_code"].nunique()),
        "value_dividend_count": int(value["industry_old_code"].nunique()),
        "selected_count": int(selected["industry_old_code"].nunique()) if not selected.empty else 0,
    }
    if selected.empty:
        return state, pd.DataFrame()

    positions = selected.copy()
    positions["month_end"] = month_end
    positions["asset_type"] = asset_type
    positions["asset_name"] = asset_name
    positions["weight"] = 1.0 / len(positions)
    positions["rank"] = np.arange(1, len(positions) + 1)
    return state, positions[
        [
            "month_end",
            "asset_type",
            "asset_name",
            "rank",
            "industry_old_code",
            "industry_code",
            "industry_name",
            "weight",
            "score",
            "source_count",
            "next_month_log_return",
        ]
    ].copy()


def build_benchmark_returns(signal_panel: pd.DataFrame, months: Iterable[pd.Timestamp], min_industries: int = 50) -> pd.DataFrame:
    """构造万得全 A 月度基准；缺失时退回行业等权。"""

    index_files = sorted(INDEX_EOD_DIR.glob("year=*/part-*.parquet"))
    if index_files:
        index_data = pd.concat([pd.read_parquet(path) for path in index_files], ignore_index=True)
        index_data = index_data[index_data["S_INFO_WINDCODE"].eq("881001.WI")].copy()
        if not index_data.empty:
            index_data["TRADE_DT"] = pd.to_datetime(index_data["TRADE_DT"].astype("string"), errors="coerce")
            index_data = index_data.sort_values("TRADE_DT")
            month_close = index_data.groupby(index_data["TRADE_DT"].dt.to_period("M")).tail(1).copy()
            month_close["month_end"] = month_close["TRADE_DT"] + pd.offsets.MonthEnd(0)
            month_close["index_month_log_return"] = np.log(month_close["S_DQ_CLOSE"] / month_close["S_DQ_CLOSE"].shift(1))
            month_close["benchmark_log_return"] = month_close["index_month_log_return"].shift(-1)
            bench = month_close[["month_end", "benchmark_log_return"]].copy()
            bench = bench[bench["month_end"].isin(list(months))]
            bench["benchmark_industry_count"] = np.nan
            bench["total_industry_count"] = np.nan
            bench["benchmark_coverage"] = 1.0
            return bench

    data = signal_panel.copy()
    data["month_end"] = pd.to_datetime(data["month_end"])
    data = data[data["month_end"].isin(list(months))].copy()
    total = (
        data.groupby("month_end", as_index=False)
        .agg(total_industry_count=("industry_old_code", "nunique"))
    )
    bench = (
        data.dropna(subset=["next_month_log_return"])
        .groupby("month_end", as_index=False)
        .agg(
            benchmark_log_return=("next_month_log_return", "mean"),
            benchmark_industry_count=("industry_old_code", "nunique"),
        )
    )
    bench = bench.merge(total, on="month_end", how="right")
    bench["benchmark_coverage"] = bench["benchmark_industry_count"] / bench["total_industry_count"]
    bench.loc[bench["benchmark_industry_count"].fillna(0).lt(min_industries), "benchmark_log_return"] = np.nan
    return bench


def build_industry_index_returns() -> pd.DataFrame:
    """构造申万行业指数的当月及次月对数收益。

    同名新旧指数交替时，当月优先使用未标记“退市”的指数；新指数
    尚无行情时自动回退到旧指数。收益由每月最后一个交易日收盘价计算。
    """

    files = sorted(SW_INDEX_EOD_DIR.glob("year=*/part-*.parquet"))
    if not INDEX_DESCRIPTION.exists() or not files:
        raise FileNotFoundError("缺少行业指数描述或日行情数据")

    description = pd.read_parquet(INDEX_DESCRIPTION, columns=["S_INFO_WINDCODE", "S_INFO_NAME"])
    description = description[
        description["S_INFO_WINDCODE"].astype(str).str.match(r"\d+\.SI")
        & description["S_INFO_NAME"].astype(str).str.contains(r"\(申万\)", regex=True, na=False)
    ].copy()
    description["industry_name"] = description["S_INFO_NAME"].astype(str).str.replace(
        r"\(申万\)(\(退市\))?$", "", regex=True
    )
    description["active_priority"] = ~description["S_INFO_NAME"].astype(str).str.contains("退市", na=False)

    prices = pd.concat(
        [pd.read_parquet(path, columns=["S_INFO_WINDCODE", "TRADE_DT", "S_DQ_CLOSE"]) for path in files],
        ignore_index=True,
    )
    prices = prices.merge(description, on="S_INFO_WINDCODE", how="inner")
    prices["TRADE_DT"] = pd.to_datetime(prices["TRADE_DT"].astype("string"), errors="coerce")
    prices["S_DQ_CLOSE"] = pd.to_numeric(prices["S_DQ_CLOSE"], errors="coerce")
    prices = prices.dropna(subset=["S_DQ_CLOSE"]).sort_values(["S_INFO_WINDCODE", "TRADE_DT"])
    month_close = prices.groupby(
        ["S_INFO_WINDCODE", prices["TRADE_DT"].dt.to_period("M")], as_index=False
    ).tail(1).copy()
    month_close["month_end"] = month_close["TRADE_DT"] + pd.offsets.MonthEnd(0)
    month_close["current_index_log_return"] = month_close.groupby("S_INFO_WINDCODE")["S_DQ_CLOSE"].transform(
        lambda close: np.log(close / close.shift(1))
    )
    month_close["next_month_log_return"] = month_close.groupby("S_INFO_WINDCODE")["current_index_log_return"].shift(-1)
    next_observation_month = month_close.groupby("S_INFO_WINDCODE")["month_end"].shift(-1)
    month_close.loc[
        next_observation_month.ne(month_close["month_end"] + pd.offsets.MonthEnd(1)),
        "next_month_log_return",
    ] = np.nan
    month_close = month_close.dropna(subset=["next_month_log_return"])
    month_close = month_close.sort_values(
        ["industry_name", "month_end", "active_priority", "S_INFO_WINDCODE"],
        ascending=[True, True, False, True],
    ).drop_duplicates(["industry_name", "month_end"], keep="first")
    return month_close[["month_end", "industry_name", "S_INFO_WINDCODE", "next_month_log_return"]].rename(
        columns={"S_INFO_WINDCODE": "industry_index_code"}
    )


def attach_industry_index_returns(positions: pd.DataFrame) -> pd.DataFrame:
    """将持仓的收益实现端替换为同名申万行业指数。"""

    if positions.empty:
        return positions.copy()
    index_returns = build_industry_index_returns()
    out = positions.drop(columns=["next_month_log_return"], errors="ignore").merge(
        index_returns,
        on=["month_end", "industry_name"],
        how="left",
        validate="many_to_one",
    )
    # 历史指数库中少数三级名称不带“Ⅲ”，只对未匹配项做严格别名回补。
    missing = out["next_month_log_return"].isna() & out["industry_name"].astype(str).str.endswith("Ⅲ")
    if missing.any():
        aliases = index_returns.rename(columns={"industry_name": "index_industry_name"})
        retry = out.loc[missing, ["month_end", "industry_name"]].copy()
        retry["index_industry_name"] = retry["industry_name"].str.removesuffix("Ⅲ")
        retry = retry.merge(aliases, on=["month_end", "index_industry_name"], how="left")
        out.loc[missing, "industry_index_code"] = retry["industry_index_code"].to_numpy()
        out.loc[missing, "next_month_log_return"] = retry["next_month_log_return"].to_numpy()
    return out


def build_daily_returns(
    positions: pd.DataFrame, states: pd.DataFrame, monthly: pd.DataFrame, config: BacktestConfig
) -> pd.DataFrame:
    """用次月逐交易日收益构造连续绩效序列。

    信号和换仓仍为月末频率；交易成本在次月首个交易日扣除。
    """

    sw_files = sorted(SW_INDEX_EOD_DIR.glob("year=*/part-*.parquet"))
    index_files = sorted(INDEX_EOD_DIR.glob("year=*/part-*.parquet"))
    if not sw_files or not index_files:
        raise FileNotFoundError("缺少构造日频绩效所需的指数日行情")

    wanted = positions["industry_index_code"].dropna().astype(str).unique().tolist()
    prices = pd.concat(
        [pd.read_parquet(p, columns=["S_INFO_WINDCODE", "TRADE_DT", "S_DQ_CLOSE"]) for p in sw_files],
        ignore_index=True,
    )
    prices = prices[prices["S_INFO_WINDCODE"].astype(str).isin(wanted)].copy()
    prices["date"] = pd.to_datetime(prices["TRADE_DT"].astype("string"), errors="coerce")
    prices["close"] = pd.to_numeric(prices["S_DQ_CLOSE"], errors="coerce")
    prices = prices.dropna(subset=["date", "close"]).sort_values(["S_INFO_WINDCODE", "date"])
    prices["daily_log_return"] = prices.groupby("S_INFO_WINDCODE")["close"].transform(lambda x: np.log(x / x.shift()))

    benchmark = pd.concat(
        [pd.read_parquet(p, columns=["S_INFO_WINDCODE", "TRADE_DT", "S_DQ_CLOSE"]) for p in index_files],
        ignore_index=True,
    )
    benchmark = benchmark[benchmark["S_INFO_WINDCODE"].eq("881001.WI")].copy()
    benchmark["date"] = pd.to_datetime(benchmark["TRADE_DT"].astype("string"), errors="coerce")
    benchmark["close"] = pd.to_numeric(benchmark["S_DQ_CLOSE"], errors="coerce")
    benchmark = benchmark.dropna(subset=["date", "close"]).sort_values("date")
    benchmark["benchmark_log_return"] = np.log(benchmark["close"] / benchmark["close"].shift())
    benchmark = benchmark[["date", "benchmark_log_return"]].dropna()

    state_lookup = states.set_index("month_end")[["asset_type", "asset_name"]].to_dict("index")
    parts: list[pd.DataFrame] = []
    for _, row in monthly.iterrows():
        signal_month = pd.Timestamp(row["month_end"])
        start = signal_month + pd.Timedelta(days=1)
        end = signal_month + pd.offsets.MonthEnd(1)
        calendar = benchmark[benchmark["date"].between(start, end)].copy()
        if calendar.empty:
            continue
        month_positions = positions[positions["month_end"].eq(signal_month)]
        state = state_lookup.get(signal_month, {})
        if state.get("asset_type") == "cash":
            calendar["gross_log_return"] = 0.0
            calendar["return_coverage"] = 1.0
            calendar["valid_return"] = True
        else:
            codes = month_positions["industry_index_code"].dropna().astype(str).unique()
            selected = prices[
                prices["S_INFO_WINDCODE"].astype(str).isin(codes) & prices["date"].between(start, end)
            ]
            pivot = selected.pivot_table(index="date", columns="S_INFO_WINDCODE", values="daily_log_return", aggfunc="last")
            calendar = calendar.merge(pivot, left_on="date", right_index=True, how="left")
            return_cols = [c for c in codes if c in calendar.columns]
            calendar["return_coverage"] = calendar[return_cols].notna().sum(axis=1) / max(len(month_positions), 1)
            calendar["valid_return"] = calendar["return_coverage"].ge(config.min_return_coverage)
            calendar["gross_log_return"] = calendar[return_cols].mean(axis=1).where(calendar["valid_return"])
        calendar["turnover"] = np.nan
        calendar["transaction_cost"] = 0.0
        calendar.loc[calendar.index[0], "turnover"] = row["turnover"]
        calendar.loc[calendar.index[0], "transaction_cost"] = row["transaction_cost"]
        calendar["gross_simple_return"] = np.expm1(calendar["gross_log_return"])
        calendar["net_simple_return"] = calendar["gross_simple_return"] - calendar["transaction_cost"]
        calendar["net_log_return"] = np.log1p(calendar["net_simple_return"].clip(lower=-0.999999))
        calendar["month_end"] = signal_month
        calendar["asset_type"] = state.get("asset_type")
        calendar["asset_name"] = state.get("asset_name")
        parts.append(calendar[["date", "month_end", "asset_type", "asset_name", "return_coverage", "valid_return", "turnover", "transaction_cost", "gross_log_return", "gross_simple_return", "net_log_return", "net_simple_return", "benchmark_log_return"]])

    daily = pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)
    daily["strategy_nav"] = np.exp(daily["net_log_return"].fillna(0).cumsum())
    daily["benchmark_nav"] = np.exp(daily["benchmark_log_return"].fillna(0).cumsum())
    daily["excess_nav"] = daily["strategy_nav"] / daily["benchmark_nav"]
    return daily


def run_decision_tree(
    holdings: pd.DataFrame,
    signal_panel: pd.DataFrame,
    config: BacktestConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """生成决策状态、持仓和月度收益。"""

    holdings = holdings.copy()
    holdings["month_end"] = pd.to_datetime(holdings["month_end"])
    availability = build_asset_availability(signal_panel, config)
    months = sorted(
        m
        for m in holdings["month_end"].dropna().unique()
        if pd.Timestamp(config.start_month) <= pd.Timestamp(m) <= pd.Timestamp(config.end_month)
    )

    states = []
    position_parts = []
    for month_end in months:
        state, positions = decide_month(pd.Timestamp(month_end), holdings, availability, config)
        states.append(state)
        if not positions.empty:
            position_parts.append(positions)

    states_df = pd.DataFrame(states).sort_values("month_end").reset_index(drop=True)
    positions_df = (
        pd.concat(position_parts, ignore_index=True).sort_values(["month_end", "rank"]).reset_index(drop=True)
        if position_parts
        else pd.DataFrame()
    )
    positions_df = attach_industry_index_returns(positions_df)

    benchmark = build_benchmark_returns(signal_panel, months, config.min_benchmark_industries)
    returns_rows = []
    prev_weights: dict[str, float] = {}
    for _, state in states_df.iterrows():
        month_end = pd.Timestamp(state["month_end"])
        month_positions = positions_df[positions_df["month_end"].eq(month_end)] if not positions_df.empty else pd.DataFrame()
        current_weights = normalize_weights(month_positions["industry_old_code"].tolist()) if not month_positions.empty else {}
        valid_positions = month_positions.dropna(subset=["next_month_log_return"]) if not month_positions.empty else month_positions
        return_coverage = len(valid_positions) / len(month_positions) if len(month_positions) else 1.0
        is_cash = state["asset_type"] == "cash"
        valid_return = is_cash or (len(month_positions) > 0 and return_coverage >= config.min_return_coverage)
        turnover = compute_turnover(prev_weights, current_weights) if valid_return else np.nan
        if is_cash:
            gross_log_return = 0.0
            cost = turnover * config.transaction_cost
            net_simple_return = -cost
            net_log_return = np.log1p(max(net_simple_return, -0.999999))
        elif valid_return:
            valid_positions = valid_positions.copy()
            valid_positions["effective_weight"] = valid_positions["weight"] / valid_positions["weight"].sum()
            gross_log_return = float((valid_positions["effective_weight"] * valid_positions["next_month_log_return"]).sum())
            cost = turnover * config.transaction_cost
            net_simple_return = np.expm1(gross_log_return) - cost
            net_log_return = np.log1p(max(net_simple_return, -0.999999))
        else:
            gross_log_return = np.nan
            cost = np.nan
            net_simple_return = np.nan
            net_log_return = np.nan
        returns_rows.append(
            {
                "month_end": month_end,
                "return_month": month_end + pd.offsets.MonthEnd(1),
                "asset_type": state["asset_type"],
                "asset_name": state["asset_name"],
                "holding_count": len(current_weights),
                "return_coverage": return_coverage,
                "valid_return": valid_return,
                "turnover": turnover,
                "transaction_cost": cost,
                "gross_log_return": gross_log_return,
                "gross_simple_return": np.expm1(gross_log_return),
                "net_log_return": net_log_return,
                "net_simple_return": net_simple_return,
            }
        )
        if valid_return:
            prev_weights = current_weights

    returns_df = pd.DataFrame(returns_rows)
    returns_df = returns_df.merge(benchmark, on="month_end", how="left")
    returns_df["valid_benchmark"] = returns_df["benchmark_log_return"].notna()
    returns_df["strategy_nav"] = np.exp(returns_df["net_log_return"].fillna(0).cumsum())
    returns_df["benchmark_nav"] = np.exp(returns_df["benchmark_log_return"].fillna(0).cumsum())
    returns_df["excess_log_return"] = returns_df["net_log_return"].fillna(0) - returns_df["benchmark_log_return"].fillna(0)
    returns_df["excess_nav"] = np.exp(returns_df["excess_log_return"].cumsum())
    return states_df, positions_df, returns_df


def annual_returns(monthly: pd.DataFrame) -> pd.DataFrame:
    """年度收益。"""

    data = monthly[monthly["valid_return"]].copy()
    data["year"] = pd.to_datetime(data["return_month"]).dt.year
    yearly = (
        data.groupby("year", as_index=False)
        .agg(
            strategy_log_return=("net_log_return", "sum"),
            benchmark_log_return=("benchmark_log_return", "sum"),
            month_count=("net_log_return", "count"),
            avg_turnover=("turnover", "mean"),
        )
    )
    yearly["strategy_return"] = np.expm1(yearly["strategy_log_return"])
    yearly["benchmark_return"] = np.expm1(yearly["benchmark_log_return"])
    yearly["excess_return"] = yearly["strategy_return"] - yearly["benchmark_return"]
    return yearly


def performance_metrics(monthly: pd.DataFrame) -> pd.DataFrame:
    """回测指标。"""

    if monthly.empty:
        return pd.DataFrame()
    valid = monthly[monthly["valid_return"]].copy()
    months = len(valid)
    total_return = float(valid["strategy_nav"].iloc[-1] - 1)
    benchmark_total_return = float(valid["benchmark_nav"].iloc[-1] - 1)
    annual_return = float(np.exp(valid["net_log_return"].mean() * 12) - 1)
    benchmark_annual_return = float(np.exp(valid["benchmark_log_return"].fillna(0).mean() * 12) - 1)
    volatility = float(valid["net_simple_return"].std(ddof=0) * np.sqrt(12))
    benchmark_volatility = float(np.expm1(valid["benchmark_log_return"].fillna(0)).std(ddof=0) * np.sqrt(12))
    downside = valid.loc[valid["net_simple_return"].lt(0), "net_simple_return"].std(ddof=0) * np.sqrt(12)
    sharpe = annual_return / volatility if volatility and not np.isnan(volatility) else np.nan
    calmar = annual_return / abs(max_drawdown(monthly["strategy_nav"])) if max_drawdown(monthly["strategy_nav"]) < 0 else np.nan
    metrics = {
        "months": months,
        "total_return": total_return,
        "benchmark_total_return": benchmark_total_return,
        "annual_return": annual_return,
        "benchmark_annual_return": benchmark_annual_return,
        "annual_volatility": volatility,
        "benchmark_annual_volatility": benchmark_volatility,
        "downside_volatility": float(downside) if not np.isnan(downside) else np.nan,
        "max_drawdown": max_drawdown(valid["strategy_nav"]),
        "benchmark_max_drawdown": max_drawdown(valid["benchmark_nav"]),
        "sharpe_like": sharpe,
        "calmar_like": calmar,
        "win_rate": float(valid["net_simple_return"].gt(0).mean()),
        "avg_turnover": float(valid["turnover"].mean()),
        "total_transaction_cost": float(valid["transaction_cost"].sum()),
        "last_strategy_nav": float(valid["strategy_nav"].iloc[-1]),
        "last_benchmark_nav": float(valid["benchmark_nav"].iloc[-1]),
    }
    return pd.DataFrame([metrics])


def _set_plot_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def plot_decision_tree() -> None:
    """输出简化版图 33。"""

    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    _set_plot_style()
    fig, ax = plt.subplots(figsize=(10.2, 5.2))
    ax.axis("off")
    boxes = [
        (0.08, 0.68, "成长优势差\n+\nPE-g 低估", "共同确认？"),
        (0.39, 0.68, "主流成长资产", "行业等权持有"),
        (0.08, 0.36, "ROE 资产", "PB-ROE 低估\n且不拥挤？"),
        (0.39, 0.36, "ROE 资产", "行业等权持有"),
        (0.08, 0.08, "质量/价值红利", "防御资产"),
        (0.39, 0.08, "红利资产", "行业等权持有"),
        (0.70, 0.08, "现金", "无信号时空仓"),
    ]
    for x, y, title, subtitle in boxes:
        patch = FancyBboxPatch(
            (x, y),
            0.22,
            0.18,
            boxstyle="round,pad=0.02,rounding_size=0.03",
            linewidth=1.2,
            edgecolor="#457B9D",
            facecolor="#F1FAEE",
        )
        ax.add_patch(patch)
        ax.text(x + 0.11, y + 0.115, title, ha="center", va="center", fontsize=12, weight="bold")
        ax.text(x + 0.11, y + 0.045, subtitle, ha="center", va="center", fontsize=10, color="#555555")
    arrows = [
        ((0.30, 0.77), (0.39, 0.77), "是"),
        ((0.19, 0.68), (0.19, 0.54), "否"),
        ((0.30, 0.45), (0.39, 0.45), "是"),
        ((0.19, 0.36), (0.19, 0.26), "否"),
        ((0.30, 0.17), (0.39, 0.17), "有"),
        ((0.61, 0.17), (0.70, 0.17), "无"),
    ]
    for (x1, y1), (x2, y2), label in arrows:
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1), arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "#1D3557"})
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.025, label, ha="center", fontsize=10, color="#1D3557")
    ax.set_title("图33：资产比较决策树（简化复现）", fontsize=15, weight="bold")
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "figures" / "fig33_asset_decision_tree.png", dpi=200)
    plt.close(fig)


def plot_backtest(monthly: pd.DataFrame, yearly: pd.DataFrame, states: pd.DataFrame) -> None:
    """输出图 34-35。"""

    import matplotlib.pyplot as plt

    _set_plot_style()
    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10.5, 5.0))
    valid_monthly = monthly[monthly["valid_return"]].copy()
    ax.plot(valid_monthly["return_month"], valid_monthly["strategy_nav"], label="资产比较策略", color="#D62828", linewidth=2.0)
    ax.plot(valid_monthly["return_month"], valid_monthly["benchmark_nav"], label="行业等权基准", color="#457B9D", linewidth=1.7)
    ax.set_title("图34：资产比较策略净值")
    ax.set_ylabel("净值")
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    ax.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig34_asset_comparison_nav.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10.5, 5.0))
    x = np.arange(len(yearly))
    width = 0.36
    ax.bar(x - width / 2, yearly["strategy_return"], width=width, label="策略", color="#D62828", alpha=0.86)
    ax.bar(x + width / 2, yearly["benchmark_return"], width=width, label="基准", color="#457B9D", alpha=0.78)
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(yearly["year"].astype(str), rotation=45)
    ax.set_title("图35：资产比较策略历年表现")
    ax.set_ylabel("年度收益")
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    ax.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig35_asset_comparison_annual_returns.png", dpi=200)
    plt.close(fig)

    allocation = states.groupby(["month_end", "asset_name"]).size().unstack(fill_value=0)
    fig, ax = plt.subplots(figsize=(10.5, 3.8))
    allocation.plot.area(ax=ax, linewidth=0, alpha=0.88)
    ax.set_title("资产比较策略月度资产类别")
    ax.set_ylabel("决策状态")
    ax.set_xlabel("月份")
    ax.legend(loc="upper left", frameon=False, ncol=3)
    ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(figure_dir / "asset_decision_allocation_timeline.png", dpi=200)
    plt.close(fig)


def run_backtest(
    config: BacktestConfig,
    overwrite: bool = False,
    industry_output_level: int = INDUSTRY_OUTPUT_LEVEL,
) -> pd.DataFrame:
    """执行资产比较决策树和月频回测。"""

    if not SIGNAL_HOLDINGS.exists():
        raise FileNotFoundError(f"缺少策略信号持仓，请先运行 python -m src.strategy：{SIGNAL_HOLDINGS}")
    if not SIGNAL_PANEL.exists():
        raise FileNotFoundError(f"缺少策略信号面板，请先运行 python -m src.strategy：{SIGNAL_PANEL}")

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("figures").mkdir(parents=True, exist_ok=True)
    holdings = pd.read_parquet(SIGNAL_HOLDINGS)
    signal_panel = pd.read_parquet(SIGNAL_PANEL)
    states, positions, monthly = run_decision_tree(holdings, signal_panel, config)
    daily = build_daily_returns(positions, states, monthly, config)
    positions = map_to_display(positions, industry_output_level)
    yearly = annual_returns(monthly)
    metrics = performance_metrics(monthly)

    states.to_parquet(DECISION_PANEL_OUTPUT, index=False)
    positions.to_parquet(MONTHLY_POSITIONS_OUTPUT, index=False)
    monthly.to_parquet(MONTHLY_RETURNS_OUTPUT, index=False)
    daily.to_parquet(DAILY_RETURNS_OUTPUT, index=False)
    yearly.to_csv(ANNUAL_RETURNS_OUTPUT, index=False, encoding="utf-8-sig")
    metrics.to_csv(METRICS_OUTPUT, index=False, encoding="utf-8-sig")

    plot_decision_tree()
    plot_backtest(monthly, yearly, states)
    # SKIP_FIG_CLEANUP=1 时保留中间回测图（供快照刷新链路避免删除触发沙箱确认）
    if not os.environ.get("SKIP_FIG_CLEANUP"):
        OUTPUT_DIR.joinpath("figures", "asset_decision_allocation_timeline.png").unlink(missing_ok=True)
    return monthly


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复现资产比较决策树与月频回测")
    parser.add_argument("--start-month", default=BACKTEST_START_DATE)
    parser.add_argument("--end-month", default=END_DATE)
    parser.add_argument("--industry-output-level", type=int, choices=[2, 3], default=INDUSTRY_OUTPUT_LEVEL)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--transaction-cost", type=float, default=TRANSACTION_COST)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = BacktestConfig(
        start_month=args.start_month,
        end_month=args.end_month,
        top_n=args.top_n,
        transaction_cost=args.transaction_cost,
    )
    monthly = run_backtest(
        config,
        overwrite=args.overwrite,
        industry_output_level=args.industry_output_level,
    )
    LOGGER.info("资产比较回测月份：%s", len(monthly))
    LOGGER.info("输出：%s", MONTHLY_RETURNS_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
