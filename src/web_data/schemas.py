"""API 响应 Schema（Pydantic）。

比率字段统一使用小数，格式化交给前端。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- health / meta
class FileStatus(BaseModel):
    key: str
    exists: bool
    mtime: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    system_time: str
    latest_model_month: Optional[str] = None
    files: list[FileStatus]


class StrategyMeta(BaseModel):
    id: str
    name: str


class AssetTypeMeta(BaseModel):
    code: str
    name: str


class MetaResponse(BaseModel):
    months: list[str]
    strategies: list[StrategyMeta]
    asset_types: list[AssetTypeMeta]
    lifecycles: list[str]
    levels: list[int]
    latest_month: Optional[str] = None
    panel_latest_month: dict[str, Optional[str]]
    data_warnings: list[str]


# ---------------------------------------------------------------- overview
class RecommendationItem(BaseModel):
    month: str
    level: int
    mode: Literal["detail", "aggregate"]
    strategy_id: str
    strategy_name: str
    rank: int
    display_industry_code: str
    display_industry_name: str
    industry_old_code: Optional[str] = None
    industry_code: Optional[str] = None
    industry_name: Optional[str] = None
    lifecycle_stage_zh: Optional[str] = None
    score: Optional[float] = None
    weight: Optional[float] = None
    next_month_log_return: Optional[float] = None
    data_available: bool = True


class TrendIndicators(BaseModel):
    actual_growth_available: bool = False
    expected_growth_available: bool = False
    roe_available: bool = False
    actual_growth_trend: Optional[float] = None
    expected_growth_trend: Optional[float] = None
    roe_trend: Optional[float] = None
    roe_crowding_high: Optional[bool] = None


class ReturnSnapshot(BaseModel):
    latest_settled_month: Optional[str] = None
    strategy_nav: Optional[float] = None
    benchmark_nav: Optional[float] = None
    return_1m: Optional[float] = None
    return_3m: Optional[float] = None
    return_6m: Optional[float] = None
    return_12m: Optional[float] = None


class AssetHistoryPoint(BaseModel):
    month: str
    asset_type: str
    asset_name: str
    selected: bool


class OverviewResponse(BaseModel):
    as_of_date: str
    asset_type: Optional[str] = None
    asset_name: Optional[str] = None
    decision_reason: Optional[str] = None
    decision_score: Optional[float] = None
    holding_count: Optional[int] = None
    trends: TrendIndicators = Field(default_factory=TrendIndicators)
    recommendations: list[RecommendationItem] = Field(default_factory=list)
    performance: Optional[ReturnSnapshot] = None
    asset_history: list[AssetHistoryPoint] = Field(default_factory=list)
    data_warnings: list[str] = Field(default_factory=list)
    has_valid_signal: bool = True
    note: Optional[str] = None


# ---------------------------------------------------------------- recommendations
class RecommendationsResponse(BaseModel):
    month: str
    level: int
    mode: Literal["detail", "aggregate"]
    count: int
    items: list[RecommendationItem]


# ---------------------------------------------------------------- decision history
class BranchAvailability(BaseModel):
    actual_growth_available: bool
    expected_growth_available: bool
    roe_available: bool
    roe_crowding_high: Optional[bool] = None


class BranchCounts(BaseModel):
    actual_growth_count: Optional[int] = None
    expected_growth_count: Optional[int] = None
    roe_asset_count: Optional[int] = None
    quality_dividend_count: Optional[int] = None
    value_dividend_count: Optional[int] = None
    selected_count: Optional[int] = None


class DecisionHistoryPoint(BaseModel):
    month: str
    asset_type: str
    asset_name: str
    reason: Optional[str] = None
    decision_score: Optional[float] = None
    branches: BranchAvailability
    counts: BranchCounts
    actual_growth_trend: Optional[float] = None
    expected_growth_trend: Optional[float] = None
    roe_trend: Optional[float] = None


class DecisionHistoryResponse(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    count: int
    items: list[DecisionHistoryPoint]


# ---------------------------------------------------------------- holdings
class HoldingItem(BaseModel):
    month: str
    asset_type: str
    asset_name: str
    rank: int
    industry_old_code: str
    industry_code: Optional[str] = None
    industry_name: Optional[str] = None
    display_industry_code: str
    display_industry_name: str
    weight: Optional[float] = None
    score: Optional[float] = None
    source_count: Optional[int] = None
    next_month_log_return: Optional[float] = None


class HoldingsResponse(BaseModel):
    month: str
    level: int
    asset_type: Optional[str] = None
    asset_name: Optional[str] = None
    count: int
    items: list[HoldingItem]


# ---------------------------------------------------------------- performance
class PerformanceMetric(BaseModel):
    key: str
    label: str
    value: Optional[float] = None
    benchmark_value: Optional[float] = None
    description: str = ""


class MonthlyReturnPoint(BaseModel):
    month: str
    strategy_return: Optional[float] = None
    benchmark_return: Optional[float] = None
    strategy_nav: Optional[float] = None
    benchmark_nav: Optional[float] = None
    drawdown: Optional[float] = None
    benchmark_drawdown: Optional[float] = None
    turnover: Optional[float] = None
    asset_type: Optional[str] = None
    asset_name: Optional[str] = None
    valid: bool = True


class DailyReturnPoint(MonthlyReturnPoint):
    """日频连续绩效点；month 字段在此表示交易日。"""

    pass


class AnnualReturnPoint(BaseModel):
    year: int
    strategy_return: Optional[float] = None
    benchmark_return: Optional[float] = None
    excess_return: Optional[float] = None
    month_count: Optional[int] = None


class SubStrategyPoint(BaseModel):
    strategy_id: str
    strategy_name: str
    month: str
    portfolio_return: Optional[float] = None
    nav: Optional[float] = None
    valid: bool = True


class PerformanceResponse(BaseModel):
    scope: Literal["final", "sub_strategy"]
    strategy_id: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    metrics: list[PerformanceMetric] = Field(default_factory=list)
    monthly: list[MonthlyReturnPoint] = Field(default_factory=list)
    daily: list[DailyReturnPoint] = Field(default_factory=list)
    annual: list[AnnualReturnPoint] = Field(default_factory=list)
    sub_strategies: list[SubStrategyPoint] = Field(default_factory=list)
    note: Optional[str] = None


# ---------------------------------------------------------------- industry detail
class IndustryIndicatorPoint(BaseModel):
    month: str
    growth_g: Optional[float] = None
    roe_ttm: Optional[float] = None
    dividend_yield: Optional[float] = None
    pb: Optional[float] = None
    pe_ttm: Optional[float] = None
    lifecycle_stage_zh: Optional[str] = None
    stock_count: Optional[int] = None


class RecommendationRecord(BaseModel):
    month: str
    strategy_id: str
    strategy_name: str
    rank: int
    score: Optional[float] = None
    next_month_log_return: Optional[float] = None


class HoldingRecord(BaseModel):
    month: str
    asset_type: str
    asset_name: str
    rank: int
    weight: Optional[float] = None
    score: Optional[float] = None
    next_month_log_return: Optional[float] = None


class IndustryChild(BaseModel):
    industry_old_code: str
    industry_code: str
    industry_name: str
    latest_lifecycle: Optional[str] = None
    latest_growth_g: Optional[float] = None
    latest_roe_ttm: Optional[float] = None
    latest_dividend_yield: Optional[float] = None


class IndustryDetailResponse(BaseModel):
    code: str
    name: str
    level: int
    is_aggregate: bool
    latest_month: Optional[str] = None
    latest_lifecycle: Optional[str] = None
    latest_score: Optional[float] = None
    lifecycle_history: list[IndustryIndicatorPoint] = Field(default_factory=list)
    indicator_history: list[IndustryIndicatorPoint] = Field(default_factory=list)
    recommendation_history: list[RecommendationRecord] = Field(default_factory=list)
    recommendation_count: Optional[int] = None
    holdings_history: list[HoldingRecord] = Field(default_factory=list)
    children: list[IndustryChild] = Field(default_factory=list)
    data_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- data status
class DatasetStatus(BaseModel):
    key: str
    label: str
    exists: bool
    status: Literal["fresh", "stale", "missing", "unknown"] = "unknown"
    rows: Optional[int] = None
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    entities: Optional[int] = None
    partitions_total: Optional[int] = None
    partitions_empty: Optional[int] = None
    duplicate_keys: Optional[int] = None
    null_key_rows: Optional[int] = None
    missing_columns: Optional[str] = None
    file_mtime: Optional[str] = None
    latest_month: Optional[str] = None
    message: Optional[str] = None


class DataStatusResponse(BaseModel):
    generated_at: str
    latest_model_month: Optional[str] = None
    market_data_date: Optional[str] = None
    fundamental_data_date: Optional[str] = None
    datasets: list[DatasetStatus]
    warnings: list[str]
    model_pipeline: list[str]
