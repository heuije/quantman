"""조건 평가 엔진 단위 테스트 — G1 아핀변환 · G2 중첩 그룹 · 하위호환.

합성 데이터(외부 의존 0)로 build_signal_mask의 새 표현력을 검증한다.
golden_backtest.py가 '기존 결과 불변'을 보장한다면, 이 파일은 '새 기능이
의도대로 동작'을 보장한다.
"""

from __future__ import annotations

import pandas as pd

from quant_core.analysis import build_signal_mask


def _data() -> dict[str, pd.DataFrame]:
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    df = pd.DataFrame(
        {
            "Close": [100, 100, 100, 100, 100],
            "MA20": [90, 95, 100, 96, 80],
        },
        index=idx,
    )
    return {"TST": df}


def test_backward_compat_flat():
    """기존 flat 조건(아핀/중첩 없음)은 종전과 동일하게 동작."""
    data = _data()
    conds = [{
        "left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
        "op": ">",
        "right": {"kind": "constant", "value": 50},
    }]
    mask = build_signal_mask(data, conds, "AND")
    assert list(mask) == [True] * 5


def test_affine_mul_scaling():
    """G1 — Close > MA20 × 1.05. MA20×1.05 < 100 인 날만 True."""
    data = _data()
    conds = [{
        "left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
        "op": ">",
        "right": {"kind": "indicator", "symbol": "TST",
                  "indicator": "MA20", "mul": 1.05},
    }]
    mask = build_signal_mask(data, conds, "AND")
    # MA20×1.05: 94.5, 99.75, 105, 100.8, 84 → 100 초과 비교
    assert list(mask) == [True, True, False, False, True]


def test_affine_add_offset():
    """G1 — add 가감. Close > MA20 + 5. (MA20+5 < 100)."""
    data = _data()
    conds = [{
        "left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
        "op": ">",
        "right": {"kind": "indicator", "symbol": "TST",
                  "indicator": "MA20", "add": 5},
    }]
    mask = build_signal_mask(data, conds, "AND")
    # MA20+5: 95, 100, 105, 101, 85
    assert list(mask) == [True, False, False, False, True]


def test_nested_group_or():
    """G2 — (Close > MA20×1.05) OR (Close > 200). 둘째는 항상 거짓이라 첫째와 동일."""
    data = _data()
    nested = {
        "conditions": [
            {"left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
             "op": ">",
             "right": {"kind": "indicator", "symbol": "TST",
                       "indicator": "MA20", "mul": 1.05}},
            {"left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
             "op": ">",
             "right": {"kind": "constant", "value": 200}},
        ],
        "logic": "OR",
    }
    mask = build_signal_mask(data, [nested], "AND")
    assert list(mask) == [True, True, False, False, True]


def test_nested_group_and_mix_with_leaf():
    """G2 — 단일 조건 AND 하위그룹 혼합. Close>50 AND (Close>MA20×1.05 OR Close>200)."""
    data = _data()
    leaf = {
        "left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
        "op": ">",
        "right": {"kind": "constant", "value": 50},
    }
    nested = {
        "conditions": [
            {"left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
             "op": ">",
             "right": {"kind": "indicator", "symbol": "TST",
                       "indicator": "MA20", "mul": 1.05}},
            {"left": {"kind": "indicator", "symbol": "TST", "indicator": "Close"},
             "op": ">",
             "right": {"kind": "constant", "value": 200}},
        ],
        "logic": "OR",
    }
    mask = build_signal_mask(data, [leaf, nested], "AND")
    # leaf 전부 True → 결과 = nested 결과
    assert list(mask) == [True, True, False, False, True]
