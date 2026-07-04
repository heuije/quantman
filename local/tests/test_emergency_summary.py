"""analytics.emergency_liquidation_summary — 비상청산 결과 요약(투명성).

2026-07 회귀: 청산이 "모의투자 영업일이 아닙니다"로 거부됐는데 성공처럼 보이던 문제.
ok=False + 사유 + "미청산 포지션 그대로" 메시지를 보장한다(웹 ack·데스크탑 배너 공용).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.analytics import emergency_liquidation_summary


def _payload(decisions, n_sold=0, n_rejected=0, n_errors=0, kill_switch=True):
    return {"decisions": decisions,
            "cycle_summary": {"n_sold": n_sold, "n_rejected": n_rejected,
                              "n_errors": n_errors, "kill_switch": kill_switch}}


def test_rejected_not_business_day_surfaces_reason_and_not_ok():
    # 실제 인시던트 재현 — 영업일 아님 거부.
    p = _payload([{"action": "rejected", "symbol": "코스피200선물",
                   "reason": "4 거부: 모의투자 영업일이 아닙니다."}],
                 n_sold=0, n_rejected=1)
    r = emergency_liquidation_summary(p)
    assert r["ok"] is False
    assert r["n_rejected"] == 1
    assert r["rejected_reasons"] == ["4 거부: 모의투자 영업일이 아닙니다."]
    assert "미청산" in r["message"] and "다시 시도" in r["message"]
    assert "모의투자 영업일이 아닙니다" in r["message"]


def test_full_success_ok():
    p = _payload([{"action": "sold", "symbol": "코스피200선물", "reason": "kill-switch"}],
                 n_sold=1)
    r = emergency_liquidation_summary(p)
    assert r["ok"] is True and r["n_liquidated"] == 1
    assert "완료" in r["message"] and "1건" in r["message"]


def test_nothing_held():
    r = emergency_liquidation_summary(_payload([], n_sold=0))
    assert r["ok"] is True
    assert "청산할 보유 종목이 없습니다" in r["message"]


def test_partial_reject_reports_both():
    p = _payload([{"action": "sold", "symbol": "005930", "reason": ""},
                  {"action": "rejected", "symbol": "코스피200선물",
                   "reason": "모의투자 영업일이 아닙니다."}],
                 n_sold=1, n_rejected=1)
    r = emergency_liquidation_summary(p)
    assert r["ok"] is False
    assert "1건 청산" in r["message"] and "1건 거부" in r["message"]


def test_reasons_dedup_preserve_order():
    p = _payload([{"action": "rejected", "reason": "A"},
                  {"action": "rejected", "reason": "B"},
                  {"action": "rejected", "reason": "A"},
                  {"action": "error", "reason": "C"}],
                 n_rejected=3, n_errors=1)
    r = emergency_liquidation_summary(p)
    assert r["rejected_reasons"] == ["A", "B", "C"]
    assert r["ok"] is False
