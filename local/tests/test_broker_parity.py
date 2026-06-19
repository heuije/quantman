"""LS 브로커 엔진 패리티 회귀 (P1·P3·P4).

P1 — Broker Protocol의 account_snapshot 시그니처가 실제 구현과 일치
P3 — reconcile_submitting이 _daily_ccld 없는 브로커에서 AttributeError 없이 graceful skip
P4 — _should_start_kis_ws 헬퍼가 브로커 종류에 따라 올바른 값을 반환
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


# ── P4: _should_start_kis_ws 헬퍼 ─────────────────────────────────────────────

def test_p4_helper_returns_false_for_ls(monkeypatch):
    """LS 활성 시 _should_start_kis_ws()는 False."""
    from localapp import intraday_loop, secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: "ls")
    # intraday_loop도 같은 함수 참조를 사용하므로 모듈 속성도 패치
    monkeypatch.setattr(intraday_loop, "get_active_broker", lambda: "ls")
    assert intraday_loop._should_start_kis_ws() is False


def test_p4_helper_returns_true_for_kis(monkeypatch):
    """KIS 활성 시 _should_start_kis_ws()는 True."""
    from localapp import intraday_loop, secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: "kis")
    monkeypatch.setattr(intraday_loop, "get_active_broker", lambda: "kis")
    assert intraday_loop._should_start_kis_ws() is True


def test_p4_ws_start_not_called_for_ls(monkeypatch):
    """LS 활성 시 KisWebSocket.start()가 호출되지 않는다 — 소스 레벨 검증."""
    # intraday_loop의 ws.start() 호출 블록이 _should_start_kis_ws() 게이트로
    # 감싸져 있는지 소스 텍스트로 확인.
    loop_path = _LOCAL / "localapp" / "intraday_loop.py"
    source = loop_path.read_text(encoding="utf-8")

    # _should_start_kis_ws가 ws.start() 상위 레벨에서 사용되어야 함
    assert "_should_start_kis_ws()" in source, (
        "intraday_loop.py에 _should_start_kis_ws() 게이트가 없습니다"
    )

    # ws.start() 직전에 _should_start_kis_ws 게이트가 있어야 함
    ws_start_pos = source.find("ws.start()")
    assert ws_start_pos != -1, "ws.start() 호출을 찾을 수 없습니다"

    # ws.start()가 있는 블록 앞에 _should_start_kis_ws가 있어야 함
    guard_pos = source.rfind("_should_start_kis_ws()", 0, ws_start_pos)
    assert guard_pos != -1, (
        "ws.start() 이전에 _should_start_kis_ws() 게이트가 없습니다"
    )


def test_p4_order_ws_also_uses_helper(monkeypatch):
    """체결통보 WS 게이트도 _should_start_kis_ws()를 사용한다 — 두 게이트 일치."""
    loop_path = _LOCAL / "localapp" / "intraday_loop.py"
    source = loop_path.read_text(encoding="utf-8")

    # _should_start_kis_ws() 사용 횟수 — price-WS + order-WS = 최소 2회
    count = source.count("_should_start_kis_ws()")
    assert count >= 2, (
        f"_should_start_kis_ws() 사용이 {count}회입니다. "
        "price-WS와 order-WS 양쪽에서 사용해야 합니다(최소 2회)."
    )
