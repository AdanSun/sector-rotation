"""重建 output/tables/data_coverage.csv（只读现有 data/raw，不连库、不动 processed）。

用途：data_coverage.csv 是前端"数据状态页"日期/分区信息的来源，若被旧文件覆盖残缺
（例如只剩非分区表两行），运行本脚本可从 data/raw 各分区统计出完整的 coverage，
随后重新导出快照即可恢复数据状态页的行情/基本面日期。

用法：python scripts/rebuild_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import database as db  # noqa: E402
from config import OUTPUT_DIR, RAW_DATA_DIR  # noqa: E402


def _resolve_columns(spec: db.DatasetSpec) -> tuple[str | None, str | None]:
    """从数据集任意一个分区文件的 schema 解析实际存在的日期列/实体列。"""

    raw_dir = RAW_DATA_DIR / spec.name
    sample = None
    if spec.partition_by_year:
        for year_dir in sorted(raw_dir.glob("year=*")):
            sample = next(iter(sorted(year_dir.glob("part-*.parquet"))), None)
            if sample:
                break
    else:
        sample = next(iter(sorted(raw_dir.glob("part-*.parquet"))), None)
    if sample is None:
        return spec.date_candidates[0] if spec.date_candidates else None, None
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(sample).schema.names)
    date_column = next((c for c in spec.date_candidates if c in names), None)
    entity_column = next((c for c in spec.entity_candidates if c in names), None)
    return date_column, entity_column


def _scan_partitions(spec: db.DatasetSpec) -> list[dict[str, object]]:
    """扫描数据集下所有已缓存分区，返回 coverage 行。"""

    raw_dir = RAW_DATA_DIR / spec.name
    rows: list[dict[str, object]] = []
    date_column, entity_column = _resolve_columns(spec)

    def _row_for(partition_dir: Path, partition: object) -> dict[str, object]:
        stats = db._parquet_partition_stats(partition_dir, date_column, entity_column)
        if stats["row_count"] > 0:
            status = "ok"
            message = ""
        elif partition_dir.exists():
            status = "empty"
            message = "查询范围内没有数据"
        else:
            status = "missing"
            message = "目录不存在"
        return {
            "dataset": spec.name,
            "status": status,
            "required": spec.required,
            "table_name": spec.table_candidates[0] if spec.table_candidates else "",
            "date_column": date_column or "",
            "partition": partition,
            **stats,
            "column_count": len(spec.selected_columns),
            "message": message,
        }

    if spec.partition_by_year:
        for year_dir in sorted(raw_dir.glob("year=*")):
            year = int(year_dir.name.split("=")[1])
            rows.append(_row_for(year_dir, year))
        if not list(raw_dir.glob("year=*")):
            # 无任何年份分区 → 标记为缺失（partition=None 单行）
            rows.append(_row_for(raw_dir / "year=missing", None))
    else:
        rows.append(_row_for(raw_dir, None))
    return rows


def rebuild() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for spec in db.DATASET_SPECS:
        rows.extend(_scan_partitions(spec))
    coverage = pd.DataFrame(rows)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)
    db._write_csv_atomic(coverage, OUTPUT_DIR / "tables" / "data_coverage.csv")
    return coverage


if __name__ == "__main__":
    coverage = rebuild()
    summary = coverage.groupby("status").size().to_dict()
    print("coverage 已重建：", summary)
    print("总行数：", len(coverage))
