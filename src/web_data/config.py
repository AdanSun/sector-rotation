"""网页数据导出配置。

只负责数据路径、白名单和展示枚举，不连接数据库、不读取敏感环境变量。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_TABLES_DIR = PROJECT_ROOT / "output" / "tables"
OUTPUT_FIGURES_DIR = PROJECT_ROOT / "output" / "figures"

# 允许读取的 Parquet 面板白名单（相对 PROJECT_ROOT 的 data/processed/ 下文件名）
PARQUET_FILES: dict[str, Path] = {
    "strategy_signal_panel": PROCESSED_DIR / "strategy_signal_panel.parquet",
    "strategy_signal_holdings": PROCESSED_DIR / "strategy_signal_holdings.parquet",
    "strategy_signal_returns": PROCESSED_DIR / "strategy_signal_returns.parquet",
    "asset_decision_tree_panel": PROCESSED_DIR / "asset_decision_tree_panel.parquet",
    "backtest_monthly_positions": PROCESSED_DIR / "backtest_monthly_positions.parquet",
    "backtest_monthly_returns": PROCESSED_DIR / "backtest_monthly_returns.parquet",
    "backtest_daily_returns": PROCESSED_DIR / "backtest_daily_returns.parquet",
    "dividend_asset_panel": PROCESSED_DIR / "dividend_asset_panel.parquet",
    "dividend_asset_holdings": PROCESSED_DIR / "dividend_asset_holdings.parquet",
}

# 允许读取的 CSV 汇总表白名单
CSV_FILES: dict[str, Path] = {
    "backtest_metrics": OUTPUT_TABLES_DIR / "backtest_metrics.csv",
    "backtest_annual_returns": OUTPUT_TABLES_DIR / "backtest_annual_returns.csv",
    "strategy_signal_summary": OUTPUT_TABLES_DIR / "strategy_signal_summary.csv",
    "data_coverage": OUTPUT_TABLES_DIR / "data_coverage.csv",
    "data_validation": OUTPUT_TABLES_DIR / "data_validation.csv",
}

# 展示层级
LEVELS = (2, 3)

# 生命周期中文枚举
LIFECYCLE_STAGES = ("成长期", "成熟期", "停滞期", "衰退期", "转型期", "未知")

# 资产类型 → 中文名
ASSET_TYPE_NAMES = {
    "main_assets": "主流资产",
    "quality_dividend": "质量红利",
    "value_dividend": "价值红利",
    "cash": "现金",
}

# 每个数据集可显示的中文名
DATASET_LABELS: dict[str, str] = {
    "strategy_signal_panel": "行业信号面板",
    "strategy_signal_holdings": "子策略推荐行业",
    "strategy_signal_returns": "子策略收益净值",
    "asset_decision_tree_panel": "资产类别决策树",
    "backtest_monthly_positions": "最终策略月度持仓",
    "backtest_monthly_returns": "最终策略月度净值",
    "dividend_asset_panel": "红利资产截面面板",
    "dividend_asset_holdings": "红利资产持仓",
    "backtest_metrics": "总体绩效指标",
    "backtest_annual_returns": "年度收益",
    "strategy_signal_summary": "子策略绩效汇总",
    "data_coverage": "数据提取覆盖",
    "data_validation": "数据质量验证",
}

# 数据滞后说明已改为由 services._dynamic_warnings() 依据 data_coverage.csv 动态生成，
# 避免固定文案与真实数据自相矛盾。此处在升级后不再提供固定文案。
