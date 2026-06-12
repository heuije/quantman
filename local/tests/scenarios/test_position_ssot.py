"""포지션 진실원천(SSOT) — 라이브 실증 결함의 재현/회귀 가드 (WS-1).

2026-06 실증 결함:
  ε  — account_snapshot이 해외/선물 조회 실패를 0으로 삼켜 부분 equity가 만들어지고,
       킬스위치가 거짓 -98% 폭락으로 발동해 보유 전량을 청산(06-09 US 북 소멸 사고).
  D3-3 — equity 시계열이 국내 평가금액(total_eval)만 적재 → 웹 자산곡선 분자/분모 혼합.
  M9 — 장수명 intraday loop Trader와 ephemeral cycle Trader가 각자 메모리 사본을 들고
       같은 파일을 덮어씀 → stale 저장이 매도된 포지션을 부활시키고, WS 체결 통보가
       "pending에 없음"으로 유실(트레이드 이벤트 3중 중복 기록의 뿌리).
  δ  — 예약주문 체결을 종목+사이드+수량으로 청구(claim)하고 체결행 odno로 dedup.
"""

from __future__ import annotations

import json
import time

import pytest

from localapp import killswitch
from localapp import market_index
from localapp import trader as tr
from localapp.kis_broker import canonical_odno


def _pending_entry(order_no: str, symbol: str, side: str, qty: int, *,
                   sid: str = "s1", is_resv: bool = False,
                   age_days: float = 0.0) -> dict:
    return {
        "order_no": order_no, "strategy_id": sid, "strategy_name": "전략",
        "symbol": symbol, "side": side, "qty": qty, "limit_price": 0,
        "intended_price": 100.0, "submitted_ts": time.time() - age_days * 86400,
        "definition": {}, "reason": "매수신호", "filled_so_far": 0,
        "is_resv": is_resv,
    }


def _read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# ── δ: 예약주문 청구 + 체결행 dedup ──────────────────────────────────────────


def test_us_reserved_fills_claimed_once_with_exclude(isolated_trader, monkeypatch):
    """동형 예약주문 2건이 체결행 2개를 1:1로 나눠 갖는다(이중 청구 금지).

    트레이더는 US 예약 pending에 hint(side/qty/reserved/exclude)를 전달하고,
    청구한 체결행 odno를 claimed 레지스트리에 영속해 교차 사이클 dedup한다.
    """
    t, broker = isolated_trader
    monkeypatch.setattr(market_index, "is_us", lambda s: s == "GOOG")

    rows = [("0000040620", 344.81), ("0000040625", 345.00)]
    hints_seen: list[dict | None] = []

    def fake_order_status(order_no, symbol=None, hint=None):
        hints_seen.append(hint)
        excl = {canonical_odno(x) for x in ((hint or {}).get("exclude_odnos") or ())}
        for odno, px in rows:
            if canonical_odno(odno) in excl:
                continue
            return {"order_no": order_no, "status": "filled", "filled_qty": 263,
                    "remain_qty": 0, "fill_price": px, "exec_odno": odno}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0,
                "remain_qty": 0, "fill_price": 0.0}

    broker.order_status = fake_order_status
    t.pending = {
        "448": _pending_entry("448", "GOOG", "buy", 263, sid="a", is_resv=True),
        "449": _pending_entry("449", "GOOG", "buy", 263, sid="b", is_resv=True),
    }

    t._resolve_pending([])

    # 두 전략 모두 체결 — 단 서로 다른 체결행으로
    assert t.ledger["a"]["qty"] == 263
    assert t.ledger["b"]["qty"] == 263
    assert {t.ledger["a"]["entry_price"], t.ledger["b"]["entry_price"]} == {344.81, 345.00}
    assert not t.pending

    # 두 번째 주문의 hint exclude에 첫 청구 odno가 들어가야 한다
    assert hints_seen[0] is not None and hints_seen[0]["reserved"] is True
    second_excl = {canonical_odno(x) for x in (hints_seen[1]["exclude_odnos"] or ())}
    assert canonical_odno("0000040620") in second_excl, \
        "이중 청구 차단 실패: 같은 체결행을 두 주문이 나눠 갖지 못함"

    # claimed 레지스트리 영속 — 다음 사이클/reconcile이 같은 행을 재청구하지 못하도록
    claimed = _read_json(tr.CLAIMED_FILLS_PATH, {})
    assert canonical_odno("0000040620") in claimed
    assert canonical_odno("0000040625") in claimed


