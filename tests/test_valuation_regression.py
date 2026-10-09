import warnings

import numpy as np
import pandas as pd

from src.valuation_regression import merge_factor_with_valuation_data, ols_r2


def test_valuation_panel_uses_actual_dividend_yield_not_legacy_factor_value() -> None:
    """红利策略的 DP 必须来自实际现金分红拆解表。"""

    factor = pd.DataFrame(
        {
            "month_end": [pd.Timestamp("2026-02-28")],
            "industry_old_code": ["7601010100"],
            "roe_ttm": [8.0],
            "dividend_yield": [np.nan],
        }
    )
    valuation = pd.DataFrame(
        {
            "month_end": [pd.Timestamp("2026-02-28")],
            "industry_old_code": ["7601010100"],
            "pb": [1.5],
            "dividend_yield": [0.023],
        }
    )

    merged = merge_factor_with_valuation_data(factor, valuation)

    assert merged.loc[0, "dividend_yield"] == 0.023


def test_ols_r2_is_finite_for_large_but_finite_values() -> None:
    """PE 类极大有限值不应在拟合阶段触发矩阵溢出。"""

    x = pd.Series(np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]) * 1e250)
    y = pd.Series(3.0 * x.to_numpy() + 2e250)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        r2, nobs = ols_r2(y, x, min_obs=5)
    assert nobs == 6
    assert np.isfinite(r2)
    assert r2 > 0.999999


def test_ols_r2_skips_constant_predictor() -> None:
    y = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    x = pd.Series([3.0] * 6)
    r2, nobs = ols_r2(y, x, min_obs=5)
    assert nobs == 6
    assert np.isnan(r2)


def test_ols_r2_requires_residual_degree_of_freedom() -> None:
    y = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    x = pd.DataFrame({"a": [1, 2, 3, 4, 5], "b": [2, 4, 7, 11, 16], "c": [1, 4, 9, 16, 25], "d": [3, 1, 4, 1, 5]})
    r2, nobs = ols_r2(y, x, min_obs=5)
    assert nobs == 5
    assert np.isnan(r2)


def test_ols_r2_skips_near_collinear_design_without_warning() -> None:
    base = np.linspace(0.1, 1.0, 10)
    x = pd.DataFrame({"a": base, "b": base + np.arange(10) * 1e-15})
    y = pd.Series(2.0 + 3.0 * base)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        r2, nobs = ols_r2(y, x, min_obs=5)
    assert nobs == 10
    assert np.isnan(r2)
