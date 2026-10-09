"""
研报 2.2.3：股息率估值与红利资产拆分。

图 20-21：2024 年 7 月行业 ROE-DP、BP-DP 截面散点。
图 22：质量红利与价值红利行业组合长期表现。

透明口径：
1. DP 使用行业近一年股息率字段；
2. 质量红利在成熟期行业中选 ROE 和 DP 综合排名靠前的行业；
3. 价值红利在成熟期行业中选 BP 和 DP 综合排名靠前的行业；
4. 月末形成组合，持有下月，收益使用行业成分股复权收益等权平均。
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
from config import INDUSTRY_OUTPUT_LEVEL, OUTPUT_DIR, PROCESSED_DATA_DIR
from src.output_industry_map import map_to_display


LOGGER = logging.getLogger(__name__)

REGRESSION_PANEL = PROCESSED_DATA_DIR / "industry_valuation_regression_panel.parquet"
RETURN_DECOMP_PANEL = PROCESSED_DATA_DIR / "industry_return_decomposition.parquet"
DIVIDEND_PANEL_OUTPUT = PROCESSED_DATA_DIR / "dividend_asset_panel.parquet"
SNAPSHOT_OUTPUT = OUTPUT_DIR / "tables" / "dividend_asset_snapshot_202407.csv"
HOLDINGS_OUTPUT = PROCESSED_DATA_DIR / "dividend_asset_holdings.parquet"
RETURNS_OUTPUT = PROCESSED_DATA_DIR / "dividend_asset_returns.parquet"
YEARLY_OUTPUT = OUTPUT_DIR / "tables" / "dividend_asset_yearly_returns.csv"


@dataclass(frozen=True)
class DividendAssetConfig:
    """红利资产拆分参数。"""

    snapshot_month: Optional[str] = None
    top_n: int = 5
    min_stocks: int = 3


def percentile_rank(series: pd.Series) -> pd.Series:
    """升序百分位排名，数值越大排名越高。"""

    return series.rank(pct=True, method="average")


def build_dividend_asset_panel(config: DividendAssetConfig) -> pd.DataFrame:
    """合并行业估值因子和下一月可投资收益。"""

    if not REGRESSION_PANEL.exists():
        raise FileNotFoundError(f"缺少估值回归面板，请先运行 python -m src.valuation_regression：{REGRESSION_PANEL}")
    if not RETURN_DECOMP_PANEL.exists():
        raise FileNotFoundError(f"缺少行业收益面板，请先运行 python -m src.valuation：{RETURN_DECOMP_PANEL}")

    panel = pd.read_parquet(
        REGRESSION_PANEL,
        columns=[
            "month_end",
            "industry_old_code",
            "industry_code",
            "industry_name",
            "industry_source_level",
            "lifecycle_stage_zh",
            "stock_count",
            "roe_ttm",
            "bp",
            "dividend_yield",
        ],
    )
    returns = pd.read_parquet(
        RETURN_DECOMP_PANEL,
        columns=["month_end", "industry_old_code", "stock_weighted_log_return"],
    ).sort_values(["industry_old_code", "month_end"])
    returns["next_month_log_return"] = returns.groupby("industry_old_code")["stock_weighted_log_return"].shift(-1)
    returns = returns[["month_end", "industry_old_code", "next_month_log_return"]]
    panel = panel.merge(returns, on=["month_end", "industry_old_code"], how="left")

    panel = panel.sort_values(["month_end", "industry_old_code"]).copy()
    for col in ["roe_ttm", "bp", "dividend_yield"]:
        panel[f"{col}_rank"] = panel.groupby("month_end", group_keys=False)[col].transform(percentile_rank)
    panel["quality_dividend_score"] = (panel["roe_ttm_rank"] + panel["dividend_yield_rank"]) / 2
    panel["value_dividend_score"] = (panel["bp_rank"] + panel["dividend_yield_rank"]) / 2
    return panel.reset_index(drop=True)


def select_dividend_assets(panel: pd.DataFrame, config: DividendAssetConfig) -> pd.DataFrame:
    """按月选择质量红利和价值红利行业。"""

    candidates = panel[
        panel["lifecycle_stage_zh"].eq("成熟期")
        & (panel["stock_count"].fillna(0) >= config.min_stocks)
    ].copy()
    rows = []
    specs = [
        ("quality_dividend", "质量红利", "quality_dividend_score"),
        ("value_dividend", "价值红利", "value_dividend_score"),
    ]
    for month_end, group in candidates.groupby("month_end", dropna=False):
        for type_id, name, col in specs:
            selected = group.dropna(subset=[col, "dividend_yield"]).sort_values(
                [col, "dividend_yield", "industry_old_code"],
                ascending=[False, False, True],
            ).head(config.top_n)
            for rank, (_, row) in enumerate(selected.iterrows(), start=1):
                rows.append(
                    {
                        "month_end": month_end,
                        "asset_type": type_id,
                        "asset_name": name,
                        "rank": rank,
                        "industry_old_code": row["industry_old_code"],
                        "industry_code": row["industry_code"],
                        "industry_name": row["industry_name"],
                        "score": row[col],
                        "roe_ttm": row["roe_ttm"],
                        "bp": row["bp"],
                        "dividend_yield": row["dividend_yield"],
                        "next_month_log_return": row["next_month_log_return"],
                    }
                )
    return pd.DataFrame(rows).sort_values(["month_end", "asset_type", "rank"]).reset_index(drop=True)


def compute_portfolio_returns(holdings: pd.DataFrame) -> pd.DataFrame:
    """等权组合月度收益、净值和年度收益。"""

    rows = []
    for (month_end, asset_type, asset_name), group in holdings.groupby(["month_end", "asset_type", "asset_name"]):
        rows.append(
            {
                "month_end": month_end,
                "asset_type": asset_type,
                "asset_name": asset_name,
                "holding_count": group["industry_old_code"].nunique(),
                "portfolio_log_return": group["next_month_log_return"].mean(skipna=True),
            }
        )
    returns = pd.DataFrame(rows).sort_values(["asset_type", "month_end"]).reset_index(drop=True)
    returns["portfolio_simple_return"] = np.expm1(returns["portfolio_log_return"])
    returns["valid_return"] = returns["portfolio_log_return"].notna()
    returns["nav"] = returns.groupby("asset_type")["portfolio_log_return"].transform(lambda s: np.exp(s.fillna(0).cumsum()))
    returns["return_month"] = pd.to_datetime(returns["month_end"]) + pd.offsets.MonthEnd(1)
    return returns


def compute_yearly_returns(returns: pd.DataFrame) -> pd.DataFrame:
    """年度收益表。"""

    data = returns.dropna(subset=["portfolio_log_return"]).copy()
    data["year"] = pd.to_datetime(data["return_month"]).dt.year
    yearly = (
        data.groupby(["asset_type", "asset_name", "year"], as_index=False)
        .agg(
            annual_log_return=("portfolio_log_return", "sum"),
            month_count=("portfolio_log_return", "count"),
            avg_holding_count=("holding_count", "mean"),
        )
    )
    yearly["annual_return"] = np.expm1(yearly["annual_log_return"])
    return yearly


def make_snapshot(panel: pd.DataFrame, holdings: pd.DataFrame, config: DividendAssetConfig) -> pd.DataFrame:
    """输出 2024-07 截面和选样标记。"""

    available_months = pd.to_datetime(panel["month_end"], errors="coerce").dropna()
    if available_months.empty:
        return panel.iloc[0:0].copy()
    requested = pd.Timestamp(config.snapshot_month) if config.snapshot_month else available_months.max()
    eligible = available_months[available_months.le(requested)]
    snapshot_month = eligible.max() if not eligible.empty else available_months.max()
    snapshot = panel[pd.to_datetime(panel["month_end"]).eq(snapshot_month)].copy()
    selected = holdings[pd.to_datetime(holdings["month_end"]).eq(snapshot_month)][
        ["asset_type", "asset_name", "industry_old_code", "rank"]
    ]
    selected_flag = (
        selected.pivot_table(
            index="industry_old_code",
            columns="asset_type",
            values="rank",
            aggfunc="min",
        )
        .reset_index()
        .rename(
            columns={
                "quality_dividend": "quality_dividend_rank",
                "value_dividend": "value_dividend_rank",
            }
        )
    )
    return snapshot.merge(selected_flag, on="industry_old_code", how="left")


def _set_plot_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "Songti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _scatter_with_trend(ax, data: pd.DataFrame, x_col: str, y_col: str, xlabel: str, ylabel: str) -> None:
    ax.scatter(data[x_col], data[y_col], s=28, alpha=0.72, color="#219EBC", edgecolor="white", linewidth=0.4)
    valid = data[[x_col, y_col]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(valid) >= 5:
        coef = np.polyfit(valid[x_col], valid[y_col], 1)
        xs = np.linspace(valid[x_col].min(), valid[x_col].max(), 100)
        ax.plot(xs, coef[0] * xs + coef[1], color="#D62828", linewidth=1.8, label="线性趋势")
        ax.legend(loc="upper left", frameon=False)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(color="#E5E5E5", linewidth=0.7)


def plot_figures(snapshot: pd.DataFrame, returns: pd.DataFrame, config: DividendAssetConfig) -> None:
    """输出图 20-22。"""

    import matplotlib.pyplot as plt

    _set_plot_style()
    figure_dir = OUTPUT_DIR / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)

    plot_data = snapshot.dropna(subset=["roe_ttm", "bp", "dividend_yield"]).copy()
    fig, ax = plt.subplots(figsize=(7.4, 5.2))
    _scatter_with_trend(ax, plot_data, "roe_ttm", "dividend_yield", "ROE_TTM（%）", "DP / 股息率（%）")
    ax.set_title("图20：截面行业 ROE 与 DP 分布（2024.7）")
    fig.tight_layout()
    fig.savefig(figure_dir / "fig20_roe_dp_scatter.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.4, 5.2))
    _scatter_with_trend(ax, plot_data, "bp", "dividend_yield", "BP（1/PB）", "DP / 股息率（%）")
    ax.set_title("图21：截面行业 BP 与 DP 分布（2024.7）")
    fig.tight_layout()
    fig.savefig(figure_dir / "fig21_bp_dp_scatter.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10.0, 4.8))
    colors = {"quality_dividend": "#219EBC", "value_dividend": "#FB8500"}
    plot_returns = returns[returns["valid_return"]].copy()
    for asset_type, group in plot_returns.groupby("asset_type"):
        ax.plot(group["return_month"], group["nav"], label=group["asset_name"].iloc[0], color=colors.get(asset_type), linewidth=2.0)
    ax.set_title("图22：质量红利与价值红利行业组合长期表现")
    ax.set_ylabel("净值")
    ax.set_xlabel("月份")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(axis="y", color="#E5E5E5", linewidth=0.7)
    fig.tight_layout()
    fig.savefig(figure_dir / "fig22_quality_value_dividend_nav.png", dpi=200)
    plt.close(fig)


def run_dividend_assets(
    config: DividendAssetConfig,
    overwrite: bool = False,
    industry_output_level: int = INDUSTRY_OUTPUT_LEVEL,
) -> pd.DataFrame:
    """执行红利资产拆分。"""

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("figures").mkdir(parents=True, exist_ok=True)

    if DIVIDEND_PANEL_OUTPUT.exists() and not overwrite:
        LOGGER.info("红利资产面板缓存已存在，跳过重算：%s", DIVIDEND_PANEL_OUTPUT)
        panel = pd.read_parquet(DIVIDEND_PANEL_OUTPUT)
    else:
        panel = build_dividend_asset_panel(config)
    panel = map_to_display(panel, industry_output_level)
    panel.to_parquet(DIVIDEND_PANEL_OUTPUT, index=False)

    holdings = select_dividend_assets(panel, config)
    holdings = map_to_display(holdings, industry_output_level)
    returns = compute_portfolio_returns(holdings)
    yearly = compute_yearly_returns(returns)
    snapshot = make_snapshot(panel, holdings, config)

    holdings.to_parquet(HOLDINGS_OUTPUT, index=False)
    returns.to_parquet(RETURNS_OUTPUT, index=False)
    yearly.to_csv(YEARLY_OUTPUT, index=False, encoding="utf-8-sig")
    snapshot.to_parquet(PROCESSED_DATA_DIR / "dividend_asset_snapshot.parquet", index=False)
    plot_figures(snapshot, returns, config)
    # SKIP_FIG_CLEANUP=1 时保留中间红利图（供快照刷新链路避免删除触发沙箱确认）
    if not os.environ.get("SKIP_FIG_CLEANUP"):
        for filename in ("fig20_roe_dp_scatter.png", "fig21_bp_dp_scatter.png"):
            OUTPUT_DIR.joinpath("figures", filename).unlink(missing_ok=True)
    return returns


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复现研报图 20-22：红利资产拆分")
    parser.add_argument("--snapshot-month", default=None, help="默认使用不晚于 END_DATE 的最近可用月末")
    parser.add_argument("--industry-output-level", type=int, choices=[2, 3], default=INDUSTRY_OUTPUT_LEVEL)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--min-stocks", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = DividendAssetConfig(
        snapshot_month=args.snapshot_month,
        top_n=args.top_n,
        min_stocks=args.min_stocks,
    )
    returns = run_dividend_assets(
        config,
        overwrite=args.overwrite,
        industry_output_level=args.industry_output_level,
    )
    LOGGER.info("红利资产月度收益：%s 行", len(returns))
    LOGGER.info("输出：%s", RETURNS_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
