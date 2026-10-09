"""数据仓库层。

统一负责从本地白名单文件中读取 Parquet/CSV，并提供：
- 按文件 mtime 失效的轻量缓存
- 白名单路径校验（防路径穿越）
- NaN/Inf 清洗（避免非法 JSON）
- 统一的日期归一化
"""

from __future__ import annotations

import math
import threading
import time
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from . import config

_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_LOCK = threading.Lock()


class DataFileError(RuntimeError):
    """数据文件缺失、损坏或字段不符合预期。"""


def _safe_value(value: Any) -> Any:
    """将 NaN/Inf/NaT 转为 None，其余原样返回，保证 JSON 合法。"""

    if value is None:
        return None
    if isinstance(value, (float, np.floating)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return None
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def clean_series(series: pd.Series) -> pd.Series:
    """把 Pandas 列中 NaN/Inf 替换为 None，便于逐行转 dict。"""

    return series.where(pd.notna(series) & ~np.isinf(series).replace({pd.NA: True}), None)


def _frame_to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """把 DataFrame 转成可 JSON 化的记录列表。"""

    frame = frame.replace([np.inf, -np.inf], np.nan)
    records = frame.to_dict(orient="records")
    return [{key: _safe_value(value) for key, value in row.items()} for row in records]


def load_frame(key: str) -> pd.DataFrame:
    """读取白名单内的 Parquet/CSV，带 mtime 缓存。"""

    if key in config.PARQUET_FILES:
        path = config.PARQUET_FILES[key]
        loader = pd.read_parquet
    elif key in config.CSV_FILES:
        path = config.CSV_FILES[key]
        loader = lambda p: pd.read_csv(p, encoding="utf-8-sig")  # noqa: E731
    else:
        raise DataFileError(f"未知数据文件：{key}")

    if not path.exists():
        raise DataFileError(f"数据文件不存在：{path.name}")

    mtime = path.stat().st_mtime
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1].copy()

    frame = loader(path)
    frame = frame.replace([np.inf, -np.inf], np.nan)
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, frame.copy())
    return frame


def file_mtime(key: str) -> float:
    """返回白名单文件的修改时间（Unix 秒），文件不存在返回 0。"""

    path = config.PARQUET_FILES.get(key) or config.CSV_FILES.get(key)
    if path is None or not path.exists():
        return 0.0
    return path.stat().st_mtime


def file_exists(key: str) -> bool:
    path = config.PARQUET_FILES.get(key) or config.CSV_FILES.get(key)
    return path is not None and path.exists()


def get_file(key: str) -> PathType:
    path = config.PARQUET_FILES.get(key) or config.CSV_FILES.get(key)
    if path is None:
        raise DataFileError(f"未知数据文件：{key}")
    return path


from pathlib import Path as PathType  # noqa: E402


def parse_month(value: str | None) -> str | None:
    """把 YYYY-MM 或 YYYY-MM-DD 统一归一化为月末日期字符串 YYYY-MM-DD。

    返回 None 表示未提供；抛出 ValueError 表示格式非法。
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = pd.Timestamp(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"月份格式非法：{value}，应为 YYYY-MM 或 YYYY-MM-DD") from exc
    return parsed.to_period("M").to_timestamp("M").strftime("%Y-%m-%d")


def latest_settled_index(frame: pd.DataFrame) -> int | None:
    """返回收益列最后一个有效行的位置（-1 表示最后一行为空收益）。"""

    if frame.empty:
        return None
    valid = frame["valid_return"].dropna() if "valid_return" in frame.columns else None
    if valid is None or valid.empty:
        return len(frame) - 1
    return frame.index.get_loc(valid.index[-1])
