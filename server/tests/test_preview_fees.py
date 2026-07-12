"""P6-2 Task 1 — preview 후보 사전 투명성: 예상수수료(주식)·레버리지 정보(선물).

사전(서버 preview) 투명성: KR 주식 후보에 est_fee_krw(예상 위탁수수료),
선물 후보에 정적 leverage/multiplier/margin_rate(계약 사실). 선물 계약수·금액은
서버가 사이징 못 함(보안경계: KIS 자격증명·계좌 로컬 전용)이라 qty/est_total=None 유지.

commission_rate 단일 출처: 전략 SimSpec.commission이 있으면 그 값, 없으면
exec_defaults bt_commission_bps/10000(3bps=0.0003). _evaluate_ir_strategy의 s=StrategyIR에서 접근.

fixture·전략·코스피200선물 후보 구성은 test_preview_ir.py에서 복사(freshness 게이트 monkeypatch 통과).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from pytest import approx

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import preview_engine


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)


# 000001: 상승 → 마지막 종가(30)가 3일 평균 위 → 신호 참 (test_preview_ir.py 복사).
_DATASET = {"000001": _df([10, 11, 12, 13, 14, 15, 16, 17, 18, 30])}

# compare(Close > ts_mean(Close, 3)) — per-symbol(__SELF__).
_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "right": {"op": "ts_mean", "params": {"window": 3},
                  "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}},
    },
}


def _ir_def(symbols, commission=None):
    sim = {"initial_capital": 10_000_000}
    if commission is not None:
        sim["commission"] = commission
    return {
        "name": "IR 모멘텀",
        "universe": {"kind": "single", "symbols": list(symbols)},
        "signal": _SIGNAL,
        "position": {
            "direction": "long",
            "sizing": {"mode": "pct_cash", "amount_pct": 100.0, "vol_window": 20},
            "entry": {"mode": "on_signal", "rebalance": "monthly"},
            "exit": {}, "overlays": {},
        },
        "simulation": sim,
        "study": {"axis": "none"},
    }


@pytest.fixture(autouse=True)
def _no_freshness_gate(monkeypatch):
    """freshness(거래정지·stale)는 별도 관심사 — 합성 과거 데이터를 통과시킨다."""
    monkeypatch.setattr(preview_engine, "_data_freshness_ok",
                        lambda *a, **k: (True, ""))


# ── 주식 후보 예상수수료 ────────────────────────────────────────────────────────

def test_kr_equity_candidate_has_est_fee_default_rate():
    """주식 후보 → est_fee_krw ≈ est_total × 기본수수료율(0.0003), >0."""
    out = preview_engine._evaluate_ir_strategy(
        _ir_def(["000001"]), _DATASET, cash=10_000_000.0,
        held_keys=set(), master_by_code={})
    assert out["signal_passed"] is True, out
    cands = out["candidates"]
    assert [c["symbol"] for c in cands] == ["000001"], out
    c = cands[0]
    assert c["est_fee_krw"] is not None and c["est_fee_krw"] > 0, c
    assert c["est_fee_krw"] == approx(c["est_total"] * 0.0003, rel=0.01), c
    assert c["currency"] == "KRW"


def test_kr_equity_candidate_uses_simspec_commission():
    """SimSpec.commission이 있으면 그 값으로 est_fee_krw 계산(단일 출처)."""
    out = preview_engine._evaluate_ir_strategy(
        _ir_def(["000001"], commission=0.001), _DATASET, cash=10_000_000.0,
        held_keys=set(), master_by_code={})
    c = out["candidates"][0]
    assert c["est_fee_krw"] == approx(c["est_total"] * 0.001, rel=0.01), c


# ── 선물 후보 정적 레버리지 정보 ────────────────────────────────────────────────

def test_futures_candidate_has_leverage_info():
    """코스피200선물 후보 → qty None 유지 + leverage 5.1·multiplier 250000·margin_rate 0.198."""
    fut = {"코스피200선물": _df([10, 11, 12, 13, 14, 15, 16, 17, 18, 30])}
    out = preview_engine._evaluate_ir_strategy(
        _ir_def(["코스피200선물"]), fut, cash=10_000_000.0,
        held_keys=set(), master_by_code={})
    assert out["signal_passed"] is True, out
    assert [c["symbol"] for c in out["candidates"]] == ["코스피200선물"], out
    c = out["candidates"][0]
    assert c["qty"] is None, c                 # 서버 사이징 불가 — 로컬 선물계좌
    assert c["est_total"] is None, c
    assert c["leverage"] == approx(5.1), c          # round(1/0.198, 1) = 5.1
    assert c["multiplier"] == approx(250_000), c
    assert c["margin_rate"] == approx(0.198), c


# ── 미국 후보 키 일관(est_fee_krw=None) ─────────────────────────────────────────

def test_us_candidate_est_fee_none_for_key_consistency():
    """미국 종목 — 서버 사이징 불가(qty=None). est_fee_krw 키는 None으로 채워 소비처 안전."""
    us = {"NVDA": _df([10, 11, 12, 13, 14, 15, 16, 17, 18, 30])}
    out = preview_engine._evaluate_ir_strategy(
        _ir_def(["NVDA"]), us, cash=10_000_000.0,
        held_keys=set(), master_by_code={})
    assert out["signal_passed"] is True, out
    c = out["candidates"][0]
    assert c["symbol"] == "NVDA" and c["qty"] is None, c
    assert "est_fee_krw" in c and c["est_fee_krw"] is None, c
    assert c["currency"] == "USD"
