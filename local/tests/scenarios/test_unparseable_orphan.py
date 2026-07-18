"""Workbench 시나리오 — 청산 불가 고아(구 스키마) 표면화 (전략 삭제 라이프사이클 Stage 3).

전략이 자동매매 중 삭제되면 보유 포지션은 원장에 freeze된 정의로 청산된다. 그 정의가
구 스키마(operand 등)라 현행 StrategyIR로 파싱 실패하면 자동 청산이 불가하다 —
손절·익절이 동작 안 함. 평상시엔 강제매도하지 않고(임의매도 회피) '청산 불가 고아'로
명시 표면화해 사용자가 웹에서 인지·수동 정리하게 한다. kill-switch 활성 시엔 기존대로
강제 전량청산(표면화 아님).

    cd platform/local && python -m pytest tests/scenarios/test_unparseable_orphan.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants  # noqa: E402


def _ds(closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {"005930": pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


_DS = _ds([70000, 70000, 70000, 70000, 70000])

# 구 operand 스키마 — top-level signal 없음 → StrategyIR.model_validate 실패(고아).
_OLD_DEF = {
    "name": "삭제된 전략",
    "trade_symbol": "005930",
    "buy": {"conditions": [], "logic": "AND"},
    "sell_rules": {"take_profit": 10.0, "stop_loss": -5.0},
    "amount_pct": 10,
}


def _inject_orphan(t, broker, sid="orphan1", qty=10):
    """삭제된 전략 소속 보유 포지션을 원장+broker에 직접 주입(진입 경로 우회)."""
    t.ledger[sid] = {
        "symbol": "005930", "qty": qty, "entry_date": "2026-05-28",
        "entry_price": 70000, "peak_price": 70000,
        "strategy_name": "삭제된 전략", "definition": _OLD_DEF,
    }
    t._save()   # R5: cycle이 락 안에서 디스크(SSOT)를 reload하므로 주입을 영속해야 보인다
    broker.set_positions([{"symbol": "005930", "qty": qty, "avg_price": 70000}])


def test_unparseable_orphan_surfaced_not_sold(isolated_trader):
    """평상시 — 파싱 실패 고아는 자동매도하지 않고 unparseable_orphan으로 표면화."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    _inject_orphan(t, broker)

    payload = t.cycle(strategies=[], dataset=_DS, buy_candidates=[],
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")

    orphans = [d for d in payload["decisions"] if d["action"] == "unparseable_orphan"]
    assert len(orphans) == 1, payload["decisions"]
    assert orphans[0]["symbol"] == "005930"
    assert orphans[0]["strategy_name"] == "삭제된 전략"
    assert payload["cycle_summary"]["n_unparseable_orphan"] == 1
    # 자동 강제매도 안 함 — 보유 유지, 매도 발주 0
    assert "orphan1" in t.ledger
    assert [s for s in broker.submitted if s["side"] == "sell"] == []
    invariants.check_all(t)


def test_unparseable_orphan_force_sold_under_killswitch(isolated_trader):
    """kill-switch 활성 — 파싱 실패 고아도 강제 전량청산(표면화 아님)."""
    from localapp import killswitch

    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    _inject_orphan(t, broker, qty=10)
    killswitch.activate("테스트 — 전량 청산 강제")

    payload = t.cycle(strategies=[], dataset=_DS, buy_candidates=[],
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")

    # 강제 매도됨 → 표면화 decision 없음(reason=kill-switch라 매도 경로로 감)
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1, broker.submitted
    assert sells[0]["qty"] == 10
    assert [d for d in payload["decisions"] if d["action"] == "unparseable_orphan"] == []
    assert payload["cycle_summary"]["n_unparseable_orphan"] == 0
