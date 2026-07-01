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


# ── 3a: 교차자산 거래달력 carry-forward 표면화 (#1) ────────────────────────────
def _cross_calendars():
    """S&P500=미국 거래일, 코스피선물=한국 거래일 — 서로 휴장일이 달라 각자 상대 개장일에 결손."""
    us = pd.bdate_range("2022-01-03", "2023-12-29")
    us_hol = pd.to_datetime(["2022-01-17", "2022-02-21", "2022-05-30", "2022-07-04",
                             "2022-09-05", "2022-11-24", "2022-12-26", "2023-01-16",
                             "2023-02-20", "2023-05-29", "2023-07-04", "2023-09-04",
                             "2023-11-23", "2023-12-25"])
    kr = pd.bdate_range("2022-01-03", "2023-12-29")
    kr_hol = pd.to_datetime(["2022-01-31", "2022-03-01", "2022-05-05", "2022-06-06",
                             "2022-09-09", "2022-10-03", "2023-01-23", "2023-03-01",
                             "2023-05-05", "2023-08-15", "2023-10-03", "2023-12-25"])
    return us[~us.isin(us_hol)], kr[~kr.isin(kr_hol)]


def test_cross_calendar_carryforward_surfaced():
    """교차자산(미국 캘린더)이 한국 개장·미국 휴장일에 전일값 유지되는 것을 INFO로 표면화 —
    사용자가 '결손 多'로 오인하던 #1. data_gap(진짜 결손)과 구분: carry-forward는 정상."""
    us, kr = _cross_calendars()
    out = assess_data_quality({"S&P500": _df(us), "코스피200선물": _df(kr)})
    cf = [w for w in out if w["code"] == "calendar_carryforward"]
    assert cf, f"교차달력 carry-forward가 표면화돼야: {out}"
    assert "S&P500" in cf[0]["message"]
    # 정상 신호이지 결손이 아니므로 data_gap로 오탐하지 않는다(5% 허용 이내).
    assert not any(w["code"] == "data_gap" for w in out), f"교차달력을 data_gap로 오탐 금지: {out}"


def test_cross_calendar_flags_only_minority_calendar():
    """다수(한국 2종) + 소수(S&P500 미국) → 소수 캘린더(S&P500)만 flag, 한국 종목은 무플래그."""
    us, kr = _cross_calendars()
    out = assess_data_quality({"005930": _df(kr), "000660": _df(kr), "S&P500": _df(us)})
    cf = [w for w in out if w["code"] == "calendar_carryforward"]
    assert cf and "S&P500" in cf[0]["message"], f"S&P500이 flag돼야: {out}"
    assert "005930" not in cf[0]["message"] and "000660" not in cf[0]["message"], \
        f"다수 캘린더(한국 종목)는 flag 금지: {cf[0]['message']}"


def test_same_calendar_no_carryforward():
    """같은 거래달력(모두 한국)이면 carry-forward 없음(회귀 가드 — 노이즈 억제)."""
    kr = pd.bdate_range("2022-01-03", "2023-12-29")
    out = assess_data_quality({"005930": _df(kr), "000660": _df(kr), "005380": _df(kr)})
    assert not any(w["code"] == "calendar_carryforward" for w in out), f"동일달력 무경고여야: {out}"


def test_carryforward_traded_reference_flags_only_signal():
    """traded(체결 유니버스=코스피선물) 기준이면 신호 심볼(S&P500)만 flag하고 거래 심볼은 제외 —
    거래 심볼은 자기 달력에서 결손이 아니므로(#1 n=2 정밀도: '내가 거래하는 종목이 왜 결손?' 방지)."""
    us, kr = _cross_calendars()
    out = assess_data_quality({"S&P500": _df(us), "코스피200선물": _df(kr)},
                              traded={"코스피200선물"})
    cf = [w for w in out if w["code"] == "calendar_carryforward"]
    assert cf and "S&P500" in cf[0]["message"], f"S&P500(신호)이 flag돼야: {out}"
    assert "코스피200선물" not in cf[0]["message"], \
        f"거래 심볼은 제외돼야(자기 달력에서 결손 아님): {cf[0]['message']}"