def test_pending_unknown_gc_after_7_days(isolated_trader):
    """상태 미확인(unknown) pending은 7일 후 GC — 영구 고아 잔존 차단(실측 448·0000040620)."""
    t, broker = isolated_trader
    t.pending = {
        "111": _pending_entry("111", "005930", "buy", 1, sid="old", age_days=8),
        "222": _pending_entry("222", "005930", "buy", 1, sid="new", age_days=2),
    }
    decisions: list[dict] = []
    t._resolve_pending(decisions)          # SimBroker: 미등록 주문 → unknown
    assert "111" not in t.pending, "7일 경과 unknown pending이 GC되지 않음"
    assert "222" in t.pending
    assert any(d["action"] == "unfilled" and "만료" in d.get("reason", "")
               for d in decisions)


# ── ε/★: 부분 잔고 → 킬스위치/day_start/equity 보류 ────────────────────────────


def test_killswitch_abstains_on_partial_balance(isolated_trader):
    """해외 조회 실패로 부분 equity일 때 킬스위치 발동 금지(06-09 거짓 -98% 청산 재현).

    완전 조회로 같은 폭락이 확인되면 정상 발동한다(보호 기능 보존).
    """
    t, broker = isolated_trader
    killswitch.update_day_start(600_000_000, "2026-06-01")

    broker._balance = {"cash": 7_000_000, "total_eval": 10_000_000,
                       "foreign_eval_krw": 0, "cash_usd": 0, "fx_usdkrw": 0,
                       "fetch_failed": ["overseas"]}
    fired = t.evaluate_killswitch_now(3.0)
    assert fired is False
    assert not killswitch.is_active(), \
        "부분 잔고(-98% 거짓 폭락)로 킬스위치가 발동 — 06-09 사고 재현"

    broker._balance.pop("fetch_failed")    # 완전 조회 + 진짜 폭락 → 발동
    fired = t.evaluate_killswitch_now(3.0)
    assert fired is True
    assert killswitch.is_active()


