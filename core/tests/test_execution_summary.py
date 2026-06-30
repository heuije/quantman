# -*- coding: utf-8 -*-
"""execution_summary — 전략 실행 명세 4분류 요약(확정/가정/발주시점/미지).

읽기 전용 IR 파생 — 실거래 불필요(dev 검증 가능). 최소 유효 IR(compare condition 신호)을
test_futures_capital_warning._ir과 동일 패턴으로 구성한다(model_validate 통과 보장).
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine.execution_summary import execution_summary

# on_signal 진입은 condition(참/거짓) 신호가 필요 — compare 노드(시장참조)로 최소 유효 신호 구성.
_SIGNAL = {"op": "compare", "params": {"op": ">="}, "inputs": {
    "left": {"op": "data", "params": {"ref": "__SELF__.pct_change_1d"}},
    "right": {"op": "const", "params": {"value": 0.001}}}}


def test_stock_pct_cash_summary():
    d = {"universe": {"kind": "list", "symbols": ["005930", "000660"]},
         "signal": _SIGNAL,
         "position": {"direction": "long",
                      "sizing": {"mode": "pct_cash", "amount_pct": 10.0},
                      "exit": {"stop_loss": -3.0, "take_profit": 5.0, "hold_days": 5}}}
    s = execution_summary(d)

    cats = {e["label"]: e["value"] for e in s["confirmed"]}
    assert "롱" in cats["방향"]
    assert "10" in cats["사이징"]                  # amount_pct
    assert "손절" in cats["청산"] and "보유" in cats["청산"]

    assumed = {e["label"]: e["value"] for e in s["assumed"]}
    assert "가정" in assumed["수수료"]             # 가정 명시
    assert any("실제 수수료" in u for u in s["unknown"])


def test_futures_summary_has_leverage():
    d = {"universe": {"kind": "single", "symbols": ["코스피200선물"]},
         "signal": _SIGNAL,
         "position": {"direction": "long",
                      "sizing": {"mode": "pct_cash", "futures_margin_pct": 20.0},
                      "exit": {"hold_days": 0}}}
    s = execution_summary(d)

    conf = {e["label"]: e["value"] for e in s["confirmed"]}
    assert "20%" in conf["사이징"]                 # futures_margin_pct
    assert any("레버리지" in e["value"] or "레버리지" in e["label"]
               for e in s["assumed"])
    assert any("계약수" in x for x in s["at_order"])
    assert "당일" in conf["청산"]                  # hold_days=0


# ── methodology_brief (Wave 2 T1): 모델 식단용 1블록 방법론 ────────────────────
from quant_core.ir_engine.execution_summary import methodology_brief


def test_methodology_brief_surfaces_logic_and_baseline():
    """백테스트 '무엇을 어떻게'(기간·기준자본·종목·방향·사이징·진입(신호기준)·청산)를 한 블록으로.
    기준자본 미지정 시 기본 1억을 명시(증상 #3 '1억 기준 미명시' 근본). 기간은 호출측이 결과에서 주입."""
    d = {"universe": {"kind": "list", "symbols": ["005930", "000660"]},
         "signal": _SIGNAL,
         "position": {"direction": "long",
                      "sizing": {"mode": "pct_cash", "amount_pct": 10.0},
                      "exit": {"hold_days": 5}}}
    out = methodology_brief(d, period="2015-01-02~2024-12-30")
    assert out.startswith("[분석 방법]")
    assert "2015-01-02~2024-12-30" in out          # 기간(#7)
    assert "100,000,000" in out                    # 기본 1억 기준자본(#3)
    assert "롱" in out and "10" in out and "보유 5일" in out   # 방향·사이징·청산
    assert "전일 데이터" in out                     # 신호 평가 기준(#7 신호기준)


def test_methodology_brief_invalid_ir_is_empty():
    assert methodology_brief({"bad": "ir"}) == ""   # 파싱 실패 → 빈 문자열(best-effort)
