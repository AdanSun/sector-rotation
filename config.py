"""
项目基础配置。
数据库密码等敏感信息从环境变量读取，不在本文件中保存。
"""

from __future__ import annotations

import os
import hashlib
from datetime import datetime, timedelta
from pathlib import Path


# 项目路径
PROJECT_ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """加载简单的 KEY=VALUE 环境文件，不覆盖终端中已有的环境变量。"""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_DIR = PROJECT_ROOT / "output"
INDUSTRY_OUTPUT_LEVEL = int(os.getenv("INDUSTRY_OUTPUT_LEVEL", "2"))
if INDUSTRY_OUTPUT_LEVEL not in (2, 3):
    raise ValueError("INDUSTRY_OUTPUT_LEVEL 只能是 2 或 3")


# 样本区间
# 2007 年起计算指标，给 2009 年开始的正式回测预留历史窗口。
START_DATE = "2007-01-01"
FINANCIAL_START_DATE = "2005-01-01"
BACKTEST_START_DATE = "2009-01-01"


# 模型数据截止日：默认动态跟随国金库最新数据（完整月末）。
# 数据来源是 src.database.extract 在增量提取后写入的 data/raw/.latest_end_date.txt；
# 可用环境变量 DATA_END_DATE 显式覆盖；文件缺失时回退到静态值。
# 说明：这只会改变模型覆盖的数据时间范围，不改变任何模型计算口径。
DEFAULT_END_DATE = "2026-06-30"


def _effective_end_date() -> str:
    override = os.getenv("DATA_END_DATE")
    if override:
        return override.strip()
    marker = RAW_DATA_DIR / ".latest_end_date.txt"
    if marker.exists():
        try:
            text = marker.read_text(encoding="utf-8").strip()
            year, month, day = (int(part) for part in text[:10].split("-"))
            if month == 12:
                month_end = datetime(year, 12, 31)
            else:
                month_end = datetime(year, month + 1, 1) - timedelta(days=1)
            # 最新日期距月末超过 10 天（该月未走完）时回退到上一月末
            if (month_end - datetime(year, month, day)).days > 10:
                month_end = datetime(year, month, 1) - timedelta(days=1)
            return month_end.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    return DEFAULT_END_DATE


END_DATE = _effective_end_date()


# 数据库连接
DB_HOST = os.getenv("FINANCEDATA_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("FINANCEDATA_PORT", "3306"))
DB_USER = os.getenv("FINANCEDATA_USER")
DB_PASSWORD = os.getenv("FINANCEDATA_PASSWORD")
DB_NAME = os.getenv("FINANCEDATA_DATABASE", "financedata")
GUOJIN_HOST = os.getenv("GUOJIN_HOST", DB_HOST)
GUOJIN_PORT = int(os.getenv("GUOJIN_PORT", str(DB_PORT)))
GUOJIN_USER = os.getenv("GUOJIN_USER", DB_USER)
GUOJIN_PASSWORD = os.getenv("GUOJIN_PASSWORD", DB_PASSWORD)
GUOJIN_DATABASE = os.getenv("GUOJIN_DATABASE", DB_NAME)
DB_CHARSET = "utf8mb4"
DB_CONNECT_TIMEOUT = 10
DB_READ_TIMEOUT = int(os.getenv("FINANCEDATA_READ_TIMEOUT", "900"))
DB_CHUNK_SIZE = 100_000


# 第一版复现的基础参数
TOP_N = 5
TRANSACTION_COST = 0.001
GROWTH_SPREAD_MA = 6
FORECAST_SPREAD_MA = 12
ROE_STD_WINDOW = 12
BETA_WINDOW = 120


def get_database_config(use_guojin: bool = True) -> dict[str, object]:
    """返回可直接传给 ``pymysql.connect`` 的连接参数。

    在真正连接前才检查账号密码，使不访问数据库的模块仍可正常导入。
    """

    user = GUOJIN_USER if use_guojin else DB_USER
    password = GUOJIN_PASSWORD if use_guojin else DB_PASSWORD
    missing = [
        name
        for name, value in {
            "GUOJIN_USER/FINANCEDATA_USER": user,
            "GUOJIN_PASSWORD/FINANCEDATA_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        variables = ", ".join(missing)
        raise RuntimeError(f"缺少数据库环境变量：{variables}")

    return {
        "host": GUOJIN_HOST if use_guojin else DB_HOST,
        "port": GUOJIN_PORT if use_guojin else DB_PORT,
        "user": user,
        "password": password,
        "database": GUOJIN_DATABASE if use_guojin else DB_NAME,
        "charset": DB_CHARSET,
        "connect_timeout": DB_CONNECT_TIMEOUT,
        "read_timeout": DB_READ_TIMEOUT,
        "autocommit": True,
    }


def raw_data_signature(dataset_names: tuple[str, ...]) -> str:
    """返回原始数据覆盖签名，用于让 processed 缓存随 raw 变化失效。"""

    digest = hashlib.sha256()
    for dataset in sorted(dataset_names):
        root = RAW_DATA_DIR / dataset
        digest.update(dataset.encode("utf-8"))
        for path in sorted(root.rglob("*.parquet")) if root.exists() else ():
            stat = path.stat()
            digest.update(str(path.relative_to(RAW_DATA_DIR)).encode("utf-8"))
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    return digest.hexdigest()[:16]
