"""
研报 2.2.1-2.2.2：不同生命周期行业的估值体系适用性。

图 16-17：按月度截面检验 PB-ROE 体系。
图 18-19：按月度截面检验 PE-g 体系。

实现原则：
1. 回归只使用当月可得的行业月度因子；
2. 截面样本少于 5 个行业时跳过；
3. 研报 PE-g 使用 PE 原值回归，而不是 ln(PE)，因为 PE 存在负值。
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
from src.lifecycle import LifecycleConfig, attach_industry_by_month, parse_wind_date, prepare_industry_map, read_parquet_dataset


LOGGER = logging.getLogger(__name__)

FACTOR_PANEL = PROCESSED_DATA_DIR / "industry_factor_panel.parquet"
RETURN_DECOMP_PANEL = PROCESSED_DATA_DIR / "industry_return_decomposition.parquet"
REGRESSION_PANEL_OUTPUT = PROCESSED_DATA_DIR / "industry_valuation_regression_panel.parquet"
REGRESSION_R2_OUTPUT = OUTPUT_DIR / "tables" / "valuation_regression_r2.csv"
REGRESSION_SUMMARY_OUTPUT = OUTPUT_DIR / "tables" / "valuation_regression_summary.csv"
REGRESSION_COVERAGE_OUTPUT = OUTPUT_DIR / "tables" / "valuation_regression_coverage.csv"

STAGE_ORDER = ["成长期", "成熟期", "停滞期", "衰退期", "转型期"]
REGRESSION_STAGE_ORDER = STAGE_ORDER + ["全样本"]
MIN_OBS = 5
# 标准化后仍极端病态的设计矩阵无法产生有解释力的高阶 PE-g 回归结果。
MAX_DESIGN_CONDITION_NUMBER = 1e10


@dataclass(frozen=True)
class RegressionConfig:
    """估值体系回归参数。"""

    start_date: str = START_DATE
    regression_start_date: str = "2008-01-01"
    pb_roe_simple_start_date: str = "2008-02-01"
    end_date: str = END_DATE
    min_obs: int = MIN_OBS
    min_stocks: int = 3
    industry_scheme: str = "hybrid"
    industry_level: int = 4
    min_tertiary_stocks: int = 8
    target_industries: Optional[int] = None


def winsorize_by_month(panel: pd.DataFrame, columns: list[str], lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """按月度截面对回归变量做轻量缩尾。"""

    panel = panel.copy()
    for col in columns:
        def _clip(series: pd.Series) -> pd.Series:
            valid = series.dropna()
            if valid.empty:
                return series
            return series.clip(valid.quantile(lower), valid.quantile(upper))

        panel[col] = panel.groupby("month_end", group_keys=False)[col].transform(_clip)
    return panel


def ols_r2(y: pd.Series, x: pd.DataFrame | pd.Series, min_obs: int = MIN_OBS) -> tuple[float, int]:
    """带截距 OLS 的 R²，跳过数值病态的截面。

    PE 与高阶一致预期增长项可能处于非常大的有限数值范围。直接以原值求
    最小二乘会令截距和预测值在矩阵乘法时溢出，进而产生看似存在、实则无效
    的 R²。R² 对因变量的线性缩放不变，因此这里先将每一列缩放到量级为 1，
    再标准化自变量；没有有效波动的自变量直接剔除。
    """

    x_df = x.to_frame() if isinstance(x, pd.Series) else x.copy()
    data = pd.concat([y.rename("y"), x_df], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < min_obs:
        return float("nan"), int(len(data))

    y_arr = data["y"].to_numpy(dtype=np.float64)
    x_arr = data.drop(columns="y").to_numpy(dtype=np.float64)
    finite_rows = np.isfinite(y_arr) & np.isfinite(x_arr).all(axis=1)
    y_arr, x_arr = y_arr[finite_rows], x_arr[finite_rows]
    if len(y_arr) < min_obs:
        return float("nan"), int(len(y_arr))

    # 先按绝对量级缩放，避免 mean、std 和 fitted 的中间计算溢出。
    y_scale = np.max(np.abs(y_arr))
    if not np.isfinite(y_scale) or y_scale == 0:
        return float("nan"), int(len(y_arr))
    y_scaled = y_arr / y_scale
    y_centered = y_scaled - y_scaled.mean()
    ss_total = float(np.dot(y_centered, y_centered))
    if not np.isfinite(ss_total) or ss_total <= np.finfo(np.float64).eps:
        return float("nan"), int(len(y_arr))

    x_scale = np.max(np.abs(x_arr), axis=0)
    usable = np.isfinite(x_scale) & (x_scale > 0)
    if not usable.any():
        return float("nan"), int(len(y_arr))
    x_scaled = x_arr[:, usable] / x_scale[usable]
    x_centered = x_scaled - x_scaled.mean(axis=0)
    x_std = x_centered.std(axis=0)
    usable = np.isfinite(x_std) & (x_std > np.finfo(np.float64).eps)
    if not usable.any():
        return float("nan"), int(len(y_arr))
    x_standardized = x_centered[:, usable] / x_std[usable]

    # 参数数 = 有效自变量数 + 截距；至少留一个残差自由度。
    if len(y_arr) < max(min_obs, x_standardized.shape[1] + 2):
        return float("nan"), int(len(y_arr))
    design = np.column_stack([np.ones(len(y_arr)), x_standardized])
    if np.linalg.matrix_rank(design) < design.shape[1]:
        return float("nan"), int(len(y_arr))
    condition_number = np.linalg.cond(design)
    if not np.isfinite(condition_number) or condition_number > MAX_DESIGN_CONDITION_NUMBER:
        return float("nan"), int(len(y_arr))
    try:
        beta, *_ = np.linalg.lstsq(design, y_scaled, rcond=None)
        # 极端病态截面即使通过底层 LAPACK，也会在此处给出 inf；检查后丢弃。
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            fitted = design @ beta
    except np.linalg.LinAlgError:
        return float("nan"), int(len(y_arr))
    if not np.isfinite(beta).all() or not np.isfinite(fitted).all():
        return float("nan"), int(len(y_arr))
    ss_resid = float(np.dot(y_scaled - fitted, y_scaled - fitted))
    if not np.isfinite(ss_resid):
        return float("nan"), int(len(y_arr))
    r2 = 1 - ss_resid / ss_total
    return (float(r2), int(len(y_arr))) if np.isfinite(r2) else (float("nan"), int(len(y_arr)))


def _read_stock_valuation_monthly(config: RegressionConfig) -> pd.DataFrame:
    """读取个股月末 PE、净利润、市值等估值字段。"""

    df = read_parquet_dataset(
        "stock_eod_derivative",
        columns=[
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_MV",
            "S_VAL_MV",
            "S_VAL_PE_TTM",
            "S_VAL_PB_NEW",
            "NET_PROFIT_PARENT_COMP_TTM",
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
    df["pe_ttm_stock"] = pd.to_numeric(df["S_VAL_PE_TTM"], errors="coerce")
    df["pb_stock"] = pd.to_numeric(df["S_VAL_PB_NEW"], errors="coerce")
    df["net_profit_ttm"] = pd.to_numeric(df["NET_PROFIT_PARENT_COMP_TTM"], errors="coerce")
    df["net_assets"] = pd.to_numeric(df["NET_ASSETS_TODAY"], errors="coerce")
    return df[
        [
            "windcode",
            "month_end",
            "float_mv",
            "total_mv",
            "pe_ttm_stock",
            "pb_stock",
            "net_profit_ttm",
            "net_assets",
        ]
    ].reset_index(drop=True)


def _read_consensus_fy1_monthly(config: RegressionConfig, stock_monthly: pd.DataFrame) -> pd.DataFrame:
    """按月末匹配最新 FY1 一致预期净利润。"""

    path = RAW_DATA_DIR / "consensus"
    if not path.exists():
        LOGGER.warning("缺少一致预期数据目录：%s，g_fttm 分支将缺失。", path)
        return stock_monthly[["windcode", "month_end"]].assign(consensus_net_profit_fy1=np.nan)

    columns = [
        "S_INFO_WINDCODE",
        "WIND_CODE",
        "EST_DT",
        "EST_REPORT_DT",
        "NUM_EST_INST",
        "NET_PROFIT_AVG",
        "CONSEN_DATA_CYCLE_TYP",
        "S_EST_YEARTYPE",
        "OPDATE",
    ]
    frames = [pd.read_parquet(file, columns=columns) for file in sorted(path.rglob("*.parquet"))]
    if not frames:
        return stock_monthly[["windcode", "month_end"]].assign(consensus_net_profit_fy1=np.nan)
    consensus = pd.concat(frames, ignore_index=True)
    consensus["windcode"] = consensus["S_INFO_WINDCODE"].fillna(consensus["WIND_CODE"])
    consensus["est_date"] = parse_wind_date(consensus["EST_DT"])
    consensus["est_report_date"] = parse_wind_date(consensus["EST_REPORT_DT"])
    consensus["net_profit_avg"] = pd.to_numeric(consensus["NET_PROFIT_AVG"], errors="coerce")
    consensus["num_est_inst"] = pd.to_numeric(consensus["NUM_EST_INST"], errors="coerce")
    consensus["opdate_ts"] = pd.to_datetime(consensus["OPDATE"], errors="coerce")
    consensus = consensus[
        consensus["windcode"].notna()
        & consensus["est_date"].notna()
        & consensus["net_profit_avg"].notna()
        & consensus["S_EST_YEARTYPE"].eq("FY1")
    ].copy()
    consensus = consensus.sort_values(
        ["windcode", "est_date", "num_est_inst", "opdate_ts"],
        ascending=[True, True, False, False],
    )
    consensus = consensus.drop_duplicates(["windcode", "est_date"], keep="first")
    consensus = consensus.sort_values(["est_date", "windcode"])

    grid = stock_monthly[["windcode", "month_end"]].drop_duplicates().sort_values(["month_end", "windcode"])
    matched = pd.merge_asof(
        grid,
        consensus[["windcode", "est_date", "est_report_date", "net_profit_avg", "num_est_inst"]],
        by="windcode",
        left_on="month_end",
        right_on="est_date",
        direction="backward",
        allow_exact_matches=True,
    )
    matched = matched.rename(
        columns={
            "net_profit_avg": "consensus_net_profit_fy1",
            "num_est_inst": "consensus_inst_count",
        }
    )
    return matched.reset_index(drop=True)


def merge_factor_with_valuation_data(factor: pd.DataFrame, valuation_base: pd.DataFrame) -> pd.DataFrame:
    """合并因子与估值数据，并以实际现金分红口径的 DP 为准。

    ``industry_factor_panel`` 曾保留一个历史 ``dividend_yield`` 列，但部分月度
    该列并未覆盖；实际现金分红计算得到的行业 DP 位于
    ``industry_return_decomposition``。红利策略应使用后者，否则 DP 全空会让
    DP+ROE、DP+BP 两类评分在整月失效。
    """

    factor_without_legacy_dp = factor.drop(columns=["dividend_yield"], errors="ignore")
    return factor_without_legacy_dp.merge(
        valuation_base,
        on=["month_end", "industry_old_code"],
        how="left",
    )


def build_industry_valuation_regression_panel(config: RegressionConfig) -> pd.DataFrame:
    """构造行业月度估值回归面板。"""

    if not FACTOR_PANEL.exists():
        raise FileNotFoundError(f"缺少行业因子面板，请先运行 python -m src.factors：{FACTOR_PANEL}")
    if not RETURN_DECOMP_PANEL.exists():
        raise FileNotFoundError(f"缺少收益拆解面板，请先运行 python -m src.valuation：{RETURN_DECOMP_PANEL}")

    factor = pd.read_parquet(
        FACTOR_PANEL,
        columns=[
            "month_end",
            "industry_old_code",
            "industry_code",
            "industry_name",
            "industry_source_level",
            "growth_g",
            "roe_ttm",
            "stock_count",
            "lifecycle_stage",
            "lifecycle_stage_zh",
        ],
    )
    valuation_base = pd.read_parquet(
        RETURN_DECOMP_PANEL,
        columns=[
            "month_end",
            "industry_old_code",
            "pb",
            "book_value",
            "total_mv",
            "market_value_yuan",
            "dividend_yield",
        ],
    )
    panel = merge_factor_with_valuation_data(factor, valuation_base)

    stock_monthly = _read_stock_valuation_monthly(config)
    consensus = _read_consensus_fy1_monthly(config, stock_monthly)
    stock_monthly = stock_monthly.merge(consensus, on=["windcode", "month_end"], how="left")
    stock_monthly["g_fttm"] = (
        stock_monthly["consensus_net_profit_fy1"] * 10000 / stock_monthly["net_profit_ttm"].where(stock_monthly["net_profit_ttm"] > 0)
        - 1
    ) * 100

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
    stock_industry = attach_industry_by_month(stock_monthly, membership, industry_universe)

    rows = []
    group_cols = ["month_end", "industry_old_code"]
    for keys, group in stock_industry.groupby(group_cols):
        weights = group["float_mv"].fillna(group["total_mv"])
        total_mv = group["total_mv"].where(group["total_mv"] > 0).sum(min_count=1)
        net_profit = group["net_profit_ttm"].sum(min_count=1)
        pe_ttm = total_mv * 10000 / net_profit if pd.notna(total_mv) and pd.notna(net_profit) and net_profit != 0 else np.nan
        rows.append(
            {
                "month_end": keys[0],
                "industry_old_code": keys[1],
                "pe_ttm": pe_ttm,
                "pe_ttm_weighted": weighted_average(group["pe_ttm_stock"], weights),
                "net_profit_ttm": net_profit,
                "g_fttm": weighted_average(group["g_fttm"], weights),
                "g_fttm_coverage": group["g_fttm"].notna().mean(),
                "consensus_stock_count": int(group["g_fttm"].notna().sum()),
            }
        )
    valuation = pd.DataFrame(rows)
    panel = panel.merge(valuation, on=["month_end", "industry_old_code"], how="left")
    panel = panel.sort_values(["industry_old_code", "month_end"]).reset_index(drop=True)

    panel["ln_pb"] = np.log(panel["pb"].where(panel["pb"] > 0))
    panel["bp"] = 1 / panel["pb"].where(panel["pb"] > 0)
    panel["ln_pe"] = np.log(panel["pe_ttm"].where(panel["pe_ttm"] > 0))
    panel["roe_delta_3m"] = panel.groupby("industry_old_code")["roe_ttm"].diff(3)
    panel["roe_std_12m"] = panel.groupby("industry_old_code")["roe_ttm"].rolling(12, min_periods=6).std().reset_index(level=0, drop=True)
    panel["g_fttm_delta_3m"] = panel.groupby("industry_old_code")["g_fttm"].diff(3)
    panel["g_fttm_sq"] = panel["g_fttm"] ** 2
    panel["g_fttm_cu"] = panel["g_fttm"] ** 3
    panel["g_fttm_4"] = panel["g_fttm"] ** 4
    panel["g_fttm_5"] = panel["g_fttm"] ** 5

    regression_start = pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M")
    panel = panel[panel["month_end"] >= regression_start].copy()
    panel = winsorize_by_month(
        panel,
        [
            "ln_pb",
            "roe_ttm",
            "roe_delta_3m",
            "roe_std_12m",
            "pe_ttm",
            "growth_g",
            "g_fttm",
            "g_fttm_delta_3m",
            "g_fttm_sq",
            "g_fttm_cu",
            "g_fttm_4",
            "g_fttm_5",
        ],
    )
    return panel.reset_index(drop=True)


def _stage_groups(panel: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    groups = [(stage, panel[panel["lifecycle_stage_zh"].eq(stage)]) for stage in STAGE_ORDER]
    groups.append(("全样本", panel[panel["lifecycle_stage_zh"].isin(STAGE_ORDER)]))
    return groups


def run_monthly_regressions(panel: pd.DataFrame, config: RegressionConfig) -> pd.DataFrame:
    """逐月逐生命周期计算 R²。"""

    model_specs: list[tuple[str, str, str, list[str], pd.Timestamp]] = [
        (
            "fig16_pb_roe_simple",
            "PB-ROE: ROE",
            "ln_pb",
            ["roe_ttm"],
            pd.Timestamp(config.pb_roe_simple_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig17_pb_roe_with_delta",
            "PB-ROE: ROE, ΔROE",
            "ln_pb",
            ["roe_ttm", "roe_delta_3m"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig17_pb_roe_full",
            "PB-ROE: ROE, ΔROE, std_ROE",
            "ln_pb",
            ["roe_ttm", "roe_delta_3m", "roe_std_12m"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig18_pe_g_ttm",
            "PE: g_ttm",
            "pe_ttm",
            ["growth_g"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig18_pe_g_fttm",
            "PE: g_fttm",
            "pe_ttm",
            ["g_fttm"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_sq",
            "PE: g_fttm^2",
            "pe_ttm",
            ["g_fttm_sq"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_cu",
            "PE: g_fttm^3",
            "pe_ttm",
            ["g_fttm_cu"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_4",
            "PE: g_fttm^4",
            "pe_ttm",
            ["g_fttm_4"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_5",
            "PE: g_fttm^5",
            "pe_ttm",
            ["g_fttm_5"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_poly",
            "PE: g_fttm, g_fttm^2, g_fttm^3, g_fttm^4, g_fttm^5",
            "pe_ttm",
            ["g_fttm", "g_fttm_sq", "g_fttm_cu", "g_fttm_4", "g_fttm_5"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_delta",
            "PE: g_fttm, Δg_fttm",
            "pe_ttm",
            ["g_fttm", "g_fttm_delta_3m"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
        (
            "fig19_pe_g_fttm_poly_delta",
            "PE: g_fttm, g_fttm^2, g_fttm^3, g_fttm^4, g_fttm^5, Δg_fttm",
            "pe_ttm",
            ["g_fttm", "g_fttm_sq", "g_fttm_cu", "g_fttm_4", "g_fttm_5", "g_fttm_delta_3m"],
            pd.Timestamp(config.regression_start_date).to_period("M").to_timestamp("M"),
        ),
    ]

    rows = []
    for model_id, model_name, y_col, x_cols, start in model_specs:
        model_panel = panel[panel["month_end"] >= start].copy()
        for month_end, month_group in model_panel.groupby("month_end"):
            for stage, stage_group in _stage_groups(month_group):
                r2, nobs = ols_r2(stage_group[y_col], stage_group[x_cols], min_obs=config.min_obs)
                rows.append(
                    {
                        "model_id": model_id,
                        "model_name": model_name,
                        "month_end": month_end,
                        "lifecycle_stage_zh": stage,
                        "r2": r2,
                        "nobs": nobs,
                        "y": y_col,
                        "x": ",".join(x_cols),
                    }
                )
    r2 = pd.DataFrame(rows)
    r2["lifecycle_stage_zh"] = pd.Categorical(r2["lifecycle_stage_zh"], REGRESSION_STAGE_ORDER, ordered=True)
    return r2.sort_values(["model_id", "month_end", "lifecycle_stage_zh"]).reset_index(drop=True)


def summarize_r2(r2: pd.DataFrame) -> pd.DataFrame:
    """汇总各模型 R² 均值。"""

    summary = (
        r2.groupby(["model_id", "model_name", "lifecycle_stage_zh"], observed=True)
        .agg(
            mean_r2=("r2", "mean"),
            median_r2=("r2", "median"),
            valid_months=("r2", lambda s: int(s.notna().sum())),
            avg_nobs=("nobs", "mean"),
        )
        .reset_index()
    )
    summary["lifecycle_stage_zh"] = pd.Categorical(summary["lifecycle_stage_zh"], REGRESSION_STAGE_ORDER, ordered=True)
    return summary.sort_values(["model_id", "lifecycle_stage_zh"]).reset_index(drop=True)


def write_coverage(panel: pd.DataFrame, r2: pd.DataFrame) -> None:
    """输出回归覆盖情况。"""

    rows = [
        {
            "dataset": "industry_valuation_regression_panel",
            "rows": len(panel),
            "min_month": panel["month_end"].min(),
            "max_month": panel["month_end"].max(),
            "industry_count": panel["industry_old_code"].nunique(),
            "ln_pb_nonnull": panel["ln_pb"].notna().sum(),
            "pe_ttm_nonnull": panel["pe_ttm"].notna().sum(),
            "growth_g_nonnull": panel["growth_g"].notna().sum(),
            "g_fttm_nonnull": panel["g_fttm"].notna().sum(),
        },
        {
            "dataset": "valuation_regression_r2",
            "rows": len(r2),
            "min_month": r2["month_end"].min(),
            "max_month": r2["month_end"].max(),
            "industry_count": None,
            "ln_pb_nonnull": None,
            "pe_ttm_nonnull": None,
            "growth_g_nonnull": None,
            "g_fttm_nonnull": None,
        },
    ]
    REGRESSION_COVERAGE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(REGRESSION_COVERAGE_OUTPUT, index=False, encoding="utf-8-sig")


def _set_plot_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _plot_grouped_bars(data: pd.DataFrame, title: str, filename: str, model_order: list[str]) -> None:
    import matplotlib.pyplot as plt

    _set_plot_style()
    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    pivot = data.pivot_table(
        index="lifecycle_stage_zh",
        columns="model_name",
        values="mean_r2",
        aggfunc="mean",
        observed=True,
    ).reindex(REGRESSION_STAGE_ORDER)
    pivot = pivot[[model for model in model_order if model in pivot.columns]]
    x = np.arange(len(pivot.index))
    width = 0.76 / max(len(pivot.columns), 1)
    colors = ["#219EBC", "#023047", "#FFB703", "#FB8500", "#6A4C93", "#2A9D8F", "#D62828", "#8ECAE6"]
    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    for idx, col in enumerate(pivot.columns):
        offset = (idx - (len(pivot.columns) - 1) / 2) * width
        ax.bar(x + offset, pivot[col], width=width, label=col, color=colors[idx % len(colors)])
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title(title)
    ax.set_ylabel("截面回归 R² 均值")
    ax.set_xlabel("生命周期阶段")
    ax.set_xticks(x)
    ax.set_xticklabels(pivot.index.astype(str))
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(figure_dir / filename, dpi=200)
    plt.close(fig)


def plot_regression_figures(summary: pd.DataFrame) -> None:
    """输出图 16-19。"""

    _plot_grouped_bars(
        summary[summary["model_id"].eq("fig16_pb_roe_simple")],
        "图16：PB-ROE 估值体系截面解释度",
        "fig16_pb_roe_r2.png",
        ["PB-ROE: ROE"],
    )
    _plot_grouped_bars(
        summary[summary["model_id"].isin(["fig17_pb_roe_with_delta", "fig17_pb_roe_full"])],
        "图17：加入 ROE 动量与稳定性的 PB-ROE 截面解释度",
        "fig17_pb_roe_enhanced_r2.png",
        ["PB-ROE: ROE, ΔROE", "PB-ROE: ROE, ΔROE, std_ROE"],
    )
    _plot_grouped_bars(
        summary[summary["model_id"].isin(["fig18_pe_g_ttm", "fig18_pe_g_fttm"])],
        "图18：PE-g 估值体系截面解释度",
        "fig18_pe_g_r2.png",
        ["PE: g_ttm", "PE: g_fttm"],
    )
    fig19_order = [
        "PE: g_fttm",
        "PE: g_fttm^2",
        "PE: g_fttm^3",
        "PE: g_fttm^4",
        "PE: g_fttm^5",
        "PE: g_fttm, g_fttm^2, g_fttm^3, g_fttm^4, g_fttm^5",
        "PE: g_fttm, Δg_fttm",
        "PE: g_fttm, g_fttm^2, g_fttm^3, g_fttm^4, g_fttm^5, Δg_fttm",
    ]
    _plot_grouped_bars(
        summary[summary["model_id"].str.startswith("fig19") | summary["model_id"].eq("fig18_pe_g_fttm")],
        "图19：PE-g 非线性与动量项截面解释度",
        "fig19_pe_g_extended_r2.png",
        fig19_order,
    )


def run_regressions(config: RegressionConfig, overwrite: bool = False) -> pd.DataFrame:
    """执行估值体系面板构造、回归和出图。"""

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("figures").mkdir(parents=True, exist_ok=True)

    if REGRESSION_PANEL_OUTPUT.exists() and not overwrite:
        LOGGER.info("估值回归面板缓存已存在，跳过重算：%s", REGRESSION_PANEL_OUTPUT)
        panel = pd.read_parquet(REGRESSION_PANEL_OUTPUT)
    else:
        panel = build_industry_valuation_regression_panel(config)
        panel.to_parquet(REGRESSION_PANEL_OUTPUT, index=False)

    r2 = run_monthly_regressions(panel, config)
    summary = summarize_r2(r2)
    r2.to_parquet(PROCESSED_DATA_DIR / "valuation_regression_r2.parquet", index=False)
    summary.to_csv(REGRESSION_SUMMARY_OUTPUT, index=False, encoding="utf-8-sig")
    write_coverage(panel, r2)
    plot_regression_figures(summary)
    # SKIP_FIG_CLEANUP=1 时保留中间回归图（供快照刷新链路避免删除触发沙箱确认）
    if not os.environ.get("SKIP_FIG_CLEANUP"):
        for filename in ("fig16_pb_roe_r2.png", "fig17_pb_roe_enhanced_r2.png", "fig18_pe_g_r2.png"):
            OUTPUT_DIR.joinpath("figures", filename).unlink(missing_ok=True)
    return summary


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复现研报 2.2 估值体系回归图 16-19")
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
    config = RegressionConfig(
        industry_scheme=args.industry_scheme,
        industry_level=args.industry_level,
        min_stocks=args.min_stocks,
        min_tertiary_stocks=args.min_tertiary_stocks,
        target_industries=args.target_industries,
    )
    summary = run_regressions(config, overwrite=args.overwrite)
    LOGGER.info("估值体系回归汇总：%s 行", len(summary))
    LOGGER.info("输出：%s", REGRESSION_SUMMARY_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
