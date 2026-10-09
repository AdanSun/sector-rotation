"""
产业生命周期分类。
Dickinson 现金流符号法的核心步骤：

1. 逐季度将上市公司现金流量表还原为单季现金流，滚动 4 个季度得到 TTM；
2. 按月末时点，取该月末之前已公告的最近报告期 TTM（避免未来函数）；
3. 将个股按月末申万行业历史归属加总为行业整体现金流；
4. 根据 CFO / CFI / CFF 的正负号映射到转型、成长、成熟、停滞、衰退五个阶段。
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


LOGGER = logging.getLogger(__name__)

CFO_COL = "NET_CASH_FLOWS_OPER_ACT"
CFI_COL = "NET_CASH_FLOWS_INV_ACT"
CFF_COL = "NET_CASH_FLOWS_FNC_ACT"

ANNUAL_OUTPUT = PROCESSED_DATA_DIR / "industry_lifecycle_annual.parquet"
MONTHLY_OUTPUT = PROCESSED_DATA_DIR / "industry_lifecycle_monthly.parquet"
CACHE_META_OUTPUT = PROCESSED_DATA_DIR / "industry_lifecycle_cache_meta.json"
STAGE_COUNTS_OUTPUT = OUTPUT_DIR / "tables" / "lifecycle_stage_counts.csv"
HYBRID_MAP_OUTPUT = OUTPUT_DIR / "tables" / "hybrid_sw_industry_map.csv"


@dataclass(frozen=True)
class LifecycleConfig:
    """生命周期划分参数。"""

    industry_scheme: str = "hybrid"
    industry_level: int = 4
    min_stocks: int = 3
    min_tertiary_stocks: int = 8
    target_industries: Optional[int] = None
    start_date: str = START_DATE
    end_date: str = END_DATE


def config_fingerprint(config: LifecycleConfig) -> str:
    """返回当前划分参数的稳定哈希，用于判断磁盘缓存是否仍然有效。"""

    payload = json.dumps(asdict(config), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parse_wind_date(series: pd.Series) -> pd.Series:
    """解析 Wind 常见的 YYYYMMDD 字符串日期。"""

    return pd.to_datetime(series.astype("string"), format="%Y%m%d", errors="coerce")


def read_parquet_dataset(dataset: str, columns: Optional[list[str]] = None) -> pd.DataFrame:
    """读取 data/raw 下的一个 Parquet 数据集。"""

    path = RAW_DATA_DIR / dataset
    if not path.exists():
        raise FileNotFoundError(f"缺少原始数据目录：{path}")
    return pd.read_parquet(path, columns=columns)


def cashflow_sign(value: float) -> str:
    """将现金流数值转为 Dickinson 符号。

    这里不把 0 强行归为正或负。行业聚合值刚好为 0 时信息含量较弱，
    后续会分类为 unknown，避免制造虚假的生命周期状态。
    """

    if pd.isna(value) or value == 0:
        return "0"
    return "+" if value > 0 else "-"


def classify_dickinson(cfo_sign: str, cfi_sign: str, cff_sign: str) -> str:
    """按 Dickinson 八种现金流符号组合划分生命周期。"""

    pattern = (cfo_sign, cfi_sign, cff_sign)
    mapping = {
        ("-", "-", "+"): "pivoting",
        ("+", "-", "+"): "growth",
        ("+", "-", "-"): "mature",
        ("-", "-", "-"): "stagnation",
        ("+", "+", "+"): "stagnation",
        ("+", "+", "-"): "stagnation",
        ("-", "+", "+"): "decline",
        ("-", "+", "-"): "decline",
    }
    return mapping.get(pattern, "unknown")


def stage_name_zh(stage: str) -> str:
    """生命周期英文标签转中文。"""

    return {
        "pivoting": "转型期",
        "growth": "成长期",
        "mature": "成熟期",
        "stagnation": "停滞期",
        "decline": "衰退期",
        "unknown": "未知",
    }.get(stage, "未知")


def statement_priority(statement_type: object) -> int:
    """Wind 报表类型优先级。

    408001000 通常是合并报表口径。其余类型作为回退，避免个别公司因报表类型缺失而完全丢样本。未知类型优先级最低。
    """

    priority = {
        "408001000": 0,
        "408002000": 1,
        "408003000": 2,
        "408004000": 3,
        "408005000": 4,
    }
    return priority.get(str(statement_type), 99)


def prepare_quarterly_cashflow_ttm() -> pd.DataFrame:
    """整理个股 PIT 口径 TTM 现金流。

    Wind 现金流量表的季度报告通常是年初至报告期的累计值，因此先转为单季现金流，再滚动 4 个季度求 TTM。随后保留公告日，供月末时点选择“最近已可得报告期”。
    """

    # 读取原始数据
    columns = [
        "S_INFO_WINDCODE",
        "WIND_CODE",
        "ANN_DT",
        "ACTUAL_ANN_DT",
        "REPORT_PERIOD",
        "STATEMENT_TYPE",
        CFO_COL,
        CFI_COL,
        CFF_COL,
        "OPDATE",
    ]
    df = read_parquet_dataset("cashflow_statement", columns=columns)

    # 日期处理
    df["windcode"] = df["S_INFO_WINDCODE"].fillna(df["WIND_CODE"])
    df["report_date"] = parse_wind_date(df["REPORT_PERIOD"])
    df["available_date"] = parse_wind_date(df["ACTUAL_ANN_DT"]).fillna(parse_wind_date(df["ANN_DT"]))
    df["opdate_ts"] = pd.to_datetime(df["OPDATE"], errors="coerce")

    # 数据清洗
    df = df[
        df["windcode"].notna()
        & df["report_date"].notna()
        & df["available_date"].notna()
        & df["report_date"].dt.month.isin([3, 6, 9, 12])
    ].copy()
    df["fiscal_year"] = df["report_date"].dt.year
    df["fiscal_quarter"] = df["report_date"].dt.quarter
    df["statement_priority"] = df["STATEMENT_TYPE"].map(statement_priority)
    for col in [CFO_COL, CFI_COL, CFF_COL]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values(
        [
            "windcode",
            "report_date",
            "statement_priority",  # 报表类型优先级 （ statement_priority ）：合并报表 > 母公司报表 > 其他
            "available_date",  # 越晚的公告越可能是修订版，优先保留
            "opdate_ts", # 最后更新的版本最准确
        ],
        ascending=[True, True, True, False, False],
    )
    df = df.drop_duplicates(["windcode", "report_date"], keep="first")
    df = df.sort_values(["windcode", "report_date"]).reset_index(drop=True)

    # 累计值转单季值
    for col in [CFO_COL, CFI_COL, CFF_COL]:
        prev_cum = df.groupby(["windcode", "fiscal_year"])[col].shift(1)
        single_col = f"{col}_Q"
        df[single_col] = df[col]
        non_q1 = df["fiscal_quarter"] > 1
        df.loc[non_q1, single_col] = df.loc[non_q1, col] - prev_cum.loc[non_q1]
        df[f"{col}_TTM"] = (
            df.groupby("windcode", group_keys=False)[single_col]
            .rolling(4, min_periods=4)
            .sum()
            .reset_index(level=0, drop=True)
        )

    ttm_cols = [f"{col}_TTM" for col in [CFO_COL, CFI_COL, CFF_COL]]
    df = df.dropna(subset=ttm_cols).copy()
    df = df.rename(
        columns={
            f"{CFO_COL}_TTM": "cfo_ttm",
            f"{CFI_COL}_TTM": "cfi_ttm",
            f"{CFF_COL}_TTM": "cff_ttm",
        }
    )
    return df[
        [
            "windcode",
            "report_date",
            "available_date",
            "STATEMENT_TYPE",
            "cfo_ttm",
            "cfi_ttm",
            "cff_ttm",
        ]
    ].reset_index(drop=True)


def _ancestor_code(old_code: str, level: int) -> str:
    """由申万旧行业代码推导上级代码。

    Wind 行业字典中，`LEVELNUM=2/3/4` 分别对应申万一级/二级/三级。
    原始成分表 `SW_IND_CODE` 是 10 位旧代码，可由前缀推导父级。
    """

    code = str(old_code)
    if len(code) < 10:
        return code
    if level == 2:
        return code[:4] + "000000"
    if level == 3:
        return code[:7] + "000"
    if level == 4:
        return code[:10]
    raise ValueError("industry_level 只能是 2、3 或 4，分别对应申万一/二/三级")


def _read_industry_membership() -> pd.DataFrame:
    """读取并标准化股票历史行业归属。"""

    membership = read_parquet_dataset(
        "industry_membership",
        columns=[
            "S_INFO_WINDCODE",
            "SW_IND_CODE",
            "ENTRY_DT",
            "REMOVE_DT",
            "CUR_SIGN",
        ],
    )
    membership["windcode"] = membership["S_INFO_WINDCODE"]
    membership["entry_date"] = parse_wind_date(membership["ENTRY_DT"])
    membership["remove_date"] = parse_wind_date(membership["REMOVE_DT"])
    membership["sw_old_code"] = membership["SW_IND_CODE"].astype("string")
    membership["sw_level2_old_code"] = membership["sw_old_code"].map(
        lambda x: _ancestor_code(x, 3) if pd.notna(x) else pd.NA
    )
    membership["sw_level3_old_code"] = membership["sw_old_code"].map(
        lambda x: _ancestor_code(x, 4) if pd.notna(x) else pd.NA
    )
    membership = membership[
        membership["windcode"].notna()
        & membership["entry_date"].notna()
        & membership["sw_old_code"].notna()
    ].copy()
    return membership


def _read_industry_code_map() -> pd.DataFrame:
    """读取申万行业代码名称映射。"""

    code = read_parquet_dataset(
        "industry_classification",
        columns=["INDUSTRIESCODE", "INDUSTRIESNAME", "LEVELNUM", "USED", "INDUSTRIESCODE_OLD"],
    )
    code["industry_old_code"] = code["INDUSTRIESCODE_OLD"].astype("string")
    code["industry_code"] = code["INDUSTRIESCODE"].astype("string")
    code["industry_name"] = code["INDUSTRIESNAME"].astype("string")
    code["levelnum"] = pd.to_numeric(code["LEVELNUM"], errors="coerce")
    code["used"] = pd.to_numeric(code["USED"], errors="coerce")
    code = code[
        code["industry_old_code"].astype("string").str.startswith("760", na=False)
    ].copy()
    code = code.sort_values(["industry_old_code", "used"], ascending=[True, False])
    code = code.drop_duplicates("industry_old_code", keep="first")
    return code[["industry_old_code", "industry_code", "industry_name", "levelnum", "used"]]


def _count_hybrid_industries(
    tertiary_to_secondary: pd.DataFrame,
    collapsed_tertiary: set[str],
) -> int:
    """计算给定合并方案下的细分行业数量。"""

    mapped = tertiary_to_secondary.copy()
    mapped["hybrid_old_code"] = mapped.apply(
        lambda row: row["secondary_old_code"]
        if row["tertiary_old_code"] in collapsed_tertiary
        else row["tertiary_old_code"],
        axis=1,
    )
    return mapped["hybrid_old_code"].nunique()


def _tertiary_activity_profile(membership: pd.DataFrame, config: LifecycleConfig) -> pd.DataFrame:
    """按月末逐月统计各三级行业的历史在职成分股数，取样本期内的最大并发数。

    修复点：原实现只用 `config.end_date` 当天的静态快照判断某三级行业是否“成分股过少”。这样会有两个方向的误判：
    1. 某三级行业在样本早期股票充足，但后期因退市/重组导致成分股收缩到截止日时数量很少——会被误判为“过小”而合并，掩盖了其历史上本应独立成一个细分资产的阶段；
    2. 反过来，若某三级行业在截止日股票数量恰好达标，但历史上长期只有零星几只股票，也会被误判为“足够大”而不合并。

    这里改为在 [start_date, end_date] 区间逐月重算在职成分股数，取该时间序列上的最大并发数量作为该三级行业的“规模代表值”，更贴近研报里“成分股过少”这一判断本应覆盖的历史全貌，而不是单一时点快照。
    """

    months = month_ends(config.start_date, config.end_date)
    monthly_counts = []
    for month_end in months:
        active = membership[(membership["entry_date"] <= month_end) & (membership["remove_date"].isna() | (membership["remove_date"] > month_end))]
        monthly_counts.append(active.groupby("sw_level3_old_code")["windcode"].nunique().rename(month_end))
    if not monthly_counts:
        return pd.DataFrame(columns=["tertiary_old_code", "max_active_stock_count"])

    panel = pd.concat(monthly_counts, axis=1).fillna(0)
    profile = panel.max(axis=1).rename("max_active_stock_count").reset_index()
    profile = profile.rename(columns={"sw_level3_old_code": "tertiary_old_code"})
    profile["tertiary_old_code"] = profile["tertiary_old_code"].astype(str)
    return profile


def build_hybrid_industry_mapping(
    membership: pd.DataFrame,
    code_map: pd.DataFrame,
    config: LifecycleConfig,
) -> pd.DataFrame:
    """构造“申万三级为主，小样本三级回收到申万二级”的行业映射。

    研报描述为“二级+三级行业，以三级行业为主，成分股过少或区分度不大的三级行业聚合到二级行业”。由于“区分度不大”没有公开的确定性阈值：

    1. 三级→二级的父子结构关系取自全历史 membership（不局限于截止日仍存续的行业），避免样本期内已退出/合并的三级行业在结构映射里“查无此码”，导致其历史月份被静默丢弃；
    2. 成分股规模改用样本期内逐月统计的历史最大并发数量判断（见`_tertiary_activity_profile`），而不是仅看截止日单点快照；
    3. 规模低于 `min_tertiary_stocks` 的三级行业先回收到父级二级；
    4. 默认不强行凑到某个固定行业数量。若显式设置 `target_industries`，才会继续按规模从小到大回收到二级，直到达到目标数量。
    """

    # 父子结构关系用全历史样本推导，不受某一时点是否仍有存续股票影响。
    tertiary_to_secondary = (
        membership[["sw_level3_old_code", "sw_level2_old_code"]]
        .dropna()
        .drop_duplicates()
        .rename(
            columns={
                "sw_level3_old_code": "tertiary_old_code",
                "sw_level2_old_code": "secondary_old_code",
            }
        )
    )
    tertiary_to_secondary["tertiary_old_code"] = tertiary_to_secondary["tertiary_old_code"].astype(str)
    # 若申万行业分类修订导致同一三级代码历史上对应过不止一个二级父级，
    # 取出现频次最高的父级，保证父子映射唯一，避免下游 merge 产生重复行。
    if tertiary_to_secondary["tertiary_old_code"].duplicated().any():
        tertiary_to_secondary = (
            tertiary_to_secondary.groupby("tertiary_old_code")["secondary_old_code"]
            .agg(lambda s: s.value_counts().idxmax())
            .reset_index()
        )

    # 计算历史最大成分股数量
    activity = _tertiary_activity_profile(membership, config)
    tertiary_to_secondary = tertiary_to_secondary.merge(activity, on="tertiary_old_code", how="left")
    tertiary_to_secondary["active_stock_count"] = (tertiary_to_secondary["max_active_stock_count"].fillna(0).astype(int))
    tertiary_to_secondary = tertiary_to_secondary.drop(columns=["max_active_stock_count"])

    # 标记需要回收的三级行业
    collapsed = set(
        tertiary_to_secondary.loc[
            tertiary_to_secondary["active_stock_count"] < config.min_tertiary_stocks,
            "tertiary_old_code",
        ].astype(str)
    )

    # [可选] 强制回收至目标数量
    current_count = _count_hybrid_industries(tertiary_to_secondary, collapsed)
    if config.target_industries and current_count > config.target_industries:
        candidates = tertiary_to_secondary[
            ~tertiary_to_secondary["tertiary_old_code"].isin(collapsed)
        ].sort_values(["active_stock_count", "tertiary_old_code"])
        for _, row in candidates.iterrows():
            if current_count <= config.target_industries:
                break
            collapsed.add(str(row["tertiary_old_code"]))
            current_count = _count_hybrid_industries(tertiary_to_secondary, collapsed)

    # 生成最终映射
    mapping = tertiary_to_secondary.copy()
    mapping["is_collapsed_to_level2"] = mapping["tertiary_old_code"].isin(collapsed)
    mapping["industry_old_code"] = mapping.apply(
        lambda row: row["secondary_old_code"]
        if row["is_collapsed_to_level2"]
        else row["tertiary_old_code"],
        axis=1,
    )
    mapping["industry_source_level"] = mapping["is_collapsed_to_level2"].map(
        {True: "sw_level2", False: "sw_level3"}
    )

    names = code_map[["industry_old_code", "industry_code", "industry_name"]]
    tertiary_names = names.rename(
        columns={
            "industry_old_code": "tertiary_old_code",
            "industry_code": "tertiary_code",
            "industry_name": "tertiary_name",
        }
    )
    secondary_names = names.rename(
        columns={
            "industry_old_code": "secondary_old_code",
            "industry_code": "secondary_code",
            "industry_name": "secondary_name",
        }
    )
    hybrid_names = names.rename(
        columns={
            "industry_code": "industry_code",
            "industry_name": "industry_name",
        }
    )
    mapping = mapping.merge(tertiary_names, on="tertiary_old_code", how="left")
    mapping = mapping.merge(secondary_names, on="secondary_old_code", how="left")
    mapping = mapping.merge(hybrid_names, on="industry_old_code", how="left")
    mapping["industry_code"] = mapping["industry_code"].fillna(mapping["industry_old_code"])
    mapping["industry_name"] = mapping["industry_name"].fillna(mapping["industry_old_code"])
    return mapping.sort_values(["industry_source_level", "industry_old_code", "tertiary_old_code"])


def prepare_industry_map(config: LifecycleConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """整理股票历史行业归属、行业代码名称映射和 hybrid 映射表。"""

    membership = _read_industry_membership()
    code_map = _read_industry_code_map()

    if config.industry_scheme == "hybrid":
        hybrid_map = build_hybrid_industry_mapping(membership, code_map, config)
        tertiary_to_hybrid = hybrid_map[
            [
                "tertiary_old_code",
                "industry_old_code",
                "industry_source_level",
                "is_collapsed_to_level2",
            ]
        ].drop_duplicates()
        membership = membership.merge(
            tertiary_to_hybrid,
            left_on="sw_level3_old_code",
            right_on="tertiary_old_code",
            how="left",
        )
        code_for_use = hybrid_map[
            ["industry_old_code", "industry_code", "industry_name", "industry_source_level"]
        ].drop_duplicates("industry_old_code")
    else:
        membership["industry_old_code"] = membership["sw_old_code"].map(
            lambda x: _ancestor_code(x, config.industry_level) if pd.notna(x) else pd.NA
        )
        membership["industry_source_level"] = f"sw_level{config.industry_level - 1}"
        hybrid_map = pd.DataFrame()
        code_for_use = code_map[
            code_map["levelnum"].eq(config.industry_level)
        ][["industry_old_code", "industry_code", "industry_name"]].copy()
        code_for_use["industry_source_level"] = membership["industry_source_level"].iloc[0]

    membership = membership[
        membership["windcode"].notna()
        & membership["entry_date"].notna()
        & membership["industry_old_code"].notna()
    ].copy()
    return membership, code_for_use, hybrid_map


def attach_industry_by_month(
    stock_monthly: pd.DataFrame,
    membership: pd.DataFrame,
    code_map: pd.DataFrame,
) -> pd.DataFrame:
    """为个股月度现金流匹配月末历史行业。"""

    merged = stock_monthly.merge(
        membership[
            [
                "windcode",
                "industry_old_code",
                "industry_source_level",
                "sw_old_code",
                "sw_level2_old_code",
                "sw_level3_old_code",
                "entry_date",
                "remove_date",
            ]
        ],
        on="windcode",
        how="left",
    )
    active = (
        (merged["entry_date"] <= merged["month_end"])
        & (merged["remove_date"].isna() | (merged["remove_date"] > merged["month_end"]))
    )
    merged = merged[active].copy()
    merged = merged.sort_values(["windcode", "month_end", "entry_date"])
    merged = merged.drop_duplicates(["windcode", "month_end"], keep="last")
    merged = merged.merge(code_map, on="industry_old_code", how="left")
    if "industry_source_level_x" in merged.columns:
        merged["industry_source_level"] = merged["industry_source_level_x"].fillna(
            merged.get("industry_source_level_y")
        )
    merged["industry_code"] = merged["industry_code"].fillna(merged["industry_old_code"])
    merged["industry_name"] = merged["industry_name"].fillna(merged["industry_old_code"])
    return merged[merged["industry_old_code"].notna()].reset_index(drop=True)


def build_monthly_ttm_lifecycle(config: LifecycleConfig) -> pd.DataFrame:
    """按研报口径生成月频 TTM 行业生命周期。

    对每个月末，先选取个股在该月末前已经公告的最近报告期 TTM 现金流；
    再按月末历史行业归属聚合为行业整体现金流，并按 Dickinson 符号划分。
    """

    ttm = prepare_quarterly_cashflow_ttm()
    months = month_ends(config.start_date, config.end_date)
    stock_codes = pd.Index(ttm["windcode"].dropna().unique(), name="windcode")
    grid = pd.MultiIndex.from_product([stock_codes, months], names=["windcode", "month_end"])
    grid_df = grid.to_frame(index=False).sort_values(["month_end", "windcode"]) # "股票×月末"笛卡尔积
    ttm = ttm.sort_values(["available_date", "windcode"])

    # PIT 匹配：按月末时点选择已公告的最近报告期
    stock_monthly = pd.merge_asof(
        grid_df,
        ttm,
        by="windcode",
        left_on="month_end",
        right_on="available_date",
        direction="backward",
        allow_exact_matches=True,
    ) 
    stock_monthly = stock_monthly.dropna(subset=["cfo_ttm", "cfi_ttm", "cff_ttm"]).copy()

    # 行业归属
    membership, industry_universe, _ = prepare_industry_map(config)
    stock_industry = attach_industry_by_month(stock_monthly, membership, industry_universe)

    # 按行业聚合现金流
    grouped = (
        stock_industry.groupby(
            [
                "month_end",
                "industry_old_code",
                "industry_code",
                "industry_name",
                "industry_source_level",
            ],
            dropna=False,
        )
        .agg(
            cfo=("cfo_ttm", "sum"),
            cfi=("cfi_ttm", "sum"),
            cff=("cff_ttm", "sum"),
            stock_count=("windcode", "nunique"),
            latest_report_date=("report_date", "max"),
            latest_available_date=("available_date", "max"),
        )
        .reset_index()
    )

    # 过滤掉股票数量不足的行业
    grouped = grouped[grouped["stock_count"] >= config.min_stocks].copy()

    # 计算 Dickinson 符号
    grouped["cfo_sign"] = grouped["cfo"].map(cashflow_sign)
    grouped["cfi_sign"] = grouped["cfi"].map(cashflow_sign)
    grouped["cff_sign"] = grouped["cff"].map(cashflow_sign)
    grouped["cashflow_pattern"] = grouped["cfo_sign"] + grouped["cfi_sign"] + grouped["cff_sign"]
    # Dickinson 生命周期分类
    grouped["lifecycle_stage"] = [
        classify_dickinson(a, b, c)
        for a, b, c in zip(grouped["cfo_sign"], grouped["cfi_sign"], grouped["cff_sign"])
    ]
    grouped["lifecycle_stage_zh"] = grouped["lifecycle_stage"].map(stage_name_zh)

    # 生成最终映射
    universe = industry_universe[
        ["industry_old_code", "industry_code", "industry_name", "industry_source_level"]
    ].drop_duplicates("industry_old_code")
    full_panel = pd.MultiIndex.from_product(
        [months, universe["industry_old_code"]],
        names=["month_end", "industry_old_code"],
    ).to_frame(index=False)
    full_panel = full_panel.merge(universe, on="industry_old_code", how="left")
    monthly = full_panel.merge(
        grouped,
        on=[
            "month_end",
            "industry_old_code",
            "industry_code",
            "industry_name",
            "industry_source_level",
        ],
        how="left",
    )
    monthly["lifecycle_stage"] = monthly["lifecycle_stage"].fillna("unknown")
    monthly["lifecycle_stage_zh"] = monthly["lifecycle_stage_zh"].fillna("未知")
    return monthly.sort_values(["month_end", "industry_old_code"]).reset_index(drop=True)


def month_ends(start_date: str, end_date: str) -> pd.DatetimeIndex:
    """生成月末日期序列。"""

    start = pd.Timestamp(start_date).to_period("M").to_timestamp("M")
    end = pd.Timestamp(end_date).to_period("M").to_timestamp("M")
    return pd.date_range(start, end, freq="ME")


def _load_cached_fingerprint() -> Optional[str]:
    """读取上一次落盘缓存时记录的参数指纹，读取失败则视为无缓存。"""

    if not CACHE_META_OUTPUT.exists():
        return None
    try:
        payload = json.loads(CACHE_META_OUTPUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload.get("fingerprint")


def run_lifecycle(config: LifecycleConfig, overwrite: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """执行生命周期划分并写出结果。"""

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.joinpath("tables").mkdir(parents=True, exist_ok=True)

    fingerprint = config_fingerprint(config)
    cached_fingerprint = _load_cached_fingerprint()
    cache_valid = (
        ANNUAL_OUTPUT.exists()
        and MONTHLY_OUTPUT.exists()
        and cached_fingerprint == fingerprint
    )

    if cache_valid and not overwrite:
        LOGGER.info("生命周期缓存命中（参数指纹一致：%s），跳过计算。如需强制重算请使用 --overwrite。", fingerprint)
        annual = pd.read_parquet(ANNUAL_OUTPUT)
        monthly = pd.read_parquet(MONTHLY_OUTPUT)
    else:
        if ANNUAL_OUTPUT.exists() and not overwrite and cached_fingerprint != fingerprint:
            LOGGER.info(
                "检测到划分参数已变化（旧指纹：%s，新指纹：%s），忽略过期缓存并重新计算。",
                cached_fingerprint,
                fingerprint,
            )
        monthly = build_monthly_ttm_lifecycle(config)
        annual = monthly[monthly["month_end"].dt.month == 12].copy()
        annual["fiscal_year"] = annual["month_end"].dt.year
        annual.to_parquet(ANNUAL_OUTPUT, index=False)
        monthly.to_parquet(MONTHLY_OUTPUT, index=False)
        CACHE_META_OUTPUT.write_text(
            json.dumps({"fingerprint": fingerprint}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    counts = (
        monthly.groupby(["month_end", "lifecycle_stage", "lifecycle_stage_zh"])
        .size()
        .reset_index(name="industry_count")
    )
    counts.to_csv(STAGE_COUNTS_OUTPUT, index=False, encoding="utf-8-sig")
    _, _, hybrid_map = prepare_industry_map(config)
    if not hybrid_map.empty:
        hybrid_map.to_csv(HYBRID_MAP_OUTPUT, index=False, encoding="utf-8-sig")
    return annual, monthly


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="Dickinson 现金流法产业生命周期划分")
    parser.add_argument(
        "--industry-scheme",
        choices=["hybrid", "fixed"],
        default="hybrid",
        help="hybrid=研报式二级+三级混合；fixed=固定 Wind LEVELNUM 层级",
    )
    parser.add_argument(
        "--industry-level",
        type=int,
        default=4,
        choices=[2, 3, 4],
        help="仅 fixed 模式使用：2/3/4 分别对应申万一级/二级/三级",
    )
    parser.add_argument("--min-stocks", type=int, default=3)
    parser.add_argument("--min-tertiary-stocks", type=int, default=8)
    parser.add_argument("--target-industries", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = LifecycleConfig(
        industry_scheme=args.industry_scheme,
        industry_level=args.industry_level,
        min_stocks=args.min_stocks,
        min_tertiary_stocks=args.min_tertiary_stocks,
        target_industries=args.target_industries,
    )
    annual, monthly = run_lifecycle(config, overwrite=args.overwrite)
    LOGGER.info("年度生命周期：%s 行；月度生命周期：%s 行", len(annual), len(monthly))
    LOGGER.info("输出：%s", ANNUAL_OUTPUT)
    LOGGER.info("输出：%s", MONTHLY_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
