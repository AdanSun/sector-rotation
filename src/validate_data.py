"""原始 Parquet 缓存的结构、主键和日期覆盖验证。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
import pyarrow.parquet as pq

from config import OUTPUT_DIR, RAW_DATA_DIR
from src.database import DATASET_SPECS


VALIDATION_RULES: Dict[str, Dict[str, object]] = {
    "stock_description": {
        "keys": ("S_INFO_WINDCODE",),
        "required": (
            "S_INFO_WINDCODE",
            "S_INFO_LISTDATE",
            "S_INFO_DELISTDATE",
        ),
    },
    "stock_eod_prices": {
        "keys": ("S_INFO_WINDCODE", "TRADE_DT"),
        "date": "TRADE_DT",
        "required": (
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_OPEN",
            "S_DQ_CLOSE",
            "S_DQ_ADJFACTOR",
        ),
    },
    "stock_eod_derivative": {
        "keys": ("S_INFO_WINDCODE", "TRADE_DT"),
        "date": "TRADE_DT",
        "required": (
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_VAL_MV",
            "S_DQ_MV",
            "S_VAL_PE_TTM",
            "S_VAL_PB_NEW",
        ),
    },
    "income_statement": {
        "keys": (
            "S_INFO_WINDCODE",
            "REPORT_PERIOD",
            "ANN_DT",
            "STATEMENT_TYPE",
        ),
        "date": "REPORT_PERIOD",
        "required": (
            "S_INFO_WINDCODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "OPER_REV",
            "NET_PROFIT_EXCL_MIN_INT_INC",
        ),
    },
    "balance_sheet": {
        "keys": (
            "S_INFO_WINDCODE",
            "REPORT_PERIOD",
            "ANN_DT",
            "STATEMENT_TYPE",
        ),
        "date": "REPORT_PERIOD",
        "required": (
            "S_INFO_WINDCODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "TOT_ASSETS",
            "TOT_SHRHLDR_EQY_EXCL_MIN_INT",
        ),
    },
    "cashflow_statement": {
        "keys": (
            "S_INFO_WINDCODE",
            "REPORT_PERIOD",
            "ANN_DT",
            "STATEMENT_TYPE",
        ),
        "date": "REPORT_PERIOD",
        "required": (
            "S_INFO_WINDCODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "NET_CASH_FLOWS_OPER_ACT",
            "NET_CASH_FLOWS_INV_ACT",
            "NET_CASH_FLOWS_FNC_ACT",
        ),
    },
    "financial_indicator": {
        "keys": (
            "S_INFO_WINDCODE",
            "REPORT_PERIOD",
            "ANN_DT",
            "STATEMENT_TYPE",
        ),
        "date": "REPORT_PERIOD",
        "required": (
            "S_INFO_WINDCODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "S_FA_ROE",
            "S_FA_YOYNETPROFIT",
        ),
    },
    "industry_membership": {
        "keys": (
            "S_INFO_WINDCODE",
            "SW_IND_CODE",
            "ENTRY_DT",
            "REMOVE_DT",
        ),
        "required": (
            "S_INFO_WINDCODE",
            "SW_IND_CODE",
            "ENTRY_DT",
            "REMOVE_DT",
        ),
    },
    "industry_classification": {
        "keys": ("INDUSTRIESCODE",),
        "required": (
            "INDUSTRIESCODE",
            "INDUSTRIESNAME",
            "LEVELNUM",
        ),
    },
    "dividend": {
        "keys": (
            "S_INFO_WINDCODE",
            "REPORT_PERIOD",
            "ANN_DT",
            "EX_DT",
        ),
        "date": "REPORT_PERIOD",
        "required": (
            "S_INFO_WINDCODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "EX_DT",
            "CASH_DVD_PER_SH_PRE_TAX",
        ),
    },
    "index_eod_prices": {
        "keys": ("S_INFO_WINDCODE", "TRADE_DT"),
        "date": "TRADE_DT",
        "required": (
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_CLOSE",
        ),
    },
    "consensus": {
        "keys": (
            "S_INFO_WINDCODE",
            "EST_DT",
            "EST_REPORT_DT",
            "CONSEN_DATA_CYCLE_TYP",
        ),
        "date": "EST_DT",
        "required": (
            "S_INFO_WINDCODE",
            "EST_DT",
            "EST_REPORT_DT",
            "NET_PROFIT_AVG",
            "NUM_EST_INST",
        ),
    },
}

# 主键以 DatasetSpec 为唯一定义源，验证器只保留必需字段约束。
for _spec in DATASET_SPECS:
    if _spec.name in VALIDATION_RULES and _spec.key_columns:
        VALIDATION_RULES[_spec.name]["keys"] = _spec.key_columns


def _partition_directories(dataset_dir: Path) -> List[Path]:
    year_directories = sorted(dataset_dir.glob("year=*"))
    return year_directories or [dataset_dir]


def _validate_partition(
    dataset: str,
    directory: Path,
    keys: Tuple[str, ...],
    required: Tuple[str, ...],
    date_column: Optional[str],
) -> Dict[str, object]:
    parts = sorted(directory.glob("part-*.parquet"))
    partition = directory.name.split("=", 1)[-1] if "=" in directory.name else None
    if not parts:
        return {
            "dataset": dataset,
            "partition": partition,
            "status": "missing_files",
            "rows": 0,
            "duplicate_keys": None,
            "null_key_rows": None,
            "min_date": None,
            "max_date": None,
            "missing_columns": "",
        }

    schema = set(pq.ParquetFile(parts[0]).schema.names)
    missing = sorted(set(required) - schema)
    available_keys = [column for column in keys if column in schema]
    columns_to_read = list(available_keys)
    if date_column and date_column in schema and date_column not in columns_to_read:
        columns_to_read.append(date_column)

    frames = [
        pd.read_parquet(path, columns=columns_to_read)
        for path in parts
    ]
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    null_keys = (
        int(frame[available_keys].isna().any(axis=1).sum())
        if available_keys
        else None
    )
    duplicates = (
        int(frame.duplicated(available_keys, keep=False).sum())
        if len(available_keys) == len(keys)
        else None
    )
    minimum = (
        frame[date_column].dropna().min()
        if date_column and date_column in frame and not frame[date_column].dropna().empty
        else None
    )
    maximum = (
        frame[date_column].dropna().max()
        if date_column and date_column in frame and not frame[date_column].dropna().empty
        else None
    )
    if missing:
        status = "missing_columns"
    elif frame.empty:
        status = "empty"
    else:
        status = "ok"
    return {
        "dataset": dataset,
        "partition": partition,
        "status": status,
        "rows": len(frame),
        "duplicate_keys": duplicates,
        "null_key_rows": null_keys,
        "min_date": minimum,
        "max_date": maximum,
        "missing_columns": ",".join(missing),
    }


def validate_raw_data(
    raw_dir: Path = RAW_DATA_DIR,
    output_path: Path = OUTPUT_DIR / "tables" / "data_validation.csv",
) -> pd.DataFrame:
    """验证所有已约定数据集并保存明细报告。"""

    records: List[Dict[str, object]] = []
    for dataset, rule in VALIDATION_RULES.items():
        dataset_dir = raw_dir / dataset
        if not dataset_dir.exists():
            records.append(
                {
                    "dataset": dataset,
                    "partition": None,
                    "status": "missing_dataset",
                    "rows": 0,
                    "duplicate_keys": None,
                    "null_key_rows": None,
                    "min_date": None,
                    "max_date": None,
                    "missing_columns": "",
                }
            )
            continue
        for directory in _partition_directories(dataset_dir):
            records.append(
                _validate_partition(
                    dataset=dataset,
                    directory=directory,
                    keys=tuple(rule["keys"]),
                    required=tuple(rule["required"]),
                    date_column=rule.get("date"),
                )
            )

    report = pd.DataFrame(records)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".csv.tmp")
    report.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(output_path)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="验证 Wind 原始数据缓存")
    parser.parse_args(argv)
    report = validate_raw_data()
    failed = report[report["status"].isin(["missing_dataset", "missing_files", "missing_columns", "empty"])]
    return 1 if not failed.empty else 0


if __name__ == "__main__":
    raise SystemExit(main())
