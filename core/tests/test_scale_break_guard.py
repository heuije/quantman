"""수집 스케일 불연속 가드 — 소스 심볼 변경/오종목 splice 자기치유 (2026-07 DAX 사건 재현·수정).

배경: 웹 GlobalMarket '세계 10대 지수'에서 DAX가 +58,000%로 폭발. 원인=볼륨 'DAX'가 옛 오종목
(독일 ETF ~$43) 이력 위에 정규 ^GDAXI(~25,000)를 증분 append해 ~580× splice. 프론트의 '첫 점
리베이스'가 이를 증폭. 가드가 병합 결과의 스케일 불연속을 감지→전체 재수집으로 교체(자기치유).
네트워크 무의존(monkeypatch·주입 history_fn).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant_core import data_fetcher as dfm


def _series(vals, start="2016-01-01"):
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.DataFrame({"Close": list(vals)}, index=idx)


# ── _has_scale_break: 순수 감지 ────────────────────────────────────────────────

def test_break_detects_dax_splice():
    # $43대 이력 + $25,000대 최근 = DAX splice(~580×)
    assert dfm._has_scale_break(_series([43.0] * 10 + [25000.0] * 5)) is True


def test_break_clean_index_false():
    # 정상 지수(9,800→25,000 완만): 인접 변동 수 %
    assert dfm._has_scale_break(_series(np.linspace(9800, 25000, 100))) is False


def test_break_covid_crash_false():
    # 코로나 ~12% 급락도 임계(3×) 미만 — 오탐 없음
    assert dfm._has_scale_break(_series([100, 100, 88, 90, 92])) is False


def test_break_short_or_empty_false():
    assert dfm._has_scale_break(pd.DataFrame()) is False
    assert dfm._has_scale_break(_series([100.0])) is False
    assert dfm._has_scale_break(None) is False


# ── _heal_or_merge: 4경우 ─────────────────────────────────────────────────────

def test_heal_replaces_spliced_with_clean_full(monkeypatch):
    saved = {}
    monkeypatch.setattr(dfm, "_save", lambda s, d: saved.__setitem__(s, d))
    monkeypatch.setattr(dfm, "mark_data_dirty", lambda: None)
    existing = _series([43.0] * 30)                             # 옛 오종목(ETF) 이력
    new = _series([25000.0, 25100.0], start="2026-07-06")      # 정규 ^GDAXI 증분 → splice 유발
    clean_full = _series(np.linspace(9800, 25100, 50))         # 전체 재수집 = 클린 지수
    out = dfm._heal_or_merge("DAX", "^GDAXI", existing, new, lambda t, s: clean_full)
    assert dfm._has_scale_break(out) is False                  # splice 제거
    assert out.equals(clean_full)                              # 클린 전체로 교체
    assert dfm._has_scale_break(saved["DAX"]) is False         # 저장본도 클린


def test_heal_normal_merge_no_break(monkeypatch):
    saved, called = {}, {"full": 0}
    monkeypatch.setattr(dfm, "_save", lambda s, d: saved.__setitem__(s, d))
    monkeypatch.setattr(dfm, "mark_data_dirty", lambda: None)

    def hist(t, s):
        called["full"] += 1
        return _series([1.0])

    existing = _series([100.0, 101.0, 102.0])
    new = _series([103.0, 104.0], start="2016-02-01")
    out = dfm._heal_or_merge("S&P500", "US500", existing, new, hist)
    assert called["full"] == 0                                 # 정상엔 전체 재수집 안 함
    assert len(out) == 5 and dfm._has_scale_break(out) is False


def test_heal_keeps_existing_when_full_empty(monkeypatch):
    monkeypatch.setattr(dfm, "_save", lambda s, d: None)
    monkeypatch.setattr(dfm, "mark_data_dirty", lambda: None)
    existing = _series([43.0] * 30)
    new = _series([25000.0], start="2026-07-06")
    out = dfm._heal_or_merge("DAX", "^GDAXI", existing, new, lambda t, s: pd.DataFrame())
    assert out.equals(existing)                                # 재수집 실패 → splice 저장 회피·기존 유지


def test_heal_keeps_merged_when_source_break_real(monkeypatch):
    saved = {}
    monkeypatch.setattr(dfm, "_save", lambda s, d: saved.__setitem__(s, d))
    monkeypatch.setattr(dfm, "mark_data_dirty", lambda: None)
    existing = _series([43.0] * 10)
    new = _series([25000.0], start="2026-07-06")
    broken_full = _series([43.0] * 10 + [25000.0])             # 전체도 동일 불연속 → 소스 실재
    dfm._heal_or_merge("X", "X", existing, new, lambda t, s: broken_full)
    assert "X" in saved                                        # merged 그대로 저장(오탐 회피)


# ── fetch_yfinance end-to-end (증분→splice→치유) ──────────────────────────────

def test_fetch_yfinance_heals_end_to_end(monkeypatch):
    saved = {}
    monkeypatch.setattr(dfm, "_save", lambda s, d: saved.__setitem__(s, d))
    monkeypatch.setattr(dfm, "mark_data_dirty", lambda: None)
    existing = _series([43.0] * 30)                            # 저장된 오종목 이력
    monkeypatch.setattr(dfm, "_load_existing", lambda s: existing)
    clean_full = _series(np.linspace(9800, 25100, 60))

    def hist(ticker, start):
        # 증분(start != CORE_FLOOR) → splice 유발 new / 전체(start == CORE_FLOOR) → 클린
        return clean_full if start == dfm.CORE_FLOOR else _series([25000.0], start="2026-07-06")

    monkeypatch.setattr(dfm, "_yf_history", hist)
    out = dfm.fetch_yfinance("DAX", "^GDAXI")
    assert dfm._has_scale_break(out) is False                 # 폭발 제거
    assert out.equals(clean_full) and dfm._has_scale_break(saved["DAX"]) is False


# ── load 시점 선제 치유 (데이터 최신 → fetch skip 상황에서도 저장 splice 치유) ──────

def test_fetch_yfinance_heals_stored_splice_even_when_current(monkeypatch):
    """저장 이력에 splice가 있고 데이터가 최신(fetch skip 조건)이어도 load 시점 선제 치유가 발화.
    PR#356 자기치유가 fetch 진행 시에만 돌아 최신 DAX가 안 낫던 갭(2026-07 실측)을 마감."""
    saved, calls = {}, {"n": 0}
    monkeypatch.setattr(dfm, "_save", lambda s, d: saved.__setitem__(s, d))
    monkeypatch.setattr(dfm, "mark_data_dirty", lambda: None)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=30)   # 마지막=최신(skip 조건)
    spliced = pd.DataFrame({"Close": [43.0] * 25 + [25000.0] * 5}, index=idx)  # 내부 splice
    monkeypatch.setattr(dfm, "_load_existing", lambda s: spliced)
    clean_full = _series(np.linspace(9800, 25100, 60))
    monkeypatch.setattr(dfm, "_yf_history",
                        lambda t, s: (calls.__setitem__("n", calls["n"] + 1) or clean_full))
    out = dfm.fetch_yfinance("DAX", "^GDAXI")
    assert dfm._has_scale_break(out) is False and out.equals(clean_full)   # skip 상황에도 치유
    assert calls["n"] == 1 and dfm._has_scale_break(saved["DAX"]) is False  # 전체 재수집 1회·저장본 클린


def test_heal_stored_break_none_and_no_refetch_on_clean(monkeypatch):
    """클린 시리즈는 선제 치유 무발화 + 전체 재수집 시도조차 안 함(오탐·낭비 방지)."""
    calls = {"n": 0}
    monkeypatch.setattr(dfm, "_yf_history",
                        lambda t, s: (calls.__setitem__("n", calls["n"] + 1) or _series([1.0])))
    clean = _series(np.linspace(24000, 25000, 30))
    assert dfm._heal_stored_break("S&P500", "US500", clean, dfm._yf_history) is None
    assert calls["n"] == 0
