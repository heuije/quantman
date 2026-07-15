"""Workbench 시나리오 — 계좌·원장 정합성(A6).

분할매수 평단(INV-LEDGER-2)과 외부 수동매도 drift의 reconcile 차감(INV-RECON-1)을
실제 _apply_fill / reconcile_with_kis로 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants, scenario


def test_split_buy_weighted_average(isolated_trader):
    """INV-LEDGER-2: 같은 전략의 분할매수는 수량가중 평균단가로 합쳐진다."""
    t, broker = isolated_trader
    scenario.buy_and_fill(t, broker, "s1", "005930", 10, 70000.0)
    scenario.buy_and_fill(t, broker, "s1", "005930", 10, 72000.0)

    lg = t.ledger["s1"]
    assert lg["qty"] == 20
    # (70000*10 + 72000*10) / 20 = 71000
    assert lg["entry_price"] == 71000.0, f"가중평단 오류: {lg['entry_price']}"
    invariants.check_all(t)


def test_external_sale_drift_observed_not_deducted(isolated_trader):
    """목표수렴 §14(구 INV-RECON-1 대체): 외부 수동매도를 reconcile이 차감하지 않는다.

    원장=전략 의도 정본 — 차감하면 의도가 소실돼 다음 사이클 drift 복원(수동매매
    되돌림)이 무력화된다. reconcile은 표면화만, 물리 복원은 _reconcile_pass 담당
    (복원 E2E는 tests/scenarios/test_target_reconciliation.py)."""
    t, broker = isolated_trader
    scenario.buy_and_fill(t, broker, "s1", "005930", 10, 70000.0)
    assert t.ledger["s1"]["qty"] == 10

    # 사용자가 HTS/MTS로 전량 수동 매도 → KIS 잔고엔 005930 없음
    broker.set_positions([])
    result = t.reconcile_with_kis(today_iso="2026-06-01")

    assert t.ledger["s1"]["qty"] == 10, "관측 전용 — 원장 불변"
    assert result["has_drift"] is True
    assert not result["applied"]
    assert any(s.get("sid") == "s1"
               for o in result["ledger_orphans"]
               for s in o.get("ledger_sids", []))


def test_external_buy_does_not_touch_ledger(isolated_trader):
    """INV-RECON-1: 외부 매수(KIS엔 있고 ledger엔 없음)는 ledger를 건드리지 않는다."""
    t, broker = isolated_trader
    # 자동매매가 산 적 없는 종목이 KIS 잔고에만 존재
    broker.set_positions([{"symbol": "000660", "qty": 5, "avg_price": 120000}])
    result = t.reconcile_with_kis(today_iso="2026-06-01")

    assert "000660" not in {lg.get("symbol") for lg in t.ledger.values()}
    assert result["external_extras_count"] >= 1
    assert not result["applied"]            # ledger 변경 없음
    invariants.check_all(t)
