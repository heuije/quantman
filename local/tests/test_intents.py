"""L-01 회귀 — 주문 멱등성 (intent journal + 2-phase + reconcile).

해결하는 문제(구체적):
  trader._submit_buy는 broker.buy_limit 호출(자금 영향) 후 self.pending 메모리만
  갱신하고 cycle 끝의 _save()에서 디스크에 쓴다. 그 사이 ~수초의 race window에
  크래시 시 KIS에는 주문이 있는데 디스크엔 흔적이 없어 재기동 시 cycle이 같은
  후보를 보고 또 매수 → 2배 포지션, 진짜 자금 손실.

검증 시나리오(아래 테스트가 1:1로 대응):
  - begin/submitted 경로 → is_active True (정상 발주 후 같은 사이클 재시도 차단)
  - begin/failed 경로 → is_active False (발주 실패 시 정상 재시도 허용)
  - begin만 (마감 안 됨) → is_active True (보수적 차단)
  - 다른 종목/전략은 게이트 영향 없음
  - reconcile: KIS 매칭 1건 → submitted 마감, 게이트 유지
  - reconcile: KIS 매칭 0건 → failed 마감, 게이트 풀림(재시도 가능)
  - reconcile: KIS 매칭 다수 → 보수적 submitted (이중 발주 절대 차단)
  - reconcile: KIS 조회 실패 → submitting 유지 (게이트 유지)
  - reconcile: 취소된 주문은 매칭 제외
  - **end-to-end 크래시 시뮬레이션**: _submit_buy 중 broker.buy_limit raise →
    재기동에서 reconcile 후 같은 후보 매수 시 (a) KIS에 흔적 있으면 차단,
    (b) 흔적 없으면 정상 재시도. 이게 L-01의 핵심 보장.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import intents, state_store


@pytest.fixture
def jpath(tmp_path):
    """테스트마다 별도 intents.jsonl 경로(격리)."""
    return tmp_path / "intents.jsonl"


# ── basic append + is_active ──────────────────────────────────────────────────

def test_begin_submitted_gate_active(jpath):
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    intents.mark_submitted("2026-05-23", "iid-1", "ORD-1", path=jpath)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


def test_begin_failed_gate_inactive(jpath):
    """failed로 마감되면 게이트 풀림 → 정상 재시도 허용."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    intents.mark_failed("2026-05-23", "iid-1", "buy_limit timeout", path=jpath)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is False


def test_begin_only_gate_active(jpath):
    """submitting 후 마감되지 않은 상태(=크래시 시점) → 보수적으로 차단 유지."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


def test_gate_isolated_by_sid_symbol_side(jpath):
    """다른 (sid, sym, side)에는 영향 없음."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True
    assert intents.is_active("2026-05-23", 42, "005930", "sell",
                              path=jpath) is False
    assert intents.is_active("2026-05-23", 42, "000660", "buy",
                              path=jpath) is False
    assert intents.is_active("2026-05-23", 43, "005930", "buy",
                              path=jpath) is False


def test_yesterdays_intent_does_not_gate_today(jpath):
    intents.begin("2026-05-22", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    intents.mark_submitted("2026-05-22", "iid-1", "ORD-1", path=jpath)
    # 오늘 같은 (sid,sym,side)는 게이트 안 걸림
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is False


# ── reconcile ────────────────────────────────────────────────────────────────

def _kr_row(symbol="005930", side_cd="02", qty=10, px=70000, odno="ORD-X",
            cncl=False):
    return {"pdno": symbol, "sll_buy_dvsn_cd": side_cd,
            "ord_qty": qty, "ord_unpr": px, "odno": odno,
            "cncl_yn": "Y" if cncl else "N"}


def _mock_broker(daily_ccld_rows=None, overseas_rows=None, raise_kr=False,
                  raise_us=False):
    b = MagicMock()
    if raise_kr:
        b._daily_ccld.side_effect = RuntimeError("KIS down")
    else:
        b._daily_ccld.return_value = {"output1": daily_ccld_rows or []}
    if raise_us:
        b._overseas_ccnl_today.side_effect = RuntimeError("KIS US down")
    else:
        b._overseas_ccnl_today.return_value = overseas_rows or []
    return b


def test_reconcile_match_one_marks_submitted(jpath):
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(daily_ccld_rows=[
        _kr_row(symbol="005930", qty=10, px=70000, odno="ORD-1")])
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["matched"] == 1 and res["no_fill"] == 0
    # 매칭 후 게이트 유지 (submitted)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


def test_reconcile_no_match_marks_failed(jpath):
    """KIS에 흔적 없으면 failed로 마감 → 게이트 풀림 → 재시도 가능."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(daily_ccld_rows=[])  # KIS 응답 비어있음
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["no_fill"] == 1 and res["matched"] == 0
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is False


def test_reconcile_ambiguous_conservative_submitted(jpath):
    """매칭 다수 → 보수적으로 submitted (이중 발주 절대 차단)."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(daily_ccld_rows=[
        _kr_row(odno="ORD-A"), _kr_row(odno="ORD-B")])
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["ambiguous"] == 1
    # 게이트는 유지(submitted)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


