"""服务层：把数据仓库的原始 DataFrame 转换为业务响应。

所有结论文案均由既有状态和数值通过确定性模板生成，不发明模型结论。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import config, repositories as repo
from .schemas import (
    AnnualReturnPoint,
    AssetHistoryPoint,
    AssetTypeMeta,
    BranchAvailability,
    BranchCounts,
    DatasetStatus,
    DecisionHistoryPoint,
    DecisionHistoryResponse,
    DailyReturnPoint,
    FileStatus,
    HoldingRecord,
    IndustryChild,
    IndustryDetailResponse,
    IndustryIndicatorPoint,
    MetaResponse,
    MonthlyReturnPoint,
    PerformanceMetric,
    PerformanceResponse,
    RecommendationItem,
    RecommendationRecord,
    RecommendationsResponse,
    ReturnSnapshot,
    StrategyMeta,
    SubStrategyPoint,
    TrendIndicators,
)

# 子策略 → 资产类型（展示分类，不改变任何模型口径）
STRATEGY_TO_ASSET: dict[str, str] = {
    "consensus_growth": "main_assets",
    "event_composite": "main_assets",
    "growth_advantage": "main_assets",
    "pe_g_cheap": "main_assets",
    "roe_asset": "main_assets",
    "quality_dividend": "quality_dividend",
    "value_dividend": "value_dividend",
}

# 资产类型 → 中文名
ASSET_TYPE_NAMES = {
    "main_assets": "主流资产",
    "quality_dividend": "质量红利",
    "value_dividend": "价值红利",
    "cash": "现金",
}


def _fmt_month(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _month_list(frame: pd.DataFrame) -> list[str]:
    return [_fmt_month(v) for v in sorted(frame["month_end"].dropna().unique())]


def _latest_month_of(frame: pd.DataFrame) -> Optional[str]:
    if frame is None or frame.empty or "month_end" not in frame.columns:
        return None
    return _fmt_month(frame["month_end"].max())


# ------------------------------------------------------------------ health / meta
def get_health() -> dict[str, Any]:
    files: list[FileStatus] = []
    for key in [*config.PARQUET_FILES, *config.CSV_FILES]:
        mtime = repo.file_mtime(key)
        files.append(
            FileStatus(
                key=key,
                exists=mtime > 0,
                mtime=dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc).isoformat()
                if mtime > 0
                else None,
            )
        )
    latest = get_latest_model_month()
    all_exist = all(repo.file_exists(k) for k in config.PARQUET_FILES)
    return {
        "status": "ok" if all_exist else "degraded",
        "system_time": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "latest_model_month": latest,
        "files": files,
    }


def get_latest_model_month() -> Optional[str]:
    try:
        frame = repo.load_frame("asset_decision_tree_panel")
        return _latest_month_of(frame)
    except repo.DataFileError:
        return None


def get_meta() -> MetaResponse:
    months: list[str] = []
    strategies: list[StrategyMeta] = []
    asset_types: list[AssetTypeMeta] = []
    latest: Optional[str] = None
    panel_latest: dict[str, Optional[str]] = {}

    try:
        decision = repo.load_frame("asset_decision_tree_panel")
        months = _month_list(decision)
        latest = _latest_month_of(decision)
    except repo.DataFileError:
        pass

    try:
        returns = repo.load_frame("strategy_signal_returns")
        strategies = [
            StrategyMeta(id=str(i), name=str(n))
            for i, n in returns.groupby("strategy_id")["strategy_name"].first().items()
        ]
    except repo.DataFileError:
        pass

    asset_types = [
        AssetTypeMeta(code=code, name=name) for code, name in ASSET_TYPE_NAMES.items()
    ]

    for key in config.PARQUET_FILES:
        try:
            frame = repo.load_frame(key)
            panel_latest[key] = _latest_month_of(frame)
        except repo.DataFileError:
            panel_latest[key] = None

    return MetaResponse(
        months=months,
        strategies=strategies,
        asset_types=asset_types,
        lifecycles=list(config.LIFECYCLE_STAGES),
        levels=list(config.LEVELS),
        latest_month=latest,
        panel_latest_month=panel_latest,
        data_warnings=_dynamic_warnings(),
    )


# ------------------------------------------------------------------ overview
def _period_return(nav_series: pd.Series, months: int) -> Optional[float]:
    """按净值序列计算最近 N 个月收益（最后有效值对 N 期前值的比值 - 1）。"""

    if len(nav_series) <= months:
        return None
    last = nav_series.iloc[-1]
    prev = nav_series.iloc[-1 - months]
    if pd.isna(last) or pd.isna(prev) or prev == 0:
        return None
    return float(last / prev - 1)


def get_overview() -> dict[str, Any]:
    warnings = _dynamic_warnings()
    as_of_date = get_latest_model_month() or ""

    decision: Optional[pd.Series] = None
    try:
        dtree = repo.load_frame("asset_decision_tree_panel")
        if not dtree.empty:
            decision = dtree.iloc[-1]
            as_of_date = _fmt_month(decision["month_end"])
    except repo.DataFileError:
        decision = None

    has_valid_signal = decision is not None and str(decision.get("asset_type")) != "cash"
    note: Optional[str] = None
    if decision is None:
        note = "决策树面板不可用，无法生成当月研判。"
    elif not has_valid_signal:
        note = "当月无有效信号，资产类别为现金。"

    trends = TrendIndicators()
    holding_count: Optional[int] = None
    asset_type: Optional[str] = None
    asset_name: Optional[str] = None
    if decision is not None:
        asset_type = str(decision.get("asset_type"))
        asset_name = str(decision.get("asset_name")) if pd.notna(decision.get("asset_name")) else None
        trends = TrendIndicators(
            actual_growth_available=bool(decision.get("actual_growth_available")),
            expected_growth_available=bool(decision.get("expected_growth_available")),
            roe_available=bool(decision.get("roe_available")),
            actual_growth_trend=_num(decision.get("actual_growth_trend")),
            expected_growth_trend=_num(decision.get("expected_growth_trend")),
            roe_trend=_num(decision.get("roe_trend")),
            roe_crowding_high=_bool_or_none(decision.get("roe_crowding_high")),
        )
        holding_count = _int_or_none(decision.get("selected_count"))

    # 推荐摘要：总览需展示五类风格各自的 Top 5，不能在跨策略排序后截断，
    # 否则字典序靠后的盈利能力、质量红利和价值红利会被误显示为“无数据”。
    recommendations: list[RecommendationItem] = []
    try:
        rec = _build_recommendations(
            month=None, level=2, strategy=None, asset_type=None, limit=35, mode="detail"
        )
        recommendations = rec["items"]
    except repo.DataFileError:
        recommendations = []

    # 绩效快照
    performance: Optional[ReturnSnapshot] = None
    try:
        bt = repo.load_frame("backtest_monthly_returns").sort_values("month_end")
        settled = bt[bt["valid_return"].notna()]
        if not settled.empty:
            nav = settled["strategy_nav"].astype(float)
            bnav = settled["benchmark_nav"].astype(float)
            performance = ReturnSnapshot(
                latest_settled_month=_fmt_month(settled["month_end"].iloc[-1]),
                strategy_nav=_num(nav.iloc[-1]),
                benchmark_nav=_num(bnav.iloc[-1]),
                return_1m=_period_return(nav, 1),
                return_3m=_period_return(nav, 3),
                return_6m=_period_return(nav, 6),
                return_12m=_period_return(nav, 12),
            )
    except repo.DataFileError:
        performance = None

    # 近 12 月资产类别时间线
    asset_history: list[AssetHistoryPoint] = []
    try:
        dtree = repo.load_frame("asset_decision_tree_panel").sort_values("month_end")
        for _, row in dtree.tail(12).iterrows():
            asset_history.append(
                AssetHistoryPoint(
                    month=_fmt_month(row["month_end"]),
                    asset_type=str(row["asset_type"]),
                    asset_name=str(row["asset_name"]) if pd.notna(row.get("asset_name")) else "",
                    selected=row["month_end"] == dtree["month_end"].max(),
                )
            )
    except repo.DataFileError:
        asset_history = []

    return {
        "as_of_date": as_of_date,
        "asset_type": asset_type,
        "asset_name": asset_name,
        "decision_reason": str(decision.get("reason")) if decision is not None and pd.notna(decision.get("reason")) else None,
        "decision_score": _num(decision.get("decision_score")) if decision is not None else None,
        "holding_count": holding_count,
        "trends": trends,
        "recommendations": recommendations,
        "performance": performance,
        "asset_history": asset_history,
        "data_warnings": warnings,
        "has_valid_signal": has_valid_signal,
        "note": note,
    }


# ------------------------------------------------------------------ recommendations
def _resolve_level(value: Optional[int]) -> int:
    level = 2 if value is None else int(value)
    if level not in config.LEVELS:
        raise ValueError(f"展示层级只能是 {config.LEVELS}，收到 {level}")
    return level


def _build_recommendations(
    month: Optional[str],
    level: int,
    strategy: Optional[str],
    asset_type: Optional[str],
    limit: int,
    mode: str,
) -> dict[str, Any]:
    if mode not in ("detail", "aggregate"):
        raise ValueError("mode 只能是 detail 或 aggregate")
    limit = min(max(int(limit), 1), 200)

    holdings = repo.load_frame("strategy_signal_holdings")
    if holdings.empty:
        return {"month": month or "", "level": level, "mode": mode, "count": 0, "items": []}

    target = _fmt_month(pd.Timestamp(month)) if month else _latest_month_of(holdings)
    latest = _latest_month_of(holdings)

    frame = holdings[holdings["month_end"].astype("datetime64[ns]") == pd.Timestamp(target)].copy()
    if frame.empty:
        return {"month": target, "level": level, "mode": mode, "count": 0, "items": []}

    if strategy:
        frame = frame[frame["strategy_id"] == strategy]
    if asset_type:
        wanted = {
            sid for sid, at in STRATEGY_TO_ASSET.items() if at == asset_type
        }
        frame = frame[frame["strategy_id"].isin(wanted)]

    # 展示层级派生：三级时重新映射展示列（二级直接用面板既有列）
    if level == 3:
        frame = _map_display(frame, level)
    else:
        frame = frame.rename(
            columns={
                "display_industry_code": "display_industry_code",
                "display_industry_name": "display_industry_name",
            }
        )

    frame = frame.sort_values(["strategy_id", "rank"])

    if mode == "aggregate":
        frame = _aggregate_recommendations(frame, level)

    items: list[RecommendationItem] = []
    for _, row in frame.head(limit).iterrows():
        is_latest = target == latest
        next_ret = None if is_latest else _num(row.get("next_month_log_return"))
        items.append(
            RecommendationItem(
                month=target,
                level=level,
                mode=mode,
                strategy_id=str(row["strategy_id"]),
                strategy_name=str(row["strategy_name"]),
                rank=int(row["rank"]) if pd.notna(row.get("rank")) else 0,
                display_industry_code=str(row.get("display_industry_code") or ""),
                display_industry_name=str(row.get("display_industry_name") or ""),
                industry_old_code=str(row.get("industry_old_code")) if pd.notna(row.get("industry_old_code")) else None,
                industry_code=str(row.get("industry_code")) if pd.notna(row.get("industry_code")) else None,
                industry_name=str(row.get("industry_name")) if pd.notna(row.get("industry_name")) else None,
                lifecycle_stage_zh=str(row.get("lifecycle_stage_zh")) if pd.notna(row.get("lifecycle_stage_zh")) else None,
                score=_num(row.get("score")),
                weight=_num(row.get("weight")),
                next_month_log_return=next_ret,
                data_available=pd.notna(row.get("score")),
            )
        )
    return {"month": target, "level": level, "mode": mode, "count": len(items), "items": items}


def _aggregate_recommendations(frame: pd.DataFrame, level: int) -> pd.DataFrame:
    """按展示行业聚合：权重求和、得分加权（无权重时等权），内部行业保留为映射计数。"""

    has_weight = "weight" in frame.columns and frame["weight"].notna().any()
    frame = frame.copy()
    frame["_w"] = frame["weight"].fillna(0.0).astype(float) if has_weight else 0.0

    def agg_block(group: pd.DataFrame) -> pd.Series:
        scores = group["score"].dropna()
        weights = group["_w"].reindex(scores.index).fillna(0.0)
        if scores.empty:
            avg_score = float("nan")
        elif weights.sum() > 0:
            avg_score = float((scores.astype(float) * weights).sum() / weights.sum())
        else:
            avg_score = float(scores.astype(float).mean())
        lifecycle = group["lifecycle_stage_zh"].dropna()
        return pd.Series(
            {
                "rank": int(group["rank"].min()),
                "lifecycle_stage_zh": str(lifecycle.mode().iloc[0]) if not lifecycle.empty else None,
                "score": avg_score,
                "weight": float(group["_w"].sum()) if has_weight else None,
                "_hybrid_count": int(group["industry_old_code"].nunique()),
            }
        )

    group_cols = ["strategy_id", "strategy_name", "display_industry_code", "display_industry_name"]
    aggregated = frame.groupby(group_cols, as_index=False).apply(agg_block, include_groups=False)
    aggregated = aggregated.sort_values(["strategy_id", "score"], ascending=[True, False])
    aggregated["rank"] = aggregated.groupby("strategy_id").cumcount() + 1
    return aggregated.reset_index(drop=True)


def _map_display(frame: pd.DataFrame, level: int) -> pd.DataFrame:
    """复用模型侧的展示映射，仅派生展示列，不修改原行业键。"""

    from src.output_industry_map import map_to_display

    return map_to_display(frame, level)


def get_recommendations(
    month: Optional[str] = None,
    level: Optional[int] = None,
    strategy: Optional[str] = None,
    asset_type: Optional[str] = None,
    limit: int = 50,
    mode: str = "detail",
) -> RecommendationsResponse:
    level = _resolve_level(level)
    month = repo.parse_month(month)
    payload = _build_recommendations(month, level, strategy, asset_type, limit, mode)
    return RecommendationsResponse(**payload)


# ------------------------------------------------------------------ decision history
def get_decision_history(start: Optional[str], end: Optional[str]) -> DecisionHistoryResponse:
    dtree = repo.load_frame("asset_decision_tree_panel").sort_values("month_end")
    if dtree.empty:
        return DecisionHistoryResponse(start=start, end=end, count=0, items=[])
    if start:
        dtree = dtree[pd.to_datetime(dtree["month_end"]) >= pd.to_datetime(repo.parse_month(start))]
    if end:
        dtree = dtree[pd.to_datetime(dtree["month_end"]) <= pd.to_datetime(repo.parse_month(end))]
    dtree = dtree.sort_values("month_end")

    items: list[DecisionHistoryPoint] = []
    for _, row in dtree.iterrows():
        items.append(
            DecisionHistoryPoint(
                month=_fmt_month(row["month_end"]),
                asset_type=str(row["asset_type"]),
                asset_name=str(row["asset_name"]) if pd.notna(row.get("asset_name")) else "",
                reason=str(row["reason"]) if pd.notna(row.get("reason")) else None,
                decision_score=_num(row.get("decision_score")),
                branches=BranchAvailability(
                    actual_growth_available=bool(row.get("actual_growth_available")),
                    expected_growth_available=bool(row.get("expected_growth_available")),
                    roe_available=bool(row.get("roe_available")),
                    roe_crowding_high=_bool_or_none(row.get("roe_crowding_high")),
                ),
                counts=BranchCounts(
                    actual_growth_count=_int_or_none(row.get("actual_growth_count")),
                    expected_growth_count=_int_or_none(row.get("expected_growth_count")),
                    roe_asset_count=_int_or_none(row.get("roe_asset_count")),
                    quality_dividend_count=_int_or_none(row.get("quality_dividend_count")),
                    value_dividend_count=_int_or_none(row.get("value_dividend_count")),
                    selected_count=_int_or_none(row.get("selected_count")),
                ),
                actual_growth_trend=_num(row.get("actual_growth_trend")),
                expected_growth_trend=_num(row.get("expected_growth_trend")),
                roe_trend=_num(row.get("roe_trend")),
            )
        )
    return DecisionHistoryResponse(start=start, end=end, count=len(items), items=items)


# ------------------------------------------------------------------ holdings
def get_holdings(month: Optional[str], level: Optional[int]) -> dict[str, Any]:
    """返回最终策略在指定月份的行业持仓（默认最新月份）。"""

    level = _resolve_level(level)
    positions = repo.load_frame("backtest_monthly_positions")
    if positions.empty:
        return {"month": month or "", "level": level, "asset_type": None, "asset_name": None, "count": 0, "items": []}

    target = _fmt_month(pd.Timestamp(month)) if month else _latest_month_of(positions)
    frame = positions[positions["month_end"].astype("datetime64[ns]") == pd.Timestamp(target)].copy()
    if frame.empty:
        return {"month": target, "level": level, "asset_type": None, "asset_name": None, "count": 0, "items": []}

    if level == 3:
        frame = _map_display(frame, level)

    latest = _latest_month_of(positions)
    frame = frame.sort_values("rank")
    items = []
    for _, row in frame.iterrows():
        is_latest = target == latest
        items.append(
            {
                "month": target,
                "level": level,
                "asset_type": str(row["asset_type"]) if pd.notna(row.get("asset_type")) else None,
                "asset_name": str(row["asset_name"]) if pd.notna(row.get("asset_name")) else None,
                "rank": int(row["rank"]) if pd.notna(row.get("rank")) else 0,
                "industry_old_code": str(row["industry_old_code"]),
                "industry_code": str(row["industry_code"]) if pd.notna(row.get("industry_code")) else None,
                "industry_name": str(row["industry_name"]) if pd.notna(row.get("industry_name")) else None,
                "display_industry_code": str(row.get("display_industry_code") or ""),
                "display_industry_name": str(row.get("display_industry_name") or ""),
                "weight": _num(row.get("weight")),
                "score": _num(row.get("score")),
                "source_count": _int_or_none(row.get("source_count")),
                "next_month_log_return": None if is_latest else _num(row.get("next_month_log_return")),
            }
        )
    asset_type = items[0]["asset_type"] if items else None
    asset_name = items[0]["asset_name"] if items else None
    return {"month": target, "level": level, "asset_type": asset_type, "asset_name": asset_name, "count": len(items), "items": items}


# ------------------------------------------------------------------ performance
def _drawdown_series(nav: pd.Series) -> list[Optional[float]]:
    running_max = nav.cummax()
    dd = nav / running_max - 1
    return [_num(v) for v in dd]


def get_performance(
    scope: str, strategy_id: Optional[str], start: Optional[str], end: Optional[str]
) -> PerformanceResponse:
    scope = "final" if scope != "sub_strategy" else "sub_strategy"
    start = repo.parse_month(start)
    end = repo.parse_month(end)

    if scope == "final":
        return _final_performance(start, end)
    return _sub_strategy_performance(strategy_id, start, end)


def _filter_months(frame: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    result = frame.sort_values("month_end")
    if start:
        result = result[pd.to_datetime(result["month_end"]) >= pd.to_datetime(start)]
    if end:
        result = result[pd.to_datetime(result["month_end"]) <= pd.to_datetime(end)]
    return result


def _final_performance(start: Optional[str], end: Optional[str]) -> PerformanceResponse:
    bt = repo.load_frame("backtest_monthly_returns")
    daily_bt = repo.load_frame("backtest_daily_returns")
    metrics_csv = repo.load_frame("backtest_metrics")
    annual_csv = repo.load_frame("backtest_annual_returns")

    monthly_raw = _filter_months(bt, start, end)

    # 净值回撤基于全区间计算，再按区间截取展示
    full = bt.sort_values("month_end")
    full_nav = full["strategy_nav"].astype(float)
    full_bnav = full["benchmark_nav"].astype(float)
    dd_full = _drawdown_series(full_nav)
    bdd_full = _drawdown_series(full_bnav)

    monthly: list[MonthlyReturnPoint] = []
    dd_lookup = dict(zip(full["month_end"].astype("datetime64[ns]"), dd_full))
    bdd_lookup = dict(zip(full["month_end"].astype("datetime64[ns]"), bdd_full))
    for _, row in monthly_raw.iterrows():
        key = pd.Timestamp(row["month_end"])
        valid = bool(pd.notna(row.get("valid_return"))) if "valid_return" in row else True
        bench_simple = None
        if pd.notna(row.get("benchmark_log_return")):
            bench_simple = float(np.expm1(float(row["benchmark_log_return"])))
        monthly.append(
            MonthlyReturnPoint(
                month=_fmt_month(row["month_end"]),
                strategy_return=_num(row.get("net_simple_return")),
                benchmark_return=bench_simple,
                strategy_nav=_num(row.get("strategy_nav")),
                benchmark_nav=_num(row.get("benchmark_nav")),
                drawdown=dd_lookup.get(key),
                benchmark_drawdown=bdd_lookup.get(key),
                turnover=_num(row.get("turnover")),
                asset_type=str(row["asset_type"]) if pd.notna(row.get("asset_type")) else None,
                asset_name=str(row["asset_name"]) if pd.notna(row.get("asset_name")) else None,
                valid=valid,
            )
        )

    daily_raw = daily_bt.sort_values("date")
    if start:
        daily_raw = daily_raw[pd.to_datetime(daily_raw["date"]) >= pd.Timestamp(start).to_period("M").start_time]
    if end:
        daily_raw = daily_raw[pd.to_datetime(daily_raw["date"]) <= pd.Timestamp(end).to_period("M").end_time]
    daily_nav = daily_raw["strategy_nav"].astype(float)
    daily_bnav = daily_raw["benchmark_nav"].astype(float)
    daily_dd = _drawdown_series(daily_nav)
    daily_bdd = _drawdown_series(daily_bnav)
    daily = [
        DailyReturnPoint(
            month=pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
            strategy_return=_num(row.get("net_simple_return")),
            benchmark_return=_num(np.expm1(row["benchmark_log_return"])),
            strategy_nav=_num(row.get("strategy_nav")),
            benchmark_nav=_num(row.get("benchmark_nav")),
            drawdown=daily_dd[i],
            benchmark_drawdown=daily_bdd[i],
            turnover=_num(row.get("turnover")),
            asset_type=str(row["asset_type"]) if pd.notna(row.get("asset_type")) else None,
            asset_name=str(row["asset_name"]) if pd.notna(row.get("asset_name")) else None,
            valid=bool(row.get("valid_return", True)),
        )
        for i, (_, row) in enumerate(daily_raw.iterrows())
    ]

    annual: list[AnnualReturnPoint] = []
    for _, row in annual_csv.iterrows():
        annual.append(
            AnnualReturnPoint(
                year=int(row["year"]),
                strategy_return=_num(row.get("strategy_return")),
                benchmark_return=_num(row.get("benchmark_return")),
                excess_return=_num(row.get("excess_return")),
                month_count=_int_or_none(row.get("month_count")),
            )
        )

    metrics: list[PerformanceMetric] = []
    if not metrics_csv.empty:
        m = metrics_csv.iloc[0]
        metrics = [
            PerformanceMetric(key="annual_return", label="年化收益", value=_num(m.get("annual_return")), benchmark_value=_num(m.get("benchmark_annual_return")), description="策略与基准的年化收益（含交易成本）"),
            PerformanceMetric(key="annual_volatility", label="年化波动", value=_num(m.get("annual_volatility")), benchmark_value=_num(m.get("benchmark_annual_volatility")), description="月度收益的年化波动率"),
            PerformanceMetric(key="max_drawdown", label="最大回撤", value=_num(m.get("max_drawdown")), benchmark_value=_num(m.get("benchmark_max_drawdown")), description="历史最大回撤（负数表示跌幅）"),
            PerformanceMetric(key="sharpe_like", label="Sharpe 类指标", value=_num(m.get("sharpe_like")), description="年化超额收益/年化波动（近似夏普，非无风险利率口径）"),
            PerformanceMetric(key="calmar_like", label="Calmar 类指标", value=_num(m.get("calmar_like")), description="年化收益/最大回撤绝对值（近似卡玛）"),
            PerformanceMetric(key="win_rate", label="月度胜率", value=_num(m.get("win_rate")), description="月度正收益月份占比"),
            PerformanceMetric(key="avg_turnover", label="平均月度换手", value=_num(m.get("avg_turnover")), description="平均月度组合换手率"),
            PerformanceMetric(key="total_transaction_cost", label="累计交易成本", value=_num(m.get("total_transaction_cost")), description="回测区间累计交易成本（收益损耗）"),
            PerformanceMetric(key="months", label="回测月数", value=_num(m.get("months")), description="参与回测的月份数"),
        ]

    return PerformanceResponse(
        scope="final",
        start=start,
        end=end,
        metrics=metrics,
        monthly=monthly,
        daily=daily,
        annual=annual,
        sub_strategies=_all_sub_strategy_series(),
    )


def _sub_strategy_performance(strategy_id: Optional[str], start: Optional[str], end: Optional[str]) -> PerformanceResponse:
    returns = repo.load_frame("strategy_signal_returns")
    summary = repo.load_frame("strategy_signal_summary")

    if strategy_id:
        frame = returns[returns["strategy_id"] == strategy_id]
        if frame.empty:
            raise ValueError(f"未知子策略：{strategy_id}")
    else:
        frame = returns

    frame = _filter_months(frame, start, end)

    monthly: list[MonthlyReturnPoint] = []
    for _, row in frame.iterrows():
        monthly.append(
            MonthlyReturnPoint(
                month=_fmt_month(row["month_end"]),
                strategy_return=_num(row.get("portfolio_simple_return")),
                strategy_nav=_num(row.get("nav")),
                valid=bool(pd.notna(row.get("valid_return"))),
            )
        )

    metrics: list[PerformanceMetric] = []
    if strategy_id and not summary.empty:
        row = summary[summary["strategy_id"] == strategy_id]
        if not row.empty:
            row = row.iloc[0]
            name = str(row["strategy_name"])
            last_nav = _num(row.get("last_nav"))
            metrics = [
                PerformanceMetric(key="months", label="回测月数", value=_int_or_none(row.get("months")), description="该子策略参与回测的月份数"),
                PerformanceMetric(key="total_return", label="累计收益", value=_num(row.get("total_return")), description="全区间累计收益"),
                PerformanceMetric(key="annual_return", label="年化收益", value=_num(row.get("annual_return")), description="年化收益（含交易成本口径随回测设置）"),
                PerformanceMetric(key="annual_volatility", label="年化波动", value=_num(row.get("annual_volatility")), description="月度收益年化波动率"),
                PerformanceMetric(key="win_rate", label="月度胜率", value=_num(row.get("win_rate")), description="月度正收益占比"),
                PerformanceMetric(key="last_nav", label="最新净值", value=last_nav, description="截至最新已结算月份的累计净值"),
            ]

    return PerformanceResponse(
        scope="sub_strategy",
        strategy_id=strategy_id,
        start=start,
        end=end,
        metrics=metrics,
        monthly=monthly,
        daily=[],
        annual=[],
        sub_strategies=_all_sub_strategy_series(),
        note="子策略无独立年度收益表，如需年度收益请查看最终策略。",
    )


def _all_sub_strategy_series() -> list[SubStrategyPoint]:
    """返回全部子策略的完整净值序列，供前端绘制对比图。"""

    try:
        returns = repo.load_frame("strategy_signal_returns")
    except repo.DataFileError:
        return []
    result: list[SubStrategyPoint] = []
    for _, row in returns.iterrows():
        result.append(
            SubStrategyPoint(
                strategy_id=str(row["strategy_id"]),
                strategy_name=str(row["strategy_name"]),
                month=_fmt_month(row["month_end"]),
                portfolio_return=_num(row.get("portfolio_simple_return")),
                nav=_num(row.get("nav")),
                valid=bool(pd.notna(row.get("valid_return"))),
            )
        )
    return result


# ------------------------------------------------------------------ industry detail
def _display_to_hybrids(panel: pd.DataFrame, code: str, level: int) -> pd.DataFrame:
    """根据展示代码返回匹配的 hybrid 行业行集合。"""

    if level == 3:
        return panel[panel["industry_old_code"].astype(str) == code]
    return panel[panel["display_industry_code"].astype(str) == code]


def get_industry_detail(code: str, level: Optional[int] = None) -> IndustryDetailResponse:
    level = _resolve_level(level)
    panel = repo.load_frame("strategy_signal_panel")
    holdings = repo.load_frame("strategy_signal_holdings")
    positions = repo.load_frame("backtest_monthly_positions")

    # 展示名称
    name = ""
    try:
        if level == 3:
            row = panel[panel["industry_old_code"].astype(str) == code]
            if not row.empty:
                name = str(row["industry_name"].iloc[0])
        else:
            row = panel[panel["display_industry_code"].astype(str) == code]
            if not row.empty:
                name = str(row["display_industry_name"].iloc[0])
    except Exception:
        name = ""

    matched = _display_to_hybrids(panel, code, level)
    if matched.empty:
        raise ValueError(f"未找到展示行业：{code}（level={level}）")

    hybrid_codes = matched["industry_old_code"].astype(str).unique().tolist()
    is_aggregate = len(hybrid_codes) > 1
    latest_month = _fmt_month(panel["month_end"].max())

    # 指标历史：聚合时按市值加权平均，生命周期取当月众数
    history: list[IndustryIndicatorPoint] = []
    for month, group in matched.groupby("month_end"):
        group = group.sort_values("month_end")
        total_mv = group["total_mv"].astype(float) if "total_mv" in group else pd.Series(dtype=float)
        w = total_mv.fillna(0.0).clip(lower=0.0)
        wsum = w.sum()

        def wavg(col: str) -> Optional[float]:
            values = group[col].astype(float)
            mask = values.notna()
            if not mask.any():
                return None
            if wsum <= 0:
                return _num(values[mask].mean())
            ww = w[mask]
            return _num(float((values[mask] * ww).sum() / ww.sum()))

        lifecycle = group["lifecycle_stage_zh"].dropna()
        history.append(
            IndustryIndicatorPoint(
                month=_fmt_month(month),
                growth_g=wavg("growth_g"),
                roe_ttm=wavg("roe_ttm"),
                dividend_yield=wavg("dividend_yield"),
                pb=wavg("pb"),
                pe_ttm=wavg("pe_ttm"),
                lifecycle_stage_zh=str(lifecycle.mode().iloc[0]) if not lifecycle.empty else None,
                stock_count=_int_or_none(group["stock_count"].sum(skipna=True)),
            )
        )
    history.sort(key=lambda p: p.month)

    # 推荐历史（按展示代码匹配）
    if level == 3:
        rec_h = holdings[holdings["industry_old_code"].astype(str) == code].sort_values("month_end")
    else:
        rec_h = holdings[holdings["display_industry_code"].astype(str) == code].sort_values("month_end")
    latest_rec_month = get_latest_model_month()
    rec_history: list[RecommendationRecord] = []
    for _, row in rec_h.iterrows():
        is_latest = _fmt_month(row["month_end"]) == latest_rec_month
        rec_history.append(
            RecommendationRecord(
                month=_fmt_month(row["month_end"]),
                strategy_id=str(row["strategy_id"]),
                strategy_name=str(row["strategy_name"]),
                rank=int(row["rank"]),
                score=_num(row.get("score")),
                next_month_log_return=None if is_latest else _num(row.get("next_month_log_return")),
            )
        )
    recommendation_count = len(rec_history)

    # 最终持仓历史
    if level == 3:
        pos_h = positions[positions["industry_old_code"].astype(str) == code].sort_values("month_end")
    else:
        pos_h = positions[positions["display_industry_code"].astype(str) == code].sort_values("month_end")
    holdings_history: list[HoldingRecord] = []
    for _, row in pos_h.iterrows():
        is_latest = _fmt_month(row["month_end"]) == latest_rec_month
        holdings_history.append(
            HoldingRecord(
                month=_fmt_month(row["month_end"]),
                asset_type=str(row["asset_type"]),
                asset_name=str(row["asset_name"]),
                rank=int(row["rank"]),
                weight=_num(row.get("weight")),
                score=_num(row.get("score")),
                next_month_log_return=None if is_latest else _num(row.get("next_month_log_return")),
            )
        )

    # 聚合视图的 hybrid 子行业明细（取最新月）
    children: list[IndustryChild] = []
    if is_aggregate:
        latest_rows = matched[matched["month_end"] == matched["month_end"].max()]
        for _, row in latest_rows.iterrows():
            children.append(
                IndustryChild(
                    industry_old_code=str(row["industry_old_code"]),
                    industry_code=str(row["industry_code"]) if pd.notna(row.get("industry_code")) else "",
                    industry_name=str(row["industry_name"]) if pd.notna(row.get("industry_name")) else "",
                    latest_lifecycle=str(row["lifecycle_stage_zh"]) if pd.notna(row.get("lifecycle_stage_zh")) else None,
                    latest_growth_g=_num(row.get("growth_g")),
                    latest_roe_ttm=_num(row.get("roe_ttm")),
                    latest_dividend_yield=_num(row.get("dividend_yield")),
                )
            )

    latest_lifecycle: Optional[str] = None
    latest_score: Optional[float] = None
    if history:
        latest_lifecycle = history[-1].lifecycle_stage_zh
    if rec_history:
        latest_score = rec_history[-1].score

    return IndustryDetailResponse(
        code=code,
        name=name or code,
        level=level,
        is_aggregate=is_aggregate,
        latest_month=latest_month,
        latest_lifecycle=latest_lifecycle,
        latest_score=latest_score,
        lifecycle_history=history,
        indicator_history=history,
        recommendation_history=rec_history,
        recommendation_count=recommendation_count,
        holdings_history=holdings_history,
        children=children,
        data_warnings=_dynamic_warnings(),
    )


# ------------------------------------------------------------------ data status
_MARKET_DATASETS = ("stock_eod_prices", "index_eod_prices", "stock_eod_derivative")
_FUNDAMENTAL_DATASETS = (
    "income_statement",
    "balance_sheet",
    "cashflow_statement",
    "financial_indicator",
    "dividend",
    "consensus",
)


def _coverage_dates(coverage: pd.DataFrame) -> tuple[str, str]:
    """从 data_coverage.csv 提取行情类与基本面类数据集最新日期（YYYY-MM-DD）。"""

    market = ""
    fundamental = ""
    if coverage.empty:
        return market, fundamental
    ok_rows = coverage[coverage["status"] == "ok"]
    specs = (("market", _MARKET_DATASETS), ("fundamental", _FUNDAMENTAL_DATASETS))
    for label, datasets in specs:
        rows = ok_rows[ok_rows["dataset"].isin(datasets)]
        if rows.empty:
            continue
        dates = pd.to_numeric(rows["max_date"], errors="coerce").dropna()
        if dates.empty:
            continue
        m = int(dates.max())
        formatted = f"{m // 10000:04d}-{m % 10000 // 100:02d}-{m % 100:02d}"
        if label == "market":
            market = formatted
        else:
            fundamental = formatted
    return market, fundamental


def _dynamic_warnings(coverage: Optional[pd.DataFrame] = None) -> list[str]:
    """基于 data_coverage 动态生成数据滞后提示，与真实数据保持一致（无固定矛盾文案）。"""

    if coverage is None:
        try:
            coverage = repo.load_frame("data_coverage")
        except repo.DataFileError:
            return []
    market, fundamental = _coverage_dates(coverage)
    warnings: list[str] = []
    if market:
        warnings.append(f"行情数据最新日期：{market}。")
    if fundamental:
        warnings.append(f"财报/分红/指数/一致预期最新日期：{fundamental}。")
    if market and fundamental and fundamental < market:
        warnings.append(
            f"基本面数据滞后于行情（{fundamental} vs {market}），相关信号可能未反映最新基本面。"
        )
    return warnings


def get_data_status() -> dict[str, Any]:
    generated_at = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    latest = get_latest_model_month()

    # 1) processed 面板状态
    datasets: list[DatasetStatus] = []
    for key, label in config.DATASET_LABELS.items():
        if key not in config.PARQUET_FILES:
            continue
        ds = _processed_dataset_status(key, label, latest)
        datasets.append(ds)

    # 2) 原始数据集覆盖摘要（来自 data_coverage.csv）
    try:
        coverage = repo.load_frame("data_coverage")
    except repo.DataFileError:
        coverage = pd.DataFrame()

    raw_summary = _coverage_summary(coverage)
    datasets.extend(raw_summary)

    market_date, fundamental_date = _coverage_dates(coverage)
    warnings = _dynamic_warnings(coverage)

    pipeline = [
        "1. 生命周期分类（Dickinson CFO/CFI/CFF）→ industry_lifecycle_monthly",
        "2. 行业因子计算 → industry_factor_panel",
        "3. 估值与红利 → dividend_asset_panel / dividend_asset_holdings",
        "4. 资产选择信号 → strategy_signal_panel / strategy_signal_holdings / strategy_signal_returns",
        "5. 决策树与回测 → asset_decision_tree_panel / backtest_monthly_positions / backtest_monthly_returns",
    ]

    return {
        "generated_at": generated_at,
        "latest_model_month": latest,
        "market_data_date": market_date or None,
        "fundamental_data_date": fundamental_date or None,
        "datasets": datasets,
        "warnings": warnings,
        "model_pipeline": pipeline,
    }


def _processed_dataset_status(key: str, label: str, latest: Optional[str]) -> DatasetStatus:
    if not repo.file_exists(key):
        return DatasetStatus(key=key, label=label, exists=False, status="missing")

    mtime = repo.file_mtime(key)
    mtime_str = (
        dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if mtime
        else None
    )
    try:
        frame = repo.load_frame(key)
    except repo.DataFileError as exc:
        return DatasetStatus(key=key, label=label, exists=True, status="stale", message=str(exc), file_mtime=mtime_str)

    if frame.empty:
        return DatasetStatus(key=key, label=label, exists=True, status="stale", rows=0, file_mtime=mtime_str, message="面板为空")

    rows = int(len(frame))
    min_date = max_date = latest_month = None
    if "month_end" in frame.columns:
        min_date = _fmt_month(frame["month_end"].min())
        max_date = _fmt_month(frame["month_end"].max())
        latest_month = max_date

    entities = None
    if "industry_old_code" in frame.columns:
        entities = int(frame["industry_old_code"].nunique())
    elif "strategy_id" in frame.columns:
        entities = int(frame["strategy_id"].nunique())

    status = "fresh" if latest_month == latest else ("stale" if latest_month else "unknown")

    return DatasetStatus(
        key=key,
        label=label,
        exists=True,
        status=status,
        rows=rows,
        min_date=min_date,
        max_date=max_date,
        entities=entities,
        file_mtime=mtime_str,
        latest_month=latest_month,
    )


def _coverage_summary(coverage: pd.DataFrame) -> list[DatasetStatus]:
    if coverage.empty:
        return []
    result: list[DatasetStatus] = []
    groups = coverage.groupby("dataset", sort=True)
    for dataset, group in groups:
        ok = group[group["status"] == "ok"]
        empty = group[group["status"] == "empty"]
        dates = pd.to_numeric(ok["max_date"], errors="coerce").dropna()
        max_date = None
        if not dates.empty:
            m = int(dates.max())
            max_date = f"{m // 10000:04d}-{m % 10000 // 100:02d}-{m % 100:02d}"
        rows = int(ok["row_count"].sum(skipna=True)) if not ok.empty else 0
        entities = int(ok["entity_count"].max(skipna=True)) if not ok.empty and ok["entity_count"].notna().any() else None

        if empty.empty:
            status: str = "fresh"
        elif ok.empty:
            status = "missing"
        else:
            status = "stale"
        result.append(
            DatasetStatus(
                key=f"raw:{dataset}",
                label=f"原始数据·{dataset}",
                exists=True,
                status=status,
                rows=rows,
                max_date=max_date,
                entities=entities,
                partitions_total=int(len(group)),
                partitions_empty=int(len(empty)),
                message=f"空分区 {len(empty)} 个" if len(empty) else None,
            )
        )
    return result


# ------------------------------------------------------------------ helpers
def _num(value: Any) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> Optional[bool]:
    if value is None or pd.isna(value):
        return None
    return bool(value)
