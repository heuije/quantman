"""assess_data_quality — Phase 0.5 실행 전 데이터 품질 불변식 (결정적·now() 미사용).

stale/gappy 입력이 침묵의 0%로 나오던 부류(#4 KOSPI 내부공백·2026 신호 staleness)를 실행 시점에
명시 경고로 표면화하는지 고정.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine.data_quality import assess_data_quality


def _df(dates, close=100.0):
    n = len(dates)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": np.full(n, close, dtype=float), "Volume": 1e6}, index=dates)


def test_clean_two_symbols_no_warning():
    d = pd.bdate_range("2020-01-01", "2024-12-31")
    assert assess_data_quality({"A": _df(d), "B": _df(d)}) == []


def test_stale_symbol_flagged():
    """한 심볼이 다른 심볼보다 크게 일찍 끝나면 stale_data (2026 신호 staleness 부류)."""
    fresh = pd.bdate_range("2020-01-01", "2026-06-15")
    stale = pd.bdate_range("2020-01-01", "2025-04-01")
    out = assess_data_quality({"코스피200선물": _df(fresh), "S&P500": _df(stale)})
    assert any(w["code"] == "stale_data" and "S&P500" in w["message"] for w in out)


def test_internal_gap_flagged():
    """공통 구간에서 한 심볼 밀도가 크게 낮으면 data_gap (#4 KOSPI 내부공백 부류)."""
    full = pd.bdate_range("2020-01-01", "2024-12-31")
    gappy = full[full.year != 2022]                 # 2022 통째 결손(끝 날짜는 동일 → stale 아님)
    out = assess_data_quality({"A": _df(full), "B": _df(gappy)})
    assert any(w["code"] == "data_gap" and "B" in w["message"] for w in out)


def test_missing_symbol_flagged():
    out = assess_data_quality({"A": _df(pd.bdate_range("2020-01-01", "2021-01-01")),
                               "B": pd.DataFrame()})
    assert any(w["code"] == "missing_data" and "B" in w["message"] for w in out)


def test_single_symbol_no_relative_warning():
    """단일 심볼은 피어 없어 상대 비교 미발생(절대 검사는 레지스트리 Coverage 후속)."""
    assert assess_data_quality({"A": _df(pd.bdate_range("2010-01-01", "2020-01-01"))}) == []


def test_relevant_scoping_excludes_nonrelevant_symbols():
    """relevant 주어지면 그 심볼만 평가 — 'all'에서 후보 아닌 매크로/지수 staleness 경고를 원천
    억제(R4 — 후보 아닌 심볼을 결함으로 표면화하지 않음). dataset엔 있어도 평가 대상 밖이면 침묵."""
    fresh = pd.bdate_range("2020-01-01", "2026-06-15")
    stale = pd.bdate_range("2020-01-01", "2025-04-01")
    ds = {"005930": _df(fresh), "000660": _df(fresh), "GDP": _df(stale)}
    # 전체 평가(relevant=None): GDP가 다른 심볼보다 일찍 끝나 stale 경고 발생
    full = assess_data_quality(ds)
    assert any(w["code"] == "stale_data" and "GDP" in w["message"] for w in full)
    # relevant=주식만: GDP(후보 아님)는 평가 대상 밖 → 경고 없음
    scoped = assess_data_quality(ds, relevant={"005930", "000660"})
    assert not any("GDP" in w.get("message", "") for w in scoped)
    assert scoped == []                      # 주식 2종은 신선 → 무경고