def test_reconcile_kis_query_failed_keeps_submitting(jpath):
    """KIS 조회 실패 → submitting 유지(게이트 유지). 다음 사이클에서 재시도."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(raise_kr=True)
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["kis_query_failed"] == 1
    # submitting 상태 그대로 → 게이트 유지(보수적)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


def test_reconcile_cancelled_order_not_matched(jpath):
    """취소된 주문은 매칭 제외 — 재발주 허용 측."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(daily_ccld_rows=[_kr_row(cncl=True)])
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["no_fill"] == 1
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is False


def test_reconcile_wrong_side_not_matched(jpath):
    """매도 측 KIS 행이 매수 intent와 매칭되지 않음."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(daily_ccld_rows=[
        _kr_row(side_cd="01")])  # 매도 행
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["no_fill"] == 1


def test_reconcile_price_outside_tolerance_not_matched(jpath):
    """ref_price와 ord_unpr가 5% 이상 다르면 매칭 안 함."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    broker = _mock_broker(daily_ccld_rows=[
        _kr_row(px=100000)])  # 70000 vs 100000 → 차이 ~43%
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["no_fill"] == 1


def test_reconcile_no_submitting_returns_empty(jpath):
    """submitting 없으면 reconcile은 무동작."""
    broker = _mock_broker()
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res == {"matched": 0, "no_fill": 0, "ambiguous": 0,
                   "kis_query_failed": 0, "details": []}


def test_reconcile_us_symbol_uses_overseas_api(jpath):
    """US 종목은 _overseas_ccnl_today로 조회."""
    intents.begin("2026-05-23", "iid-1", 42, "T", "AAPL", "buy", 5, 200.0,
                  path=jpath)
    broker = _mock_broker(overseas_rows=[
        {"pdno": "AAPL", "sll_buy_dvsn_cd": "02",
         "ft_ord_qty": 5, "ft_ord_unpr3": 200.0, "odno": "US-ORD-1",
         "prcs_stat_name": ""}])
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["matched"] == 1
    broker._overseas_ccnl_today.assert_called_with("AAPL")


# ── end-to-end 크래시 시뮬레이션 ─────────────────────────────────────────────

