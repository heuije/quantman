"""reconcile fail-safe(I2) + 관측 전용(목표수렴 §14) 회귀.

2026-07 원장↔브로커 분기 인시던트: symbol_unmapped·fetch_failed 시 orphan 오판 차단.
목표수렴 전환(2026-07-15)으로 reconcile은 **전면 관측 전용** — 어떤 orphan도 자동
차감하지 않는다(원장=전략 의도 정본·수동 매도는 다음 사이클 drift 교정이 복원).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.trader import Trader


class _B:
    """reconcile용 브로커 더블 — account_snapshot만 제공."""
    def __init__(self, snap):
        self._snap = snap

    def account_snapshot(self):
        return self._snap


def _fut_ledger(qty=3):
    return {"10": {"symbol": "코스피200선물", "qty": qty, "side": "short",
                   "entry_price": 1300.0, "strategy_name": "일중 롱숏",
                   "entry_date": "2026-07-04"}}


def test_unmapped_position_blocks_futures_orphan_deletion():
    # 인시던트 재현 — 브로커가 미정규화 코드(101T9000)로 같은 포지션을 들고 있음.
    # (symbol, side) 키가 원장(상품명)과 불일치 → 종전엔 orphan 오판·원장 삭제.
    snap = {"balance": {}, "positions": [
        {"symbol": "101T9000", "side": "short", "qty": 3, "symbol_unmapped": True,
         "asset_class": "futures"}]}
    t = Trader(_B(snap))
    t.ledger = _fut_ledger()
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert t.ledger["10"]["qty"] == 3                    # 파괴적 차감 없음
    assert not r["applied"]
    assert r["reconcile_blocked"]["unmapped_codes"] == ["101T9000"]
    assert r["reconcile_blocked"]["blocked_futures_orphans"] == 1
    assert r["has_drift"] is True                        # 표면화(웹 경보 경로)


def test_fetch_failed_blocks_futures_orphan_deletion():
    # 구성된 선물 leg 조회 실패 → 선물 포지션이 스냅샷에 없음. 종전엔 원장 선물 전량이
    # orphan으로 보여 삭제될 수 있었다(동일 부류의 잠복 사고).
    snap = {"balance": {"fetch_failed": ["futures"]}, "positions": []}
    t = Trader(_B(snap))
    t.ledger = _fut_ledger()
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert t.ledger["10"]["qty"] == 3
    assert not r["applied"]
    assert r["reconcile_blocked"]["fetch_failed"] == ["futures"]
    assert r["has_drift"] is True


def test_stock_orphan_not_deducted_observe_only():
    # 목표수렴(kr-target-reconciliation.md §14) — reconcile은 관측 전용으로 방향 역전.
    # 구 I3(주식 외부매도 자동 차감)는 폐기: 원장=전략 의도가 정본이고, 수동 매도는
    # 다음 사이클 _reconcile_pass의 drift 교정이 되돌린다. 여기서 차감하면 의도가
    # 소실돼 되돌림이 무력화된다.
    snap = {"balance": {"fetch_failed": ["futures"]}, "positions": []}
    t = Trader(_B(snap))
    t.ledger = {"s1": {"symbol": "005930", "qty": 10, "side": "long",
                       "entry_price": 70000.0, "strategy_name": "삼성"},
                **_fut_ledger()}
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert t.ledger["s1"]["qty"] == 10                   # 원장 불변(관측 전용)
    assert t.ledger["10"]["qty"] == 3
    assert not r["applied"]
    assert {o["symbol"] for o in r["ledger_orphans"]} == {"005930", "코스피200선물"}
    assert r["has_drift"] is True                        # 표면화는 유지


def test_healthy_futures_orphan_surfaced_not_deducted():
    # 신원계층 정상 + 브로커 선물 0(수동 청산 추정) — 관측 전용: 원장 유지·표면화만.
    # 물리 복원은 다음 사이클 목표수렴 drift 교정 담당(§14 방향 역전).
    snap = {"balance": {}, "positions": []}
    t = Trader(_B(snap))
    t.ledger = _fut_ledger(qty=2)
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert t.ledger["10"]["qty"] == 2                    # 원장 불변
    assert not r["applied"]
    assert len(r["ledger_orphans"]) == 1
    assert r["has_drift"] is True
    assert "reconcile_blocked" not in r


def test_normalized_futures_in_sync_no_drift():
    # 수정 후 정상 경로 — 라우터가 정규화한 선물 포지션은 원장과 in_sync(인시던트 종결 상태).
    snap = {"balance": {}, "positions": [
        {"symbol": "코스피200선물", "contract_code": "101T9000", "side": "short",
         "qty": 3, "asset_class": "futures"}]}
    t = Trader(_B(snap))
    t.ledger = _fut_ledger()
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert t.ledger["10"]["qty"] == 3
    assert r["in_sync"] == ["코스피200선물"]
    assert not r["applied"] and r["has_drift"] is False
