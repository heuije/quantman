"""실행 가능한 머니패스 불변식 단언. ID는 docs/INVARIANTS.md와 1:1 대응."""
from __future__ import annotations


def check_ledger_nonneg(trader) -> None:
    """INV-LEDGER-1: ledger에 남은 포지션의 qty는 양수다(0 이하는 삭제됨)."""
    for sid, lg in trader.ledger.items():
        assert lg["qty"] > 0, f"INV-LEDGER-1 위반: {sid} qty={lg['qty']}"


def check_pending_has_order_no(trader) -> None:
    """INV-CONC-1 보조: 모든 pending 엔트리는 raw order_no를 보유(KIS 라운드트립)."""
    for key, p in trader.pending.items():
        assert p.get("order_no"), f"pending[{key}]에 order_no 없음"


def check_all(trader) -> None:
    check_ledger_nonneg(trader)
    check_pending_has_order_no(trader)


def check_futures_sign(positions) -> None:
    """INV-FUT-1: 선물 포지션 side는 long|short, qty>0(flat은 미보유=목록부재)."""
    for p in positions:
        assert p.get("side") in ("long", "short"), \
            f"INV-FUT-1 위반: {p.get('symbol')} side={p.get('side')}"
        assert int(p.get("qty", 0)) > 0, \
            f"INV-FUT-1 위반: {p.get('symbol')} qty={p.get('qty')}"


def check_futures_pnl(positions) -> None:
    """INV-FUT-2: eval_pnl = (eval−avg)×qty×승수×부호(롱+1/숏−1)."""
    from .futures import settlement_pnl
    for p in positions:
        exp = settlement_pnl(p["symbol"], p["side"], p["qty"], p["avg_price"], p["eval_price"])
        assert abs(float(p["eval_pnl"]) - exp) < 1e-6, \
            f"INV-FUT-2 위반: {p['symbol']} eval_pnl={p['eval_pnl']} 기대={exp}"


def check_futures_margin(snapshot) -> None:
    """INV-FUT-3: 점유 증거금 ≤ 가용 증거금(과레버리지 차단)."""
    m = snapshot.get("margin")
    if not m:
        return
    total, avail = float(m["total_margin"]), float(m["available_margin"])
    assert total <= avail, f"INV-FUT-3 위반: 증거금 {total} > 가용 {avail}"
