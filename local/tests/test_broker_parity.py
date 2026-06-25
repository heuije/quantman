"""LS 브로커 엔진 패리티 회귀 (P1·P3·P4).

P1 — Broker Protocol의 account_snapshot 시그니처가 실제 구현과 일치
P3 — reconcile_submitting이 _daily_ccld 없는 브로커에서 AttributeError 없이 graceful skip
P4 — make_quote_ws/make_order_ws 팩토리가 브로커별 WS(kis/ls)를 선택
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


# ── P1: Protocol account_snapshot 시그니처 ─────────────────────────────────────

def test_p1_kis_broker_account_snapshot_has_overseas_param():
    """KisBroker.account_snapshot이 overseas kwarg를 받는다."""
    from localapp.kis_broker import KisBroker
    sig = inspect.signature(KisBroker.account_snapshot)
    assert "overseas" in sig.parameters, (
        "KisBroker.account_snapshot에 'overseas' 파라미터가 없습니다"
    )


def test_p1_ls_broker_account_snapshot_has_overseas_param():
    """LsBroker.account_snapshot이 overseas kwarg를 받는다."""
    from localapp.ls_broker import LsBroker
    sig = inspect.signature(LsBroker.account_snapshot)
    assert "overseas" in sig.parameters, (
        "LsBroker.account_snapshot에 'overseas' 파라미터가 없습니다"
    )


def test_p1_protocol_account_snapshot_has_overseas_param():
    """Broker Protocol 선언도 overseas 파라미터를 명시한다."""
    from localapp.broker import Broker
    sig = inspect.signature(Broker.account_snapshot)
    assert "overseas" in sig.parameters, (
        "Broker Protocol의 account_snapshot에 'overseas' 파라미터가 없습니다"
    )


def test_p1_overseas_defaults_to_true_on_all():
    """overseas 파라미터의 기본값이 True (Protocol·KIS·LS 전부)."""
    from localapp.broker import Broker
    from localapp.kis_broker import KisBroker
    from localapp.ls_broker import LsBroker

    for cls in (Broker, KisBroker, LsBroker):
        sig = inspect.signature(cls.account_snapshot)
        param = sig.parameters.get("overseas")
        assert param is not None, f"{cls.__name__}.account_snapshot에 overseas 없음"
        assert param.default is True, (
            f"{cls.__name__}.account_snapshot(overseas) 기본값이 True가 아님: "
            f"{param.default!r}"
        )


# ── P3: reconcile_submitting — _daily_ccld 없는 브로커에서 graceful skip ──────

def test_p3_ls_broker_no_daily_ccld_no_raise(tmp_path):
    """_daily_ccld 없는 브로커로 reconcile_submitting을 호출해도 AttributeError 없이 반환."""
    from localapp import intents

    # submitting intent 하나 준비
    jpath = tmp_path / "intents.jsonl"
    intents.begin("2026-06-19", "iid-ls-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)

    class FakeLs:
        """_daily_ccld 없음 — LS 브로커 시뮬레이션."""

    broker = FakeLs()
    assert not hasattr(broker, "_daily_ccld")

    # AttributeError 없이 반환해야 함
    result = intents.reconcile_submitting(broker, "2026-06-19", path=jpath)

    # KR intent는 kr_rows=None → kis_query_failed로 집계
    assert isinstance(result, dict), "reconcile_submitting이 dict를 반환해야 합니다"
    assert result["kis_query_failed"] == 1, (
        f"_daily_ccld 미지원 브로커의 KR intent는 kis_query_failed여야 합니다: {result}"
    )


def test_p3_ls_no_daily_ccld_is_conservative_gate(tmp_path):
    """_daily_ccld 없는 브로커 → submitting 게이트 유지(보수적) — 이중 발주 차단."""
    from localapp import intents

    jpath = tmp_path / "intents.jsonl"
    intents.begin("2026-06-19", "iid-ls-2", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)

    class FakeLs:
        pass

    intents.reconcile_submitting(FakeLs(), "2026-06-19", path=jpath)

    # submitting 상태 유지 → is_active True (게이트 잠김)
    assert intents.is_active("2026-06-19", 42, "005930", "buy", path=jpath) is True, (
        "reconcile skip 후 submitting 게이트가 유지(보수적)돼야 합니다"
    )


def test_p3_kis_path_unchanged(tmp_path):
    """_daily_ccld 있는 브로커(KIS 모킹)는 기존 경로 그대로 — KIS 동작 불변."""
    from unittest.mock import MagicMock
    from localapp import intents

    jpath = tmp_path / "intents.jsonl"
    intents.begin("2026-06-19", "iid-kis-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)

    broker = MagicMock()
    broker._daily_ccld.return_value = {"output1": [
        {"pdno": "005930", "sll_buy_dvsn_cd": "02",
         "ord_qty": 10, "ord_unpr": 70000, "odno": "ORD-K1",
         "cncl_yn": "N"}
    ]}

    result = intents.reconcile_submitting(broker, "2026-06-19", path=jpath)

    # KIS 경로: 매칭 1건 → matched=1 (기존 동작 불변)
    assert result["matched"] == 1, f"KIS 경로 matched 불일치: {result}"
    broker._daily_ccld.assert_called_once()


# ── P4: 브로커별 WS 팩토리 (make_quote_ws / make_order_ws) ──────────────────
# 구 _should_start_kis_ws 게이트가 팩토리로 일반화됨 — KIS는 기존 KisWebSocket/
# KisOrderWebSocket을 그대로 받고(byte-identical), LS도 전용 WS를 start한다.


class _FakeWsBroker:
    """WS 팩토리 구성용 최소 브로커 — 네트워크 호출 없음."""

    virtual = True

    def _token(self):
        return "TOK"


def _patch_active_broker(monkeypatch, kind):
    from localapp import intraday_loop, secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: kind)
    # intraday_loop도 같은 함수 참조를 사용하므로 모듈 속성도 패치
    monkeypatch.setattr(intraday_loop, "get_active_broker", lambda: kind)


def test_p4_quote_ws_ls_is_ls_websocket(monkeypatch):
    """LS 활성 → make_quote_ws가 LsWebSocket을 만든다(이전엔 시세 WS skip)."""
    from localapp import intraday_loop
    from localapp.ls_websocket import LsWebSocket
    _patch_active_broker(monkeypatch, "ls")
    ws = intraday_loop.make_quote_ws(_FakeWsBroker(), lambda s, p: None)
    assert isinstance(ws, LsWebSocket)


def test_p4_quote_ws_kis_is_kis_websocket(monkeypatch):
    """KIS 활성 → make_quote_ws가 KisWebSocket을 만든다(기존 경로 불변)."""
    from localapp import intraday_loop
    from localapp.kis_websocket import KisWebSocket
    _patch_active_broker(monkeypatch, "kis")
    ws = intraday_loop.make_quote_ws(_FakeWsBroker(), lambda s, p: None)
    assert isinstance(ws, KisWebSocket)


def test_p4_order_ws_ls_normalizes_krx_market(monkeypatch):
    """LS 활성 → make_order_ws가 LsOrderWebSocket·루프 'KRX'를 'KR'로 정규화.

    회귀 방지: 정규화 없으면 LsOrderWebSocket(market='KRX')가 ValueError를 던져
    loop-start가 크래시한다(2026-06-25 라이브 실측). 실제 호출부 market은 'KRX'.
    """
    from localapp import intraday_loop
    from localapp.ls_order_websocket import LsOrderWebSocket
    _patch_active_broker(monkeypatch, "ls")
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "KRX")
    assert isinstance(ws, LsOrderWebSocket)
    assert ws._market == "KR"   # 'KRX' → 'KR' 정규화


def test_p4_order_ws_ls_us_market_kept(monkeypatch):
    """LS US 시장은 'US' 유지."""
    from localapp import intraday_loop
    _patch_active_broker(monkeypatch, "ls")
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "US")
    assert ws._market == "US"


def test_p4_order_ws_kis_with_hts_is_kis_order_websocket(monkeypatch):
    """KIS 활성 + hts_id → KisOrderWebSocket(기존 경로 불변·market 그대로 전달)."""
    from localapp import intraday_loop
    from localapp.kis_order_websocket import KisOrderWebSocket
    _patch_active_broker(monkeypatch, "kis")
    monkeypatch.setattr(intraday_loop, "load_kis", lambda: {"hts_id": "myhts"})
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "KR")
    assert isinstance(ws, KisOrderWebSocket)


def test_p4_order_ws_kis_no_hts_returns_none(monkeypatch):
    """KIS 활성 + hts_id 없음 → None(REST 폴링이 체결 인지 fallback) — 기존 동작 불변."""
    from localapp import intraday_loop
    _patch_active_broker(monkeypatch, "kis")
    monkeypatch.setattr(intraday_loop, "load_kis", lambda: {})
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "KRX")
    assert ws is None
