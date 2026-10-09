"""
行业月度资产特征因子。

1. 将个股日频估值/市值数据压缩到月末；
2. 将财务指标按公告日做 PIT 月度对齐；
3. 按月末历史行业归属，把个股因子自由流通市值加权聚合到行业层面；
4. 合并生命周期标签，输出行业-月份的成长、盈利、分红特征。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import END_DATE, OUTPUT_DIR, PROCESSED_DATA_DIR, RAW_DATA_DIR, START_DATE
from src.lifecycle import (
    LifecycleConfig,
    attach_industry_by_month,
    month_ends,
    parse_wind_date,
    prepare_industry_map,
    read_parquet_dataset,
    statement_priority,
)


LOGGER = logging.getLogger(__name__)

INDUSTRY_FACTOR_OUTPUT = PROCESSED_DATA_DIR / "industry_factor_panel.parquet"
STOCK_MONTHLY_OUTPUT = PROCESSED_DATA_DIR / "stock_factor_monthly.parquet"
COVERAGE_OUTPUT = OUTPUT_DIR / "tables" / "industry_factor_coverage.csv"
CACHE_META_OUTPUT = PROCESSED_DATA_DIR / "industry_factor_cache_meta.json"


@dataclass(frozen=True)
class FactorConfig:
    """行业资产特征面板参数。"""

    start_date: str = START_DATE
    end_date: str = END_DATE
    min_stocks: int = 3
    industry_scheme: str = "hybrid"
    industry_level: int = 4
    min_tertiary_stocks: int = 8
    target_industries: Optional[int] = None


def config_fingerprint(config: FactorConfig) -> str:
    """返回当前参数指纹，用于判断缓存是否仍有效。"""

    payload = json.dumps(asdict(config), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_cached_fingerprint() -> Optional[str]:
    if not CACHE_META_OUTPUT.exists():
        return None
    try:
        return json.loads(CACHE_META_OUTPUT.read_text(encoding="utf-8")).get("fingerprint")
    except (json.JSONDecodeError, OSError):
        return None


def winsorize_series(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """简单分位数缩尾，降低极端财务值对行业加权均值的影响。"""

    valid = series.dropna()
    if valid.empty:
        return series
    lo = valid.quantile(lower)
    hi = valid.quantile(upper)
    return series.clip(lo, hi)


def weighted_average(values: pd.Series, weights: pd.Series) -> float:
    """计算带缺失处理的加权均值。"""

    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return float("nan")
    return float((values[mask] * weights[mask]).sum() / weights[mask].sum())


def read_stock_derivative_monthly(config: FactorConfig) -> pd.DataFrame:
    """读取日频衍生指标并压缩为个股月末因子。"""

    columns = [
        "S_INFO_WINDCODE",
        "TRADE_DT",
        "S_DQ_MV",
        "S_VAL_MV",
        "S_PRICE_DIV_DPS",
        "NET_PROFIT_PARENT_COMP_TTM",
        "NET_ASSETS_TODAY",
    ]
    df = read_parquet_dataset("stock_eod_derivative", columns=columns)
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
    price_to_dividend = pd.to_numeric(df["S_PRICE_DIV_DPS"], errors="coerce")
    df["dividend_yield"] = 100 / price_to_dividend.where(price_to_dividend > 0)
    net_profit_ttm = pd.to_numeric(df["NET_PROFIT_PARENT_COMP_TTM"], errors="coerce")
    net_assets = pd.to_numeric(df["NET_ASSETS_TODAY"], errors="coerce")
    df["roe_ttm"] = (net_profit_ttm / net_assets.replace(0, pd.NA)) * 100

    return df[
        [
            "windcode",
            "month_end",
            "trade_date",
            "float_mv",
            "total_mv",
            "roe_ttm",
            "dividend_yield",
        ]
    ].reset_index(drop=True)


def prepare_financial_indicator_monthly(config: FactorConfig, stock_monthly: pd.DataFrame) -> pd.DataFrame:
    """按公告日将财务指标对齐到个股月末。"""

    columns = [
        "S_INFO_WINDCODE",
        "WIND_CODE",
        "ANN_DT",
        "REPORT_PERIOD",
        "STATEMENT_TYPE",
        "S_FA_YOYNETPROFIT",
        "S_QFA_YOYNETPROFIT",
        "S_FA_ROE",
        "WAA_ROE",
        "OPDATE",
    ]
    financial = read_parquet_dataset("financial_indicator", columns=columns)
    financial["windcode"] = financial["S_INFO_WINDCODE"].fillna(financial["WIND_CODE"])
    financial["ann_date"] = parse_wind_date(financial["ANN_DT"])
    financial["report_date"] = parse_wind_date(financial["REPORT_PERIOD"])
    financial["opdate_ts"] = pd.to_datetime(financial["OPDATE"], errors="coerce")
    financial = financial[
        financial["windcode"].notna()
        & financial["ann_date"].notna()
        & financial["report_date"].notna()
    ].copy()
    financial["statement_priority"] = financial["STATEMENT_TYPE"].map(statement_priority)
    financial["growth_g"] = pd.to_numeric(financial["S_FA_YOYNETPROFIT"], errors="coerce")
    financial["growth_g_quarterly"] = pd.to_numeric(financial["S_QFA_YOYNETPROFIT"], errors="coerce")
    financial["roe_reported"] = pd.to_numeric(financial["S_FA_ROE"], errors="coerce").fillna(
        pd.to_numeric(financial["WAA_ROE"], errors="coerce")
    )
    financial = financial.sort_values(
        ["windcode", "report_date", "statement_priority", "ann_date", "opdate_ts"],
        ascending=[True, True, True, False, False],
    )
    financial = financial.drop_duplicates(["windcode", "report_date"], keep="first")
    financial = financial.sort_values(["ann_date", "windcode"])

    grid = stock_monthly[["windcode", "month_end"]].drop_duplicates()
    grid = grid.sort_values(["month_end", "windcode"])
    monthly = pd.merge_asof(
        grid,
        financial[
            [
                "windcode",
                "ann_date",
                "report_date",
                "growth_g",
                "growth_g_quarterly",
                "roe_reported",
            ]
        ],
        by="windcode",
        left_on="month_end",
        right_on="ann_date",
        direction="backward",
        allow_exact_matches=True,
    )
    return monthly.reset_index(drop=True)


def build_stock_factor_monthly(config: FactorConfig) -> pd.DataFrame:
    """生成个股月度因子面板。"""

    stock = read_stock_derivative_monthly(config)
    financial = prepare_financial_indicator_monthly(config, stock)
    panel = stock.merge(financial, on=["windcode", "month_end"], how="left")

    # 对横截面极端值做轻量缩尾。研报图 4-6 统计的是行业平均暴露，极端值会明显扰动。
    for col in ["growth_g", "roe_ttm", "dividend_yield"]:
        panel[col] = panel.groupby("month_end", group_keys=False)[col].transform(winsorize_series)
    return panel.reset_index(drop=True)


def aggregate_to_industry(stock_panel: pd.DataFrame, config: FactorConfig) -> pd.DataFrame:
    """按月末行业归属和自由流通市值权重聚合到行业层面。"""

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
        weights = group["float_mv"].fillna(group["total_mv"])
        rows.append(
            {
                **dict(zip(group_cols, keys)),
                "stock_count": group["windcode"].nunique(),
                "weight_sum": weights[weights > 0].sum(),
                "growth_g": weighted_average(group["growth_g"], weights),
                "roe_ttm": weighted_average(group["roe_ttm"], weights),
                "dividend_yield": weighted_average(group["dividend_yield"], weights),
                "growth_g_coverage": group["growth_g"].notna().mean(),
                "roe_ttm_coverage": group["roe_ttm"].notna().mean(),
                "dividend_yield_coverage": group["dividend_yield"].notna().mean(),
            }
        )
    industry = pd.DataFrame(rows)
    industry = industry[industry["stock_count"] >= config.min_stocks].copy()

    months = pd.DatetimeIndex(sorted(stock_panel["month_end"].dropna().unique()))
    universe = industry_universe[
        ["industry_old_code", "industry_code", "industry_name", "industry_source_level"]
    ].drop_duplicates("industry_old_code")
    full_panel = pd.MultiIndex.from_product(
        [months, universe["industry_old_code"]],
        names=["month_end", "industry_old_code"],
    ).to_frame(index=False)
    full_panel = full_panel.merge(universe, on="industry_old_code", how="left")
    industry = full_panel.merge(industry, on=group_cols, how="left")
    return industry.sort_values(["month_end", "industry_old_code"]).reset_index(drop=True)


def merge_lifecycle(industry_panel: pd.DataFrame) -> pd.DataFrame:
    """合并已生成的行业生命周期标签。"""

    lifecycle_path = PROCESSED_DATA_DIR / "industry_lifecycle_monthly.parquet"
    if not lifecycle_path.exists():
        LOGGER.warning("未找到生命周期面板：%s，因子面板将不含生命周期标签。", lifecycle_path)
        return industry_panel

    lifecycle = pd.read_parquet(
        lifecycle_path,
        columns=[
            "month_end",
            "industry_old_code",
            "lifecycle_stage",
            "lifecycle_stage_zh",
            "cashflow_pattern",
        ],
    )
    return industry_panel.merge(lifecycle, on=["month_end", "industry_old_code"], how="left")


def write_coverage_report(stock_panel: pd.DataFrame, industry_panel: pd.DataFrame) -> None:
    """输出覆盖情况，方便后续判断因子是否可用于统计。"""

    rows = []
    for name, df, entity_col in [
        ("stock_factor_monthly", stock_panel, "windcode"),
        ("industry_factor_panel", industry_panel, "industry_old_code"),
    ]:
        rows.append(
            {
                "dataset": name,
                "rows": len(df),
                "min_month": df["month_end"].min(),
                "max_month": df["month_end"].max(),
                "entity_count": df[entity_col].nunique(),
                "growth_g_nonnull": df["growth_g"].notna().sum() if "growth_g" in df else None,
                "roe_ttm_nonnull": df["roe_ttm"].notna().sum() if "roe_ttm" in df else None,
                "dividend_yield_nonnull": df["dividend_yield"].notna().sum()
                if "dividend_yield" in df
                else None,
            }
        )
    COVERAGE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(COVERAGE_OUTPUT, index=False, encoding="utf-8-sig")


def run_factors(config: FactorConfig, overwrite: bool = False) -> pd.DataFrame:
    """执行行业月度因子构建。"""

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)

    fingerprint = config_fingerprint(config)
    cached_fingerprint = _load_cached_fingerprint()
    cache_valid = INDUSTRY_FACTOR_OUTPUT.exists() and cached_fingerprint == fingerprint
    if cache_valid and not overwrite:
        LOGGER.info("行业因子缓存命中（参数指纹一致：%s），跳过计算。", fingerprint)
        industry_panel = pd.read_parquet(INDUSTRY_FACTOR_OUTPUT)
        stock_panel = pd.read_parquet(STOCK_MONTHLY_OUTPUT) if STOCK_MONTHLY_OUTPUT.exists() else pd.DataFrame()
        write_coverage_report(stock_panel, industry_panel)
        return industry_panel

    stock_panel = build_stock_factor_monthly(config)
    industry_panel = aggregate_to_industry(stock_panel, config)
    industry_panel = merge_lifecycle(industry_panel)

    stock_panel.to_parquet(STOCK_MONTHLY_OUTPUT, index=False)
    industry_panel.to_parquet(INDUSTRY_FACTOR_OUTPUT, index=False)
    CACHE_META_OUTPUT.write_text(
        json.dumps({"fingerprint": fingerprint}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_coverage_report(stock_panel, industry_panel)
    return industry_panel


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建行业月度资产特征因子面板")
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
    config = FactorConfig(
        industry_scheme=args.industry_scheme,
        industry_level=args.industry_level,
        min_stocks=args.min_stocks,
        min_tertiary_stocks=args.min_tertiary_stocks,
        target_industries=args.target_industries,
    )
    panel = run_factors(config, overwrite=args.overwrite)
    LOGGER.info("行业因子面板：%s 行", len(panel))
    LOGGER.info("输出：%s", INDUSTRY_FACTOR_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
