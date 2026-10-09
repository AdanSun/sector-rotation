"""最终交付层的申万行业展示映射。

该模块只增加展示列，不修改内部 hybrid 行业键。
"""

from __future__ import annotations

import pandas as pd

from src.lifecycle import _read_industry_code_map


def to_display_code(code: str, level: int) -> str:
    """将申万三级旧码转为指定展示层级的旧码。"""

    if level not in (2, 3):
        raise ValueError("level 只能是 2（申万二级）或 3（申万三级）")
    if pd.isna(code):
        return code
    value = str(code)
    if level == 3 or len(value) < 10 or value.endswith("000"):
        return value
    return value[:7] + "000"


def load_display_map(level: int) -> pd.DataFrame:
    """读取展示行业旧码与名称对照表。"""

    if level not in (2, 3):
        raise ValueError("level 只能是 2 或 3")
    code_map = _read_industry_code_map()
    wind_level = 3 if level == 2 else 4
    result = code_map[code_map["levelnum"].eq(wind_level)][
        ["industry_old_code", "industry_name"]
    ].copy()
    return result.rename(columns={"industry_name": "display_industry_name"})


def map_to_display(frame: pd.DataFrame, level: int) -> pd.DataFrame:
    """在不改变原行业键的前提下增加展示代码和名称。"""

    if "industry_old_code" not in frame.columns:
        return frame
    result = frame.drop(
        columns=["display_industry_code", "display_industry_name"],
        errors="ignore",
    ).copy()
    result["display_industry_code"] = result["industry_old_code"].map(
        lambda value: to_display_code(value, level)
    )
    display_map = load_display_map(level).rename(
        columns={"industry_old_code": "display_industry_code"}
    )
    result = result.merge(display_map, on="display_industry_code", how="left")
    if "industry_name" in result.columns:
        result["display_industry_name"] = result["display_industry_name"].fillna(
            result["industry_name"]
        )
    return result