def test_cycle_partial_balance_skips_day_start_and_killswitch(isolated_trader):
    """cycle 진입부: 부분 잔고면 day_start 앵커·손실한도 평가를 보류하고 표면화."""
    t, broker = isolated_trader
    killswitch.update_day_start(600_000_000, "2026-06-01")   # 오늘(고정 날짜) 완전 측정치
    broker._balance = {"cash": 7_000_000, "total_eval": 10_000_000,
                       "foreign_eval_krw": 0, "cash_usd": 0, "fx_usdkrw": 0,
                       "fetch_failed": ["overseas"]}

    payload = t.cycle([], {}, buy_candidates=[],
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")

    assert not killswitch.is_active(), "부분 equity(-98%)로 cycle 킬스위치 발동"
    ks = killswitch.load()
    assert ks["day_start_equity"] == 600_000_000   # 부분 측정치로 재앵커 금지
    assert t.equity == [], "부분 잔고 equity가 시계열에 적재됨(웹 -98% 곡선 오염)"
    assert any(d["action"] == "risk_eval_skipped" for d in payload["decisions"])


def test_cycle_equity_records_unified_total(isolated_trader):
    """equity 시계열 = 통합 자산(국내+해외+USD현금+선물) — 국내만(total_eval) 적재 금지(D3-3)."""
    t, broker = isolated_trader
    broker._balance = {"cash": 1_000_000, "total_eval": 2_000_000,
                       "foreign_eval_krw": 3_000_000, "cash_usd": 100.0,
                       "fx_usdkrw": 1_000.0, "futures_eval_krw": 500_000}
    t.cycle([], {}, buy_candidates=[], market="KRX")
    assert t.equity[-1]["value"] == 5_600_000, \
        f"equity가 국내만 기록: {t.equity[-1]['value']} (통합 5,600,000이어야)"


# ── M9: disk-SSOT — stale 인스턴스의 덮어쓰기/체결 유실 ──────────────────────────


def test_state_snapshot_does_not_resurrect_stale_ledger(isolated_trader):
    """장수명 인스턴스의 state_snapshot이 stale 메모리로 디스크를 덮어쓰지 않는다.

    재현: A(loop)가 포지션을 들고 있는 동안 B(cycle)가 청산·저장 → A의 스냅샷 push가
    매도된 포지션을 부활 → settlement reconcile이 '외부 매도 추정'으로 오판(P&L 소실).
    """
    t, broker = isolated_trader
    t.ledger["s1"] = {"symbol": "005930", "qty": 10, "entry_date": "2026-06-01",
                      "entry_price": 1000.0, "peak_price": 1000.0, "side": "long",
                      "strategy_name": "전략", "definition": {}}
    t._save()

    t2 = tr.Trader(broker)                 # ephemeral cycle 인스턴스 — 청산 후 저장
    del t2.ledger["s1"]
    t2._save()

    t.state_snapshot()                     # 장수명 인스턴스(stale 메모리)의 상태 push

    on_disk = _read_json(tr.LEDGER_PATH, {})
    assert "s1" not in on_disk, "stale state_snapshot 저장이 매도된 포지션을 부활시킴"


def test_exec_event_on_stale_instance_applies_fill(isolated_trader):
    """다른 인스턴스가 발주한 주문의 WS 체결 통보가 장수명 인스턴스에서 유실되지 않는다.

    재현: B(cycle)가 발주·pending 영속 → A(loop, B 이전 생성)가 체결 통보 수신 —
    수정 전엔 A 메모리에 pending이 없어 '미매칭 체결'로 버려졌다(체결 영구 미기록).
    """
    from localapp import intraday_loop

    t, broker = isolated_trader            # A — pending 비어있는 stale 인스턴스
    t2 = tr.Trader(broker)                 # B — 발주 인스턴스
    t2.pending["0000000001"] = _pending_entry("0000000001", "005930", "buy", 5)
    t2._save()

    evt = {"CNTG_YN": "2", "ODER_NO": "0000000001", "STCK_SHRN_ISCD": "005930",
           "CNTG_QTY": "5", "CNTG_UNPR": "1000", "STCK_CNTG_HOUR": "100001",
           "RFUS_YN": ""}
    intraday_loop._on_exec_event(t, broker, evt)

    ledger_disk = _read_json(tr.LEDGER_PATH, {})
    assert ledger_disk.get("s1", {}).get("qty") == 5, \
        "stale 인스턴스가 체결 통보를 유실(미매칭) 또는 미영속"
    pending_disk = _read_json(tr.PENDING_ORDERS_PATH, {})
    assert "0000000001" not in pending_disk


def test_submit_persists_pending_immediately(isolated_trader):
    """발주 직후 pending이 디스크에 영속 — cycle 끝 저장까지의 크래시 유실창 제거."""
    t, broker = isolated_trader
    from quant_core.exec_defaults import merged_execution
    t._submit_buy("s1", "전략", {}, "005930", 5, 1000.0,
                  merged_execution(None), decisions=[])
    on_disk = _read_json(tr.PENDING_ORDERS_PATH, {})
    assert len(on_disk) == 1, "발주 직후 pending 미영속(크래시 시 추적 유실)"
    entry = next(iter(on_disk.values()))
    assert entry["symbol"] == "005930"
    assert entry["is_resv"] is False
