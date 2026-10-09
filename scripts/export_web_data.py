"""把 processed 面板导出为前端静态 JSON 快照。

前端不再调用 API，而是直接读取本脚本生成的 `frontend/public/data/` 下的 JSON 文件。
口径由 src/web_data/services.py 的本地派生逻辑统一定义，不依赖任何 Web API。

用法:
    python scripts/export_web_data.py [--out DIR] [--skip-industry]

说明:
    - 默认输出到 frontend/public/data/，Vite 构建时自动打包进 dist。
    - 行业详情按申万展示行业预计算为独立 JSON（level2 + level3）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src.web_data import repositories as repo  # noqa: E402
from src.web_data import services  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "frontend" / "public" / "data"


def _to_jsonable(obj: object) -> object:
    """递归把 Pydantic 模型 / 嵌套结构转为可 JSON 序列化的对象。"""

    if hasattr(obj, "model_dump"):
        return _to_jsonable(obj.model_dump(mode="json"))
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _dump(model_or_dict: object) -> str:
    return json.dumps(_to_jsonable(model_or_dict), ensure_ascii=False, indent=1, allow_nan=False)


def _write(path: Path, payload: str, tag: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    size = path.stat().st_size / 1024
    try:
        display_path = path.relative_to(DEFAULT_OUT.parent)
    except ValueError:
        display_path = path
    print(f"  [ok] {display_path}  ({size:.0f} KB)")


def export_recommendations_raw(out_dir: Path) -> None:
    """导出全量子策略推荐原始表，前端本地做月份/层级/聚合筛选。"""

    holdings = repo.load_frame("strategy_signal_holdings")
    if holdings.empty:
        _write(out_dir / "recommendations.json", "[]", "recommendations")
        return
    rows = []
    for _, row in holdings.sort_values(["month_end", "strategy_id", "rank"]).iterrows():
        rows.append(
            {
                "month": repo._safe_value(row["month_end"]),
                "strategy_id": str(row["strategy_id"]),
                "strategy_name": str(row["strategy_name"]),
                "rank": int(row["rank"]),
                "industry_old_code": str(row["industry_old_code"]),
                "industry_code": str(row.get("industry_code")) if pd.notna(row.get("industry_code")) else None,
                "industry_name": str(row["industry_name"]) if pd.notna(row.get("industry_name")) else None,
                "lifecycle_stage_zh": str(row["lifecycle_stage_zh"]) if pd.notna(row.get("lifecycle_stage_zh")) else None,
                "score": repo._safe_value(row.get("score")),
                "next_month_log_return": repo._safe_value(row.get("next_month_log_return")),
                "display_industry_code": str(row.get("display_industry_code") or row["industry_old_code"]),
                "display_industry_name": str(row.get("display_industry_name") or row["industry_name"]),
                "asset_type": services.STRATEGY_TO_ASSET.get(str(row["strategy_id"]), "main_assets"),
            }
        )
    _write(out_dir / "recommendations.json", json.dumps(rows, ensure_ascii=False, allow_nan=False), "recommendations")


def export_holdings_raw(out_dir: Path) -> None:
    """导出最终策略全量月度持仓，前端本地按月份筛选。"""

    positions = repo.load_frame("backtest_monthly_positions")
    if positions.empty:
        _write(out_dir / "holdings.json", "[]", "holdings")
        return
    rows = []
    for _, row in positions.sort_values(["month_end", "rank"]).iterrows():
        rows.append(
            {
                "month": repo._safe_value(row["month_end"]),
                "asset_type": str(row["asset_type"]) if pd.notna(row.get("asset_type")) else None,
                "asset_name": str(row["asset_name"]) if pd.notna(row.get("asset_name")) else None,
                "rank": int(row["rank"]),
                "industry_old_code": str(row["industry_old_code"]),
                "industry_code": str(row.get("industry_code")) if pd.notna(row.get("industry_code")) else None,
                "industry_name": str(row["industry_name"]) if pd.notna(row.get("industry_name")) else None,
                "display_industry_code": str(row.get("display_industry_code") or row["industry_old_code"]),
                "display_industry_name": str(row.get("display_industry_name") or row["industry_name"]),
                "weight": repo._safe_value(row.get("weight")),
                "score": repo._safe_value(row.get("score")),
                "source_count": repo._safe_value(row.get("source_count")),
                "next_month_log_return": repo._safe_value(row.get("next_month_log_return")),
            }
        )
    _write(out_dir / "holdings.json", json.dumps(rows, ensure_ascii=False, allow_nan=False), "holdings")


def export_performance(out_dir: Path) -> None:
    """导出最终策略绩效与全部子策略绩效。"""

    final = services.get_performance("final", None, None, None)
    _write(out_dir / "performance_final.json", _dump(final), "performance_final")

    # 每个子策略的月度序列与指标，前端按策略切换
    sub = {"strategies": []}
    returns = repo.load_frame("strategy_signal_returns")
    for sid, name in returns.groupby("strategy_id")["strategy_name"].first().items():
        one = services.get_performance("sub_strategy", str(sid), None, None)
        sub["strategies"].append(
            {
                "strategy_id": str(sid),
                "strategy_name": str(name),
                "metrics": [m.model_dump(mode="json") for m in one.metrics],
                "monthly": [m.model_dump(mode="json") for m in one.monthly],
            }
        )
    _write(out_dir / "performance_sub.json", json.dumps(sub, ensure_ascii=False, allow_nan=False), "performance_sub")


def export_industry_details(out_dir: Path) -> None:
    """按展示行业预计算详情：level2（展示代码）+ level3（内部 hybrid 代码）。"""

    panel = repo.load_frame("strategy_signal_panel")
    level2_codes = sorted(panel["display_industry_code"].dropna().astype(str).unique())
    level3_codes = sorted(panel["industry_old_code"].dropna().astype(str).unique())
    index: dict[str, dict] = {"2": {}, "3": {}}
    industry_dir = out_dir / "industry"
    industry_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    for level, codes in ((2, level2_codes), (3, level3_codes)):
        for i, code in enumerate(codes):
            try:
                detail = services.get_industry_detail(code, level)
                payload = detail.model_dump(mode="json")
            except (ValueError, repo.DataFileError):
                continue
            path = industry_dir / f"{code}_{level}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
            index[str(level)][code] = {
                "name": payload.get("name") or code,
                "hybrid_count": len(payload.get("children", [])) if payload.get("is_aggregate") else 1,
            }
            if (i + 1) % 50 == 0:
                print(f"    ... level{level} {i + 1}/{len(codes)} ({time.time() - start:.0f}s)")
        print(f"  [ok] industry level{level}: {len(codes)} 个行业详情")

    # 行业索引：页面跳转/名称展示用
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "levels": index,
        "lifecycle_colors": {
            "成长期": "#c0392b",
            "成熟期": "#2c5282",
            "停滞期": "#95a5a6",
            "衰退期": "#1e8449",
            "转型期": "#b9770e",
            "未知": "#bdc3c7",
        },
    }
    _write(out_dir / "industry_index.json", json.dumps(payload, ensure_ascii=False, allow_nan=False), "industry_index")


def _apply_dynamic_warnings(out_dir: Path, data_status: dict) -> None:
    """把固定的数据滞后文案替换为基于快照时刻的动态警告。"""

    warnings = data_status.get("warnings", [])
    overview_path = out_dir / "overview.json"
    if overview_path.exists():
        payload = json.loads(overview_path.read_text(encoding="utf-8"))
        payload["data_warnings"] = warnings
        overview_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8"
        )
    industry_dir = out_dir / "industry"
    if industry_dir.exists():
        for path in industry_dir.glob("*.json"):
            if path.name == "industry_index.json":
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["data_warnings"] = warnings
            path.write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="导出前端静态 JSON 快照")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--skip-industry", action="store_true", help="跳过行业详情预计算")
    args = parser.parse_args()

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"导出目录: {out_dir}")
    started = time.time()

    print("==> meta")
    meta = services.get_meta()
    _write(out_dir / "meta.json", _dump(meta), "meta")

    print("==> data-status")
    data_status = services.get_data_status()
    _write(out_dir / "data_status.json", _dump(data_status), "data_status")

    print("==> overview")
    overview = services.get_overview()
    _write(out_dir / "overview.json", _dump(overview), "overview")

    print("==> recommendations（全量原始表）")
    export_recommendations_raw(out_dir)

    print("==> decision-history")
    decision = services.get_decision_history(None, None)
    _write(out_dir / "decision_history.json", _dump(decision), "decision_history")

    print("==> holdings（全量月度持仓）")
    export_holdings_raw(out_dir)

    print("==> performance")
    export_performance(out_dir)

    if not args.skip_industry:
        print("==> industry 详情（预计算）")
        export_industry_details(out_dir)

    print("==> 动态警告回填")
    _apply_dynamic_warnings(out_dir, data_status)

    elapsed = time.time() - started
    print(f"\n完成，耗时 {elapsed:.1f}s。输出目录: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