def test_e2e_crash_in_buy_limit_marks_failed_via_reconcile(jpath, monkeypatch):
    """가장 중요한 검증: 발주 도중 크래시 → 재기동 시 KIS에 흔적 없으면 재시도 허용.

    시나리오:
      1) trader가 intent.begin 호출
      2) broker.buy_limit가 raise (네트워크 끊김 시뮬레이션)
      3) → intent.mark_failed 자동 기록 (trader가 except에서 호출)
      4) (재기동 가정) reconcile 호출 — KIS daily ccld는 빈 응답
      5) → is_active False → 같은 후보 재시도 가능
    """
    # _submit_buy를 직접 호출하기 어려워, intents 모듈 자체 흐름으로 시뮬레이션
    iid = intents.new_intent_id()
    intents.begin("2026-05-23", iid, 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    # broker.buy_limit가 raise한 케이스를 trader가 처리한 결과:
    intents.mark_failed("2026-05-23", iid, "buy_limit: timeout", path=jpath)

    # 재기동 후 reconcile (KIS 비어 있음)
    broker = _mock_broker(daily_ccld_rows=[])
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    # failed로 이미 마감된 intent는 reconcile 대상이 아님 (submitting만 처리)
    assert res["matched"] == 0 and res["no_fill"] == 0
    # 게이트 풀려 있음 — 재시도 가능
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is False


def test_e2e_crash_between_phases_recovered_by_reconcile(jpath):
    """더 어려운 시나리오: KIS는 주문 받았는데 우리 응답 받기 전에 크래시.

      1) intent.begin 기록 (submitting)
      2) broker.buy_limit 호출 — KIS는 받음
      3) ★ 응답 받기 전 우리 프로세스 강제 종료 (intent.mark_submitted 호출 못함)
      4) 재기동 → reconcile이 KIS daily ccld 조회 → 매칭됨
      5) → submitted 마감, 게이트 유지 → 재발주 차단 (이중 발주 방지!)
    """
    intents.begin("2026-05-23", "iid-X", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    # mark_submitted/failed 호출 없음 — 크래시 시뮬레이션

    # 재기동 후 reconcile: KIS는 우리가 보낸 주문을 알고 있음
    broker = _mock_broker(daily_ccld_rows=[
        _kr_row(symbol="005930", qty=10, px=70000, odno="ORD-KIS-RECEIVED")])
    res = intents.reconcile_submitting(broker, "2026-05-23", path=jpath)
    assert res["matched"] == 1
    # 게이트 유지 — 이중 발주 차단됨
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


def test_append_is_fsynced(jpath, monkeypatch):
    """디스크 도달 보장 — os.fsync 호출됨.

    R5 이후 fsync는 state_store.append_jsonl(fsync=True) 안에서 일어난다.
    intents.begin → state_store가 fsync하는 위임 경로를 검증한다(L-01 불변).
    """
    fsync_called = []
    real_fsync = state_store.os.fsync

    def _wrapped(fd):
        fsync_called.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(state_store.os, "fsync", _wrapped)
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    assert len(fsync_called) == 1


def test_corrupted_line_skipped(jpath):
    """파일에 깨진 라인이 섞여도 정상 라인은 읽힘 (graceful)."""
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text("not json\n", encoding="utf-8")
    intents.begin("2026-05-23", "iid-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)
    assert intents.is_active("2026-05-23", 42, "005930", "buy",
                              path=jpath) is True


# ── Trader 통합: 실제 _submit_buy 흐름 검증 ───────────────────────────────────

def test_trader_submit_buy_writes_intent_on_success(jpath, monkeypatch):
    """Trader._submit_buy 정상 흐름 → submitting + submitted 둘 다 기록."""
    monkeypatch.setattr(intents, "INTENTS_PATH", jpath)
    from localapp.trader import Trader
    broker = MagicMock()
    broker.quote_band.return_value = {"price": 70000.0, "upper": None, "lower": None}  # 가격 평면 정책
    broker.buy_limit.return_value = {"success": True, "order_no": "ORD-OK"}
    # _load_json 호출은 파일이 없으면 default — 그대로 진행
    trader_dirs = [intents.INTENTS_PATH.parent]
    monkeypatch.setattr("localapp.trader.LEDGER_PATH", trader_dirs[0] / "ledger.json")
    monkeypatch.setattr("localapp.trader.EQUITY_PATH", trader_dirs[0] / "equity.json")
    monkeypatch.setattr("localapp.trader.PENDING_ORDERS_PATH",
                        trader_dirs[0] / "pending.json")
    monkeypatch.setattr("localapp.trader.TRADES_PATH",
                        trader_dirs[0] / "trades.jsonl")

    t = Trader(broker)
    policy = {"use_limit": True, "buy_tolerance_pct": 0.5,
              "sell_tolerance_pct": 0.5, "unfilled_timeout_sec": 300}
    decisions: list[dict] = []
    t._submit_buy("sid-1", "T", {"name": "T"}, "005930", 10, 70000,
                   policy, decisions)

    today = trader_dirs[0]   # not used; just structural
    # intents.jsonl 내용 확인
    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # submitting + submitted
    import json as _j
    a, b = _j.loads(lines[0]), _j.loads(lines[1])
    assert a["phase"] == "submitting" and a["symbol"] == "005930"
    assert b["phase"] == "submitted" and b["order_no"] == "ORD-OK"


def test_trader_submit_buy_marks_failed_when_broker_raises(jpath, monkeypatch):
    """**가장 중요한 회귀**: broker.buy_limit가 raise하면 intent는 failed로 마감.

    이 흐름이 깨지면(예: intent.begin 없이 buy_limit 호출, 또는 except에서 mark_failed
    누락) 크래시 시뮬레이션 회귀가 발생해 L-01의 보장이 무효화된다.
    """
    monkeypatch.setattr(intents, "INTENTS_PATH", jpath)
    from localapp.trader import Trader
    broker = MagicMock()
    broker.quote_band.return_value = {"price": 70000.0, "upper": None, "lower": None}  # 가격 평면 정책
    broker.buy_limit.side_effect = RuntimeError("KIS timeout")
    monkeypatch.setattr("localapp.trader.LEDGER_PATH", jpath.parent / "ledger.json")
    monkeypatch.setattr("localapp.trader.EQUITY_PATH", jpath.parent / "equity.json")
    monkeypatch.setattr("localapp.trader.PENDING_ORDERS_PATH",
                        jpath.parent / "pending.json")
    monkeypatch.setattr("localapp.trader.TRADES_PATH",
                        jpath.parent / "trades.jsonl")

    t = Trader(broker)
    policy = {"use_limit": True, "buy_tolerance_pct": 0.5,
              "sell_tolerance_pct": 0.5, "unfilled_timeout_sec": 300}
    t._submit_buy("sid-1", "T", {"name": "T"}, "005930", 10, 70000,
                   policy, [])

    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # submitting + failed
    import json as _j
    a, b = _j.loads(lines[0]), _j.loads(lines[1])
    assert a["phase"] == "submitting"
    assert b["phase"] == "failed" and "KIS timeout" in b["error"]
    # 게이트는 풀려야 함 (failed) — 정상 재시도 허용
    today = a["date"]
    assert intents.is_active(today, "sid-1", "005930", "buy",
                              path=jpath) is False
