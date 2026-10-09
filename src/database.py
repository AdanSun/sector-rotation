"""financedata 的只读盘点、查询与 Parquet 缓存。

本模块只生成 SELECT / SHOW 查询，不包含任何数据库写入语句。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd
import pymysql
import pyarrow.parquet as pq

from config import (
    DB_CHUNK_SIZE,
    GUOJIN_DATABASE,
    END_DATE,
    FINANCIAL_START_DATE,
    OUTPUT_DIR,
    RAW_DATA_DIR,
    START_DATE,
    get_database_config,
)


LOGGER = logging.getLogger(__name__)
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")


@dataclass(frozen=True)
class DatasetSpec:
    """一个 Wind 原始数据集的定位与提取规则。"""

    name: str
    table_candidates: Tuple[str, ...]
    table_keywords: Tuple[str, ...] = ()
    date_candidates: Tuple[str, ...] = ()
    selected_columns: Tuple[str, ...] = ()
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    partition_by_year: bool = False
    entity_candidates: Tuple[str, ...] = (
        "S_INFO_WINDCODE",
        "S_CON_WINDCODE",
        "S_INFO_COMPCODE",
    )
    required: bool = True
    where_in: Optional[Tuple[str, Tuple[str, ...]]] = None
    key_columns: Tuple[str, ...] = ()


DATASET_SPECS: Tuple[DatasetSpec, ...] = (
    DatasetSpec(
        name="stock_description",
        table_candidates=("AShareDescription",),
        key_columns=("S_INFO_WINDCODE",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "S_INFO_CODE",
            "S_INFO_NAME",
            "S_INFO_COMPNAME",
            "S_INFO_EXCHMARKET",
            "S_INFO_LISTBOARD",
            "S_INFO_LISTDATE",
            "S_INFO_DELISTDATE",
            "S_INFO_LISTBOARDNAME",
            "S_INFO_COMPCODE",
            "OPDATE",
            "OPMODE",
        ),
    ),
    DatasetSpec(
        name="stock_eod_prices",
        table_candidates=("AShareEODPrices",),
        key_columns=("S_INFO_WINDCODE", "TRADE_DT"),
        date_candidates=("TRADE_DT",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_PRECLOSE",
            "S_DQ_OPEN",
            "S_DQ_HIGH",
            "S_DQ_LOW",
            "S_DQ_CLOSE",
            "S_DQ_PCTCHANGE",
            "S_DQ_VOLUME",
            "S_DQ_AMOUNT",
            "S_DQ_ADJPRECLOSE",
            "S_DQ_ADJOPEN",
            "S_DQ_ADJHIGH",
            "S_DQ_ADJLOW",
            "S_DQ_ADJCLOSE",
            "S_DQ_ADJFACTOR",
            "S_DQ_AVGPRICE",
            "S_DQ_TRADESTATUS",
            "S_DQ_TRADESTATUSCODE",
            "S_DQ_STOPPING",
            "S_DQ_ADJCLOSE_BACKWARD",
            "OPDATE",
            "OPMODE",
        ),
        start_date=START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="stock_eod_derivative",
        table_candidates=(
            "AShareEODDerivativeIndicator",
            "AShareEODDerivativeIndicators",
        ),
        table_keywords=("ashare", "eod", "derivative"),
        key_columns=("S_INFO_WINDCODE", "TRADE_DT"),
        date_candidates=("TRADE_DT",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_VAL_MV",
            "S_DQ_MV",
            "S_VAL_PE",
            "S_VAL_PB_NEW",
            "S_VAL_PE_TTM",
            "S_VAL_PS_TTM",
            "S_DQ_TURN",
            "S_DQ_FREETURNOVER",
            "TOT_SHR_TODAY",
            "FLOAT_A_SHR_TODAY",
            "FREE_SHARES_TODAY",
            "S_DQ_CLOSE_TODAY",
            "S_PRICE_DIV_DPS",
            "NET_PROFIT_PARENT_COMP_TTM",
            "NET_PROFIT_PARENT_COMP_LYR",
            "NET_ASSETS_TODAY",
            "NET_CASH_FLOWS_OPER_ACT_TTM",
            "OPER_REV_TTM",
            "OPER_REV_LYR",
            "UP_DOWN_LIMIT_STATUS",
            "OPDATE",
            "OPMODE",
        ),
        start_date=START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="income_statement",
        table_candidates=("AShareIncome",),
        key_columns=("S_INFO_WINDCODE", "REPORT_PERIOD", "ANN_DT", "STATEMENT_TYPE"),
        date_candidates=("REPORT_PERIOD",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "ANN_DT",
            "ACTUAL_ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "TOT_OPER_REV",
            "OPER_REV",
            "OPER_PROFIT",
            "TOT_PROFIT",
            "NET_PROFIT_INCL_MIN_INT_INC",
            "NET_PROFIT_EXCL_MIN_INT_INC",
            "NET_PROFIT_AFTER_DED_NR_LP",
            "EBIT",
            "EBITDA",
            "S_INFO_COMPCODE",
            "OPDATE",
            "OPMODE",
        ),
        start_date=FINANCIAL_START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="balance_sheet",
        table_candidates=("AShareBalanceSheet",),
        key_columns=("S_INFO_WINDCODE", "REPORT_PERIOD", "ANN_DT", "STATEMENT_TYPE"),
        date_candidates=("REPORT_PERIOD",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "ANN_DT",
            "ACTUAL_ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "TOT_ASSETS",
            "TOT_LIAB",
            "TOT_SHRHLDR_EQY_EXCL_MIN_INT",
            "TOT_SHRHLDR_EQY_INCL_MIN_INT",
            "CAP_STK",
            "TOT_SHR",
            "S_INFO_COMPCODE",
            "OPDATE",
            "OPMODE",
        ),
        start_date=FINANCIAL_START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="cashflow_statement",
        table_candidates=("AShareCashFlow",),
        key_columns=("S_INFO_WINDCODE", "REPORT_PERIOD", "ANN_DT", "STATEMENT_TYPE"),
        date_candidates=("REPORT_PERIOD",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "ANN_DT",
            "ACTUAL_ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "NET_CASH_FLOWS_OPER_ACT",
            "NET_CASH_FLOWS_INV_ACT",
            "NET_CASH_FLOWS_FNC_ACT",
            "STOT_CASH_INFLOWS_OPER_ACT",
            "STOT_CASH_OUTFLOWS_OPER_ACT",
            "STOT_CASH_INFLOWS_INV_ACT",
            "STOT_CASH_OUTFLOWS_INV_ACT",
            "STOT_CASH_INFLOWS_FNC_ACT",
            "STOT_CASH_OUTFLOWS_FNC_ACT",
            "FREE_CASH_FLOW",
            "S_INFO_COMPCODE",
            "OPDATE",
            "OPMODE",
        ),
        start_date=FINANCIAL_START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="financial_indicator",
        table_candidates=("AShareFinancialIndicator",),
        key_columns=("S_INFO_WINDCODE", "REPORT_PERIOD", "ANN_DT", "STATEMENT_TYPE"),
        date_candidates=("REPORT_PERIOD",),
        selected_columns=(
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "ANN_DT",
            "REPORT_PERIOD",
            "STATEMENT_TYPE",
            "S_FA_DEDUCTEDPROFIT",
            "S_FA_ROE",
            "S_FA_ROE_DEDUCTED",
            "S_FA_ROE_AVG",
            "WAA_ROE",
            "S_FA_YOYNETPROFIT",
            "S_FA_YOYNETPROFIT_DEDUCTED",
            "S_FA_YOY_TR",
            "S_FA_YOY_OR",
            "S_QFA_YOYNETPROFIT",
            "S_QFA_YOYSALES",
            "S_INFO_COMPCODE",
            "OPDATE",
            "OPMODE",
        ),
        start_date=FINANCIAL_START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="industry_membership",
        table_candidates=("AShareSWNIndustriesClass",),
        table_keywords=("ashare", "swn", "industries", "class"),
        key_columns=("S_INFO_WINDCODE", "SW_IND_CODE", "ENTRY_DT", "REMOVE_DT"),
        selected_columns=(
            "S_INFO_WINDCODE",
            "SW_IND_CODE",
            "ENTRY_DT",
            "REMOVE_DT",
            "CUR_SIGN",
            "OPDATE",
            "OPMODE",
        ),
    ),
    DatasetSpec(
        name="industry_classification",
        table_candidates=(
            "AShareIndustriesCode",
            "IndustriesCode",
            "AShareIndustryCode",
        ),
        table_keywords=("industr", "code"),
        key_columns=("INDUSTRIESCODE",),
    ),
    DatasetSpec(
        name="dividend",
        table_candidates=("AShareDividend",),
        table_keywords=("ashare", "dividend"),
        key_columns=("S_INFO_WINDCODE", "REPORT_PERIOD", "ANN_DT", "EX_DT"),
        date_candidates=("REPORT_PERIOD", "ANN_DT", "EX_DT"),
        selected_columns=(
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "S_DIV_PROGRESS",
            "STK_DVD_PER_SH",
            "CASH_DVD_PER_SH_PRE_TAX",
            "CASH_DVD_PER_SH_AFTER_TAX",
            "EQY_RECORD_DT",
            "EX_DT",
            "DVD_PAYOUT_DT",
            "DVD_ANN_DT",
            "ANN_DT",
            "REPORT_PERIOD",
            "S_DIV_BASESHARE",
            "TOT_CASH_DVD",
            "TOT_SHR",
            "OPDATE",
            "OPMODE",
        ),
        start_date=FINANCIAL_START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="index_description",
        table_candidates=("AIndexDescription",),
        table_keywords=("index", "description"),
        key_columns=("S_INFO_WINDCODE",),
        selected_columns=(
            "S_INFO_WINDCODE", "S_INFO_CODE", "S_INFO_NAME", "S_INFO_COMPNAME",
            "S_INFO_INDEXCODE", "S_INFO_INDEXTYPE", "S_INFO_INDEXSTYLE",
            "S_INFO_PUBLISHER", "OPDATE", "OPMODE",
        ),
    ),
    DatasetSpec(
        name="index_eod_prices",
        table_candidates=(
            "AIndexWindIndustriesEOD",
            "AIndexEODPrices",
        ),
        date_candidates=("TRADE_DT",),
        key_columns=("S_INFO_WINDCODE", "TRADE_DT"),
        selected_columns=(
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_PRECLOSE",
            "S_DQ_OPEN",
            "S_DQ_HIGH",
            "S_DQ_LOW",
            "S_DQ_CLOSE",
            "S_DQ_PCTCHANGE",
            "S_DQ_VOLUME",
            "S_DQ_AMOUNT",
            "OPDATE",
            "OPMODE",
        ),
        start_date=START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
        # 不再只提取万得全A；AIndexWindIndustriesEOD 中的申万行业指数一并保留。
        # 后续回测按行业代码映射筛选，881001.WI 仍作为市场基准。
    ),
    DatasetSpec(
        name="sw_index_eod_prices",
        table_candidates=("ASWSIndexEOD",),
        date_candidates=("TRADE_DT",),
        key_columns=("S_INFO_WINDCODE", "TRADE_DT"),
        selected_columns=(
            "S_INFO_WINDCODE",
            "TRADE_DT",
            "S_DQ_PRECLOSE",
            "S_DQ_OPEN",
            "S_DQ_HIGH",
            "S_DQ_LOW",
            "S_DQ_CLOSE",
            "S_DQ_VOLUME",
            "S_DQ_AMOUNT",
            "OPDATE",
            "OPMODE",
        ),
        start_date=START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
    ),
    DatasetSpec(
        name="consensus",
        table_candidates=(
            "AShareConsensusData",
            "AShareConsensusRollingData",
            "AShareConsensusDetail",
        ),
        table_keywords=("ashare", "consensus"),
        key_columns=("S_INFO_WINDCODE", "EST_DT", "EST_REPORT_DT", "CONSEN_DATA_CYCLE_TYP"),
        date_candidates=(
            "EST_DT",
            "EST_DATE",
            "TRADE_DT",
            "ANN_DT",
            "RATING_DT",
        ),
        selected_columns=(
            "S_INFO_WINDCODE",
            "WIND_CODE",
            "EST_DT",
            "EST_REPORT_DT",
            "NUM_EST_INST",
            "MAIN_BUS_INC_AVG",
            "MAIN_BUS_INC_MEDIAN",
            "NET_PROFIT_AVG",
            "NET_PROFIT_MEDIAN",
            "NET_PROFIT_DEV",
            "NET_PROFIT_MAX",
            "NET_PROFIT_MIN",
            "NET_PROFIT_UPGRADE",
            "NET_PROFIT_DOWNGRADE",
            "NET_PROFIT_MAINTAIN",
            "CONSEN_DATA_CYCLE_TYP",
            "S_EST_NETPROFITINSTNUM",
            "S_EST_YEARTYPE",
            "OPDATE",
            "OPMODE",
        ),
        start_date=START_DATE,
        end_date=END_DATE,
        partition_by_year=True,
        required=False,
    ),
)


def quote_identifier(identifier: str) -> str:
    """安全引用 MySQL 标识符。"""

    if not IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"非法 SQL 标识符：{identifier!r}")
    return f"`{identifier}`"


@contextmanager
def get_connection() -> Iterator[pymysql.connections.Connection]:
    """创建并可靠关闭数据库连接。"""

    connection_config = get_database_config()
    connection_config["cursorclass"] = pymysql.cursors.SSCursor
    connection = pymysql.connect(**connection_config)
    try:
        yield connection
    finally:
        connection.close()


def fetch_dataframe(
    connection: pymysql.connections.Connection,
    sql: str,
    params: Optional[Sequence[object]] = None,
) -> pd.DataFrame:
    """执行一条只读查询并返回 DataFrame。"""

    stripped = sql.lstrip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("SHOW")):
        raise ValueError("数据库模块仅允许 SELECT 或 SHOW 查询")
    return pd.read_sql_query(sql, connection, params=params)


def list_tables(connection: pymysql.connections.Connection) -> pd.DataFrame:
    """返回当前数据库的表清单。"""

    sql = """
        SELECT
            TABLE_NAME,
            TABLE_TYPE,
            ENGINE,
            TABLE_ROWS,
            CREATE_TIME,
            UPDATE_TIME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME
    """
    return fetch_dataframe(connection, sql, (GUOJIN_DATABASE,))


def list_columns(connection: pymysql.connections.Connection) -> pd.DataFrame:
    """返回当前数据库全部字段。"""

    sql = """
        SELECT
            TABLE_NAME,
            ORDINAL_POSITION,
            COLUMN_NAME,
            COLUMN_TYPE,
            IS_NULLABLE,
            COLUMN_KEY,
            COLUMN_DEFAULT,
            COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """
    return fetch_dataframe(connection, sql, (GUOJIN_DATABASE,))


def list_indexes(connection: pymysql.connections.Connection) -> pd.DataFrame:
    """返回当前数据库全部索引。"""

    sql = """
        SELECT
            TABLE_NAME,
            INDEX_NAME,
            SEQ_IN_INDEX,
            COLUMN_NAME,
            NON_UNIQUE
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
    """
    return fetch_dataframe(connection, sql, (GUOJIN_DATABASE,))


def _casefold_lookup(values: Iterable[str]) -> Dict[str, str]:
    return {str(value).casefold(): str(value) for value in values}


def resolve_table_name(
    available_tables: Iterable[str],
    candidates: Sequence[str],
    keywords: Sequence[str] = (),
) -> Optional[str]:
    """按候选名称优先、关键词其次定位真实表名。"""

    names = list(available_tables)
    lookup = _casefold_lookup(names)
    for candidate in candidates:
        match = lookup.get(candidate.casefold())
        if match:
            return match

    normalized_keywords = tuple(keyword.casefold() for keyword in keywords)
    matches = [
        name
        for name in names
        if normalized_keywords
        and all(keyword in name.casefold() for keyword in normalized_keywords)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_column_name(
    available_columns: Iterable[str],
    candidates: Sequence[str],
) -> Optional[str]:
    """大小写不敏感地定位真实字段名。"""

    lookup = _casefold_lookup(available_columns)
    for candidate in candidates:
        match = lookup.get(candidate.casefold())
        if match:
            return match
    return None


def year_ranges(start_date: str, end_date: str) -> List[Tuple[int, str, str]]:
    """将闭区间日期拆成年度区间。"""

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")
    result: List[Tuple[int, str, str]] = []
    for year in range(start.year, end.year + 1):
        lower = max(start, date(year, 1, 1))
        upper = min(end, date(year, 12, 31))
        result.append((year, lower.strftime("%Y%m%d"), upper.strftime("%Y%m%d")))
    return result


def _write_csv_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def inventory_database(
    connection: pymysql.connections.Connection,
    output_dir: Path = OUTPUT_DIR / "tables",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """盘点表、字段和索引并保存 CSV。"""

    tables = list_tables(connection)
    columns = list_columns(connection)
    indexes = list_indexes(connection)
    _write_csv_atomic(tables, output_dir / "database_tables.csv")
    _write_csv_atomic(columns, output_dir / "database_columns.csv")
    _write_csv_atomic(indexes, output_dir / "database_indexes.csv")

    inventory = columns.merge(
        tables[["TABLE_NAME", "TABLE_TYPE", "TABLE_ROWS"]],
        on="TABLE_NAME",
        how="left",
    )
    _write_csv_atomic(inventory, output_dir / "data_inventory.csv")
    return tables, columns, indexes


def _remove_existing_parts(directory: Path) -> None:
    for path in directory.glob("part-*.parquet"):
        path.unlink()


def _extract_query_to_parquet(
    connection: pymysql.connections.Connection,
    sql: str,
    params: Sequence[object],
    output_dir: Path,
    overwrite: bool,
    chunk_size: int,
    key_columns: Sequence[str] = (),
    merge_existing: bool = False,
) -> int:
    """分块读取查询结果，可与旧分区去重合并后原子写回。"""

    existing = sorted(output_dir.glob("part-*.parquet"))
    if existing and not overwrite and not merge_existing:
        return sum(pq.ParquetFile(path).metadata.num_rows for path in existing)

    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        chunks = pd.read_sql_query(
            sql,
            connection,
            params=params,
            chunksize=chunk_size,
        )
        if not merge_existing:
            targets: List[Path] = []
            row_count = 0
            for part_number, frame in enumerate(chunks):
                row_count += len(frame)
                target = output_dir / f"part-{part_number:05d}.parquet"
                temporary = output_dir / f".{target.name}.tmp"
                frame.to_parquet(temporary, index=False)
                targets.append(target)
            for target in targets:
                (output_dir / f".{target.name}.tmp").replace(target)
            for stale in existing:
                if stale not in targets:
                    stale.unlink()
            return row_count

        new_chunks = list(chunks)
        frames: List[pd.DataFrame] = []
        frames.extend(pd.read_parquet(path) for path in existing)
        frames.extend(new_chunks)
        merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        available_keys = [column for column in key_columns if column in merged.columns]
        if available_keys and not merged.empty:
            merged = merged.drop_duplicates(available_keys, keep="last")

        targets: List[Path] = []
        for part_number, start in enumerate(range(0, len(merged), chunk_size)):
            frame = merged.iloc[start : start + chunk_size]
            target = output_dir / f"part-{part_number:05d}.parquet"
            temporary = output_dir / f".{target.name}.tmp"
            frame.to_parquet(temporary, index=False)
            targets.append(target)
        for target in targets:
            temporary = output_dir / f".{target.name}.tmp"
            temporary.replace(target)
        for stale in existing:
            if stale not in targets:
                stale.unlink()
    except Exception:
        for temporary in output_dir.glob(".*.tmp"):
            temporary.unlink()
        raise
    return len(merged)


def _parquet_partition_stats(
    directory: Path,
    date_column: Optional[str],
    entity_column: Optional[str],
) -> Dict[str, object]:
    """汇总单个缓存分区的行数、日期范围和实体数量。"""

    parts = sorted(directory.glob("part-*.parquet"))
    if not parts:
        return {
            "row_count": 0,
            "min_date": None,
            "max_date": None,
            "entity_count": None,
        }

    rows = sum(pq.ParquetFile(path).metadata.num_rows for path in parts)
    minimum = None
    maximum = None
    entities = set()
    requested_columns = [
        column for column in (date_column, entity_column) if column
    ]
    for path in parts:
        if not requested_columns:
            break
        frame = pd.read_parquet(path, columns=requested_columns)
        if date_column and date_column in frame:
            values = frame[date_column].dropna()
            if not values.empty:
                part_min = values.min()
                part_max = values.max()
                minimum = part_min if minimum is None else min(minimum, part_min)
                maximum = part_max if maximum is None else max(maximum, part_max)
        if entity_column and entity_column in frame:
            entities.update(frame[entity_column].dropna().astype(str).unique())

    return {
        "row_count": rows,
        "min_date": minimum,
        "max_date": maximum,
        "entity_count": len(entities) if entity_column else None,
    }


def _extract_partition(
    connection: pymysql.connections.Connection,
    table_name: str,
    selected_columns: Sequence[str],
    date_column: Optional[str],
    lower: Optional[str],
    upper: Optional[str],
    where_in: Optional[Tuple[str, Tuple[str, ...]]],
    output_dir: Path,
    overwrite: bool,
    chunk_size: int,
    key_columns: Sequence[str] = (),
    merge_existing: bool = False,
) -> int:
    table_sql = quote_identifier(table_name)
    select_sql = ", ".join(
        quote_identifier(column) for column in selected_columns
    )

    clauses: List[str] = []
    params_list: List[object] = []
    if date_column and lower and upper:
        column_sql = quote_identifier(date_column)
        clauses.append(f"{column_sql} >= %s AND {column_sql} <= %s")
        params_list.extend((lower, upper))
    if where_in:
        filter_column, values = where_in
        placeholders = ", ".join(["%s"] * len(values))
        clauses.append(
            f"{quote_identifier(filter_column)} IN ({placeholders})"
        )
        params_list.extend(values)

    sql = f"SELECT {select_sql} FROM {table_sql}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return _extract_query_to_parquet(
        connection,
        sql,
        params_list,
        output_dir,
        overwrite,
        chunk_size,
        key_columns,
        merge_existing,
    )


def _extract_partition_by_month(
    connection: pymysql.connections.Connection,
    table_name: str,
    selected_columns: Sequence[str],
    date_column: str,
    lower: str,
    upper: str,
    where_in: Optional[Tuple[str, Tuple[str, ...]]],
    output_dir: Path,
    chunk_size: int,
) -> int:
    """将大型日频年分区拆成月度查询，避免长时间单查询被服务端断开。"""

    start = pd.to_datetime(lower, format="%Y%m%d")
    end = pd.to_datetime(upper, format="%Y%m%d")
    existing = sorted(output_dir.glob("part-*.parquet"))
    output_dir.mkdir(parents=True, exist_ok=True)
    targets: List[Path] = []
    row_count = 0
    part_number = 0
    try:
        for month_start in pd.date_range(start.to_period("M").start_time, end, freq="MS"):
            month_lower = max(start, month_start)
            month_upper = min(end, month_start + pd.offsets.MonthEnd(0))
            table_sql = quote_identifier(table_name)
            select_sql = ", ".join(quote_identifier(column) for column in selected_columns)
            clauses = [f"{quote_identifier(date_column)} >= %s AND {quote_identifier(date_column)} <= %s"]
            params: List[object] = [month_lower.strftime("%Y%m%d"), month_upper.strftime("%Y%m%d")]
            if where_in:
                filter_column, values = where_in
                clauses.append(f"{quote_identifier(filter_column)} IN ({', '.join(['%s'] * len(values))})")
                params.extend(values)
            sql = f"SELECT {select_sql} FROM {table_sql} WHERE " + " AND ".join(clauses)
            for frame in pd.read_sql_query(sql, connection, params=params, chunksize=chunk_size):
                row_count += len(frame)
                target = output_dir / f"part-{part_number:05d}.parquet"
                frame.to_parquet(output_dir / f".{target.name}.tmp", index=False)
                targets.append(target)
                part_number += 1
        for target in targets:
            (output_dir / f".{target.name}.tmp").replace(target)
        for stale in existing:
            if stale not in targets:
                stale.unlink()
    except Exception:
        for temporary in output_dir.glob(".*.tmp"):
            temporary.unlink()
        raise
    return row_count


# 用于探测"国金库当前数据最新日期"的代表性表与候选日期列
_LATEST_DATE_PROBES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ashareeodprices", ("TRADE_DT",)),
    ("aindexeodprices", ("TRADE_DT",)),
    ("ashareincome", ("REPORT_PERIOD",)),
    ("ashareconsensusdata", ("REPORT_DATE", "CONSENSUS_DATE", "PUBLISH_DATE")),
)

# 增量提取后记录"数据库当前最新日期"的文件，供模型 end-month 跟随
LATEST_DATE_FILE = RAW_DATA_DIR / ".latest_end_date.txt"


def _write_latest_end_date(end_date: str) -> None:
    """记录国金库当前最新数据日期（YYYY-MM-DD）。"""

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_DATE_FILE.write_text(end_date.strip(), encoding="utf-8")


def detect_latest_database_date(
    connection: pymysql.connections.Connection,
    columns_by_table: Dict[str, List[str]],
    table_names: Sequence[str],
) -> Optional[str]:
    """探测国金库关键表的最新日期（YYYY-MM-DD）。

    让增量提取的查询上界跟随数据库实际最新数据，而不是固定的 END_DATE。
    探测失败返回 None（调用方回退到配置的 END_DATE）。
    """

    latest: Optional[pd.Timestamp] = None
    for table, candidates in _LATEST_DATE_PROBES:
        resolved_table = resolve_table_name(table_names, (table,))
        if resolved_table is None:
            continue
        date_col = resolve_column_name(
            columns_by_table.get(resolved_table, []),
            candidates,
        )
        if date_col is None:
            continue
        sql = (
            f"SELECT MAX({quote_identifier(date_col)}) AS mx "
            f"FROM {quote_identifier(resolved_table)}"
        )
        try:
            frame = fetch_dataframe(connection, sql)
            value = frame["mx"].iloc[0]
            if pd.notna(value):
                parsed = pd.to_datetime(str(value), errors="coerce")
                if pd.notna(parsed) and (latest is None or parsed > latest):
                    latest = parsed
        except Exception:
            continue
    if latest is None:
        return None
    return latest.strftime("%Y-%m-%d")


def extract_datasets(
    connection: pymysql.connections.Connection,
    specs: Sequence[DatasetSpec] = DATASET_SPECS,
    raw_dir: Path = RAW_DATA_DIR,
    overwrite: bool = False,
    chunk_size: int = DB_CHUNK_SIZE,
) -> pd.DataFrame:
    """按配置提取所有原始数据，并返回覆盖报告。"""

    tables = list_tables(connection)
    columns = list_columns(connection)
    table_names = tables["TABLE_NAME"].astype(str).tolist()
    columns_by_table = {
        str(name): group["COLUMN_NAME"].astype(str).tolist()
        for name, group in columns.groupby("TABLE_NAME", sort=False)
    }

    # 动态上界：探测国金库当前最新日期，并让增量提取跟随（而非固定 END_DATE）
    probe_end = detect_latest_database_date(connection, columns_by_table, table_names)
    if probe_end:
        _write_latest_end_date(probe_end)
        specs = [
            replace(spec, end_date=probe_end)
            if probe_end and spec.end_date and probe_end > spec.end_date
            else spec
            for spec in specs
        ]

    coverage_rows: List[Dict[str, object]] = []
    refreshed_any = False

    for spec in specs:
        table_name = resolve_table_name(
            table_names,
            spec.table_candidates,
            spec.table_keywords,
        )
        if table_name is None:
            coverage_rows.append(
                {
                    "dataset": spec.name,
                    "status": "missing_table",
                    "required": spec.required,
                    "table_name": None,
                    "date_column": None,
                    "partition": None,
                    "row_count": 0,
                    "min_date": None,
                    "max_date": None,
                    "entity_count": None,
                    "message": "未找到唯一匹配表",
                }
            )
            continue

        date_column = resolve_column_name(
            columns_by_table.get(table_name, []),
            spec.date_candidates,
        )
        if spec.partition_by_year and date_column is None:
            coverage_rows.append(
                {
                    "dataset": spec.name,
                    "status": "missing_date_column",
                    "required": spec.required,
                    "table_name": table_name,
                    "date_column": None,
                    "partition": None,
                    "row_count": 0,
                    "min_date": None,
                    "max_date": None,
                    "entity_count": None,
                    "message": "无法安全按年份提取",
                }
            )
            continue

        available_columns = columns_by_table.get(table_name, [])
        if spec.selected_columns:
            selected_columns = [
                resolved
                for requested in spec.selected_columns
                if (
                    resolved := resolve_column_name(
                        available_columns,
                        (requested,),
                    )
                )
            ]
        else:
            selected_columns = available_columns
        if not selected_columns:
            coverage_rows.append(
                {
                    "dataset": spec.name,
                    "status": "missing_selected_columns",
                    "required": spec.required,
                    "table_name": table_name,
                    "date_column": date_column,
                    "partition": None,
                    "row_count": 0,
                    "min_date": None,
                    "max_date": None,
                    "entity_count": None,
                    "column_count": 0,
                    "message": "没有可提取字段",
                }
            )
            continue

        resolved_where_in = spec.where_in
        if spec.where_in:
            requested_filter, values = spec.where_in
            actual_filter = resolve_column_name(
                available_columns,
                (requested_filter,),
            )
            if actual_filter is None:
                coverage_rows.append(
                    {
                        "dataset": spec.name,
                        "status": "missing_filter_column",
                        "required": spec.required,
                        "table_name": table_name,
                        "date_column": date_column,
                        "partition": None,
                        "row_count": 0,
                        "min_date": None,
                        "max_date": None,
                        "entity_count": None,
                        "column_count": len(selected_columns),
                        "message": f"缺少筛选字段 {requested_filter}",
                    }
                )
                continue
            resolved_where_in = (actual_filter, values)

        partitions: List[Tuple[Optional[int], Optional[str], Optional[str]]]
        if spec.partition_by_year:
            assert spec.start_date and spec.end_date
            partitions = [
                (year, lower, upper)
                for year, lower, upper in year_ranges(
                    spec.start_date,
                    spec.end_date,
                )
            ]
        else:
            partitions = [(None, None, None)]

        entity_column = resolve_column_name(
            columns_by_table.get(table_name, []),
            spec.entity_candidates,
        )
        for year, lower, upper in partitions:
            destination = raw_dir / spec.name
            if year is not None:
                destination = destination / f"year={year}"
            try:
                existing_stats = _parquet_partition_stats(destination, date_column, entity_column)
                has_cache = bool(existing_stats["row_count"])
                query_lower = lower
                merge_existing = False
                should_query = overwrite or not has_cache or not spec.partition_by_year

                if spec.partition_by_year and has_cache and not overwrite:
                    assert upper is not None and year is not None
                    cached_max = pd.to_datetime(existing_stats["max_date"], errors="coerce")
                    upper_date = pd.to_datetime(upper, format="%Y%m%d")
                    is_financial = spec.name in {
                        "income_statement",
                        "balance_sheet",
                        "cashflow_statement",
                        "financial_indicator",
                    }
                    financial_refresh_start = (
                        pd.Timestamp(spec.end_date) - pd.DateOffset(months=24)
                    ).year - 1
                    if is_financial and year >= financial_refresh_start:
                        # 年报可在次年公告；多回看一个报告年，
                        # 保证 ANN_DT 至少 24 个月的重述能被合并。
                        should_query = True
                        merge_existing = True
                    elif pd.notna(cached_max) and cached_max < upper_date:
                        should_query = True
                        merge_existing = True
                        query_lower = (cached_max + pd.Timedelta(days=1)).strftime("%Y%m%d")
                    else:
                        should_query = False

                if should_query:
                    for attempt in range(2):
                        try:
                            connection.ping(reconnect=True)
                            use_monthly_slices = (
                                not has_cache
                                and bool(date_column and query_lower and upper)
                                and spec.name in {"stock_eod_prices", "stock_eod_derivative"}
                            )
                            if use_monthly_slices:
                                _extract_partition_by_month(
                                    connection,
                                    table_name,
                                    selected_columns,
                                    date_column,
                                    query_lower,
                                    upper,
                                    resolved_where_in,
                                    destination,
                                    chunk_size,
                                )
                            else:
                                _extract_partition(
                                    connection=connection,
                                    table_name=table_name,
                                    selected_columns=selected_columns,
                                    date_column=date_column,
                                    lower=query_lower,
                                    upper=upper,
                                    where_in=resolved_where_in,
                                    output_dir=destination,
                                    overwrite=overwrite or not merge_existing,
                                    chunk_size=chunk_size,
                                    key_columns=[
                                        resolved
                                        for key in spec.key_columns
                                        if (resolved := resolve_column_name(selected_columns, (key,)))
                                    ],
                                    merge_existing=merge_existing,
                                )
                            break
                        except Exception:
                            if attempt:
                                raise
                            LOGGER.warning(
                                "分区提取中断，重连后重试 dataset=%s year=%s",
                                spec.name,
                                year,
                            )
                            try:
                                connection.close()
                            except Exception:
                                pass
                            connection_config = get_database_config()
                            connection_config["cursorclass"] = pymysql.cursors.SSCursor
                            connection = pymysql.connect(**connection_config)
                    refreshed_any = True
                stats = _parquet_partition_stats(
                    destination,
                    date_column,
                    entity_column,
                )
                if stats["row_count"] == 0:
                    status = "empty"
                    message = "查询范围内没有数据"
                else:
                    status = "ok"
                    message = ""
            except Exception as exc:
                LOGGER.exception(
                    "提取失败 dataset=%s table=%s year=%s",
                    spec.name,
                    table_name,
                    year,
                )
                stats = {
                    "row_count": 0,
                    "min_date": None,
                    "max_date": None,
                    "entity_count": None,
                }
                status = "error"
                message = f"{type(exc).__name__}: {exc}"
            coverage_rows.append(
                {
                    "dataset": spec.name,
                    "status": status,
                    "required": spec.required,
                    "table_name": table_name,
                    "date_column": date_column,
                    "partition": year,
                    **stats,
                    "column_count": len(selected_columns),
                    "message": message,
                }
            )

    coverage = pd.DataFrame(coverage_rows)
    _write_csv_atomic(coverage, OUTPUT_DIR / "tables" / "data_coverage.csv")
    if refreshed_any:
        _invalidate_processed_cache()
    return coverage


def _invalidate_processed_cache() -> None:
    """原始数据刷新后移除所有可重建的 processed 面板。"""

    from config import PROCESSED_DATA_DIR

    if not PROCESSED_DATA_DIR.exists():
        return
    for path in PROCESSED_DATA_DIR.iterdir():
        if path.name == ".gitkeep":
            continue
        if path.is_file():
            path.unlink()


def run_inventory_and_extract(overwrite: bool = False) -> pd.DataFrame:
    """执行完整盘点与原始数据提取。"""

    with get_connection() as connection:
        inventory_database(connection)
        return extract_datasets(connection, overwrite=overwrite)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wind 落库只读取数")
    parser.add_argument(
        "--step",
        choices=("inventory", "extract", "all"),
        default="all",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--dataset",
        action="append",
        help="只处理指定数据集；可重复传入",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        with get_connection() as connection:
            if args.step in ("inventory", "all"):
                inventory_database(connection)
            if args.step in ("extract", "all"):
                specs = DATASET_SPECS
                if args.dataset:
                    requested = set(args.dataset)
                    specs = tuple(
                        spec
                        for spec in DATASET_SPECS
                        if spec.name in requested
                    )
                    unknown = requested - {spec.name for spec in specs}
                    if unknown:
                        names = ", ".join(sorted(unknown))
                        raise ValueError(f"未知数据集：{names}")
                coverage = extract_datasets(
                    connection,
                    specs=specs,
                    overwrite=args.overwrite,
                )
                failed_required = coverage[
                    coverage["required"].fillna(False)
                    & ~coverage["status"].isin({"ok", "empty"})
                ]
                return 1 if not failed_required.empty else 0
    except pymysql.MySQLError as exc:
        LOGGER.error("无法连接或查询 financedata：%s", exc)
        failure = pd.DataFrame(
            [
                {
                    "dataset": "database",
                    "status": "database_unavailable",
                    "required": True,
                    "table_name": None,
                    "date_column": None,
                    "partition": None,
                    "row_count": 0,
                    "min_date": None,
                    "max_date": None,
                    "entity_count": None,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ]
        )
        _write_csv_atomic(
            failure,
            OUTPUT_DIR / "tables" / "data_coverage.csv",
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
