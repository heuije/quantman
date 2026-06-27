"""Workbench 시나리오 공용 픽스처/헬퍼.

isolated_trader: trader 영속 경로를 tmp로 격리 + KST 날짜 고정 + 서버 push 차단 후
SimBroker 위의 실제 Trader를 돌려준다. buy_and_fill: 매수 발주→WS 체결까지 한 번에.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim.broker import SimBroker


@pytest.fixture
def isolated_trader(tmp_path, monkeypatch):
    from localapp import trader as tr
    from localapp import intraday_loop, killswitch, intents, order_log
    for name in ("LEDGER_PATH", "EQUITY_PATH", "PENDING_ORDERS_PATH",
                 "TRADES_PATH"):
        monkeypatch.setattr(tr, name, tmp_path / f"{name}.json")
    # WS-1(δ): 체결행 청구 레지스트리 — raising=False는 red 단계(구현 전) 호환용
    monkeypatch.setattr(tr, "CLAIMED_FILLS_PATH",
                        tmp_path / "claimed_fills.json", raising=False)
    monkeypatch.setattr(killswitch, "KILLSWITCH_PATH", tmp_path / "ks.json")
    monkeypatch.setattr(intents, "INTENTS_PATH", tmp_path / "intents.jsonl")
    # order_log 쓰기를 tmp로 격리 — 시나리오가 실사용자 ~/.quant-platform 로그를 오염하지 않게.
    for name in ("ORDERS_PATH", "CYCLES_PATH", "SLIPPAGE_PATH"):
        monkeypatch.setattr(order_log, name, tmp_path / f"{name}.jsonl")
    monkeypatch.setattr(intraday_loop, "push_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(tr, "kst_today", lambda: datetime.date(2026, 6, 1))
    # θ: liquidate_day_trades/_cycle_body의 _wait_pending이 SimBroker(상태 unknown
    # 유지) 위에서 운영 기본 60초를 폴링하지 않도록 시나리오 기본 wait=0.
    # 지연 체결을 검증하는 θ 테스트는 자체적으로 재패치한다.
    real_merged = tr.merged_execution
    monkeypatch.setattr(tr, "merged_execution", lambda o=None: {
        **real_merged(o), "post_submit_wait_sec": 0, "poll_interval_sec": 0.01})

    broker = SimBroker()
    from localapp import coverage
    monkeypatch.setattr(coverage, "covered_categories",
                        lambda: {"kr_equity", "us_equity", "kr_futures", "us_futures"})
    return tr.Trader(broker), broker
