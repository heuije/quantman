"""R6 관측 — 저널 append 실패 승격·journal_status·휴장 skip 저널 기록.

orders.jsonl append 실패의 무증상 삼킴(07-14~17 mwmw 3일 공백 부류)을
diagnostics로 표면화하고, 휴장 skip(CY-7)을 cycles.jsonl에 남긴다.
"""

from __future__ import annotations

import json
from datetime import date

from localapp import analytics, order_log


def test_append_failure_promoted_to_status(monkeypatch):
    order_log._journal_status.update(
        {"append_failures": 0, "last_error": None, "last_error_ts": None})

    def _boom(obj, path):
        raise PermissionError("locked")

    monkeypatch.setattr(order_log, "append_jsonl", _boom)
    order_log.log_order("submitted", "005930", "buy", 1)
    order_log.log_order("filled", "005930", "buy", 1)
    st = order_log.journal_status()
    assert st["append_failures"] == 2
    assert "locked" in st["last_error"]
    assert st["last_error_ts"] is not None


def test_journal_status_reads_last_event_ts():
    order_log._journal_status.update(
        {"append_failures": 0, "last_error": None, "last_error_ts": None})
    order_log.log_order("submitted", "005930", "buy", 1)
    order_log.log_order("filled", "005930", "buy", 1, fill_price=100.0,
                        intended_price=100.0)
    st = order_log.journal_status()
    assert st["append_failures"] == 0
    assert st["orders_last_event_ts"]           # 마지막 이벤트 ts 존재
    assert st["orders_last_event_ts"].startswith("20")


def test_diagnostics_block_carries_journal():
    d = analytics.diagnostics_block(None, None)
    assert "journal" in d
    assert "append_failures" in d["journal"]


def test_holiday_skip_recorded_in_cycles(monkeypatch):
    """CY-7 — 휴장 skip이 cycles.jsonl에 남아 '앱 다운'과 사후 식별 가능해야 한다."""
    from quant_core import market_calendar as mc

    from localapp import runner
    from localapp.config import CYCLES_PATH

    monkeypatch.setattr(runner, "_flush_pending", lambda: None)
    monkeypatch.setattr(mc, "check_fresh", lambda m, t, lookahead_days=7: (True, ""))
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: False)
    out = runner.run_cycle(market="KRX", trigger="test")
    assert out["status"] == "skipped_holiday"

    lines = [json.loads(x) for x in
             CYCLES_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    rec = [x for x in lines
           if (x.get("summary") or {}).get("kind") == "cycle_skipped_holiday"
           or x.get("kind") == "cycle_skipped_holiday"]
    assert rec, f"휴장 skip 기록 없음: {lines[-3:]}"
