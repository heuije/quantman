"""reconcile fail-safe(I2·I3) — 선물 신원계층 비정상 시 파괴적 자동 정정 차단.

2026-07 원장↔브로커 분기 인시던트의 회귀 테스트: LS 잔고 코드 정규화 실패(symbol_unmapped)
또는 선물 leg 조회 실패(fetch_failed) 상태에서 reconcile이 "외부 매도 추정"으로 원장을
삭제하면 안 된다(무동작+표면화). 주식은 symbol=종목코드로 매칭 신뢰 가능 → 자동 차감 유지.
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


def test_stock_orphan_still_deducted_when_futures_identity_broken():
    # I3 — 주식 스냅샷은 정상(주식 fetch 실패면 account_snapshot 자체가 raise)이므로
    # 주식 외부 매도 자동 차감(승인된 제품 동작)은 선물 신원계층과 무관하게 유지.
    snap = {"balance": {"fetch_failed": ["futures"]}, "positions": []}
    t = Trader(_B(snap))
    t.ledger = {"s1": {"symbol": "005930", "qty": 10, "side": "long",
                       "entry_price": 70000.0, "strategy_name": "삼성"},
                **_fut_ledger()}
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert "s1" not in t.ledger                          # 주식 차감 유지
    assert t.ledger["10"]["qty"] == 3                    # 선물은 보호
    assert [p["symbol"] for p in r["applied"]] == ["005930"]


def test_healthy_futures_orphan_still_deducted():
    # 신원계층 정상 + 브로커 선물 0 = 진짜 외부 청산 → 종전 자동 차감 유지(회귀 금지).
    snap = {"balance": {}, "positions": []}
    t = Trader(_B(snap))
    t.ledger = _fut_ledger(qty=2)
    r = t.reconcile_with_kis(today_iso="2026-07-04")
    assert "10" not in t.ledger
    assert len(r["applied"]) == 1
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
