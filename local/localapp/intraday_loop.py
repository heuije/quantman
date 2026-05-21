"""장중 stop loss 루프 오케스트레이션.

평일 09:15 ~ 15:30 사이 KIS WebSocket 시세를 받아 보유 종목에 대해 익절/손절/
트레일링 자동 매도. 메인 사이클(08:55) 종료 후 시작, 정규장 마감(15:30) 시 종료.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date
from pathlib import Path

import quant_core as qc

from .broker import Broker
from .config import PENDING_PATH
from .intraday_stop import IntradayStopManager
from .kis_order_websocket import KisOrderWebSocket
from .kis_websocket import KisWebSocket
from .runner import make_broker
from .secrets_store import load_kis
from .sync_client import push_snapshot
from .trader import Trader

log = logging.getLogger("localapp.intraday_loop")

# 모듈 전역 상태 — scheduler가 start/stop 호출
_state = {
    "running": False,
    "ws": None,
    "order_ws": None,         # Phase 33 — 체결 통보 WebSocket
    "manager": None,
    "trader": None,
    "broker": None,
    "stop_flag": None,
    "sync_thread": None,
}
_lock = threading.Lock()


def _on_exec_event(trader: Trader, broker: Broker, evt: dict) -> None:
    """KIS 체결 통보 수신 시 호출 — ledger 즉시 갱신 + 서버 push.

    KIS H0STCNI0 evt 필드 (주요):
      CNTG_YN: '2'=체결, '1'=접수(주문·정정·취소·거부)
      ODER_NO: KIS 주문번호
      STCK_SHRN_ISCD: 종목코드
      CNTG_QTY: 체결 수량
      CNTG_UNPR: 체결 가격
      SELN_BYOV_CLS: '01'=매도, '02'=매수 (또는 '1'/'2' — KIS spec 따라)
      RFUS_YN: 'Y'=거부
    """
    cntg_yn = evt.get("CNTG_YN", "")
    order_no = evt.get("ODER_NO", "").strip()
    symbol = evt.get("STCK_SHRN_ISCD", "")
    rfus_yn = evt.get("RFUS_YN", "")

    if rfus_yn == "Y":
        log.warning("[order-ws] 주문 거부: ODER_NO=%s symbol=%s", order_no, symbol)
        return

    if cntg_yn != "2":
        # 접수 통보(1) — 체결 아님. pending에 ODER_NO만 안정적으로 기록 가능
        log.info("[order-ws] 접수 통보: ODER_NO=%s symbol=%s", order_no, symbol)
        return

    # 체결 통보 → pending에서 매칭 → _apply_fill
    pending = trader.pending
    if order_no not in pending:
        log.info("[order-ws] 미매칭 체결: ODER_NO=%s (pending에 없음)", order_no)
        return

    try:
        filled_qty = int(evt.get("CNTG_QTY", "0") or 0)
        fill_price = float(evt.get("CNTG_UNPR", "0") or 0)
    except ValueError:
        log.warning("[order-ws] 잘못된 수량/가격: qty=%s price=%s",
                     evt.get("CNTG_QTY"), evt.get("CNTG_UNPR"))
        return

    if filled_qty <= 0 or fill_price <= 0:
        return

    p = pending[order_no]
    already = int(p.get("filled_so_far", 0) or 0)
    decisions: list[dict] = []

    if filled_qty + already >= int(p.get("qty", 0)):
        # 전량 체결
        trader._apply_fill(order_no, p, filled_qty, fill_price, decisions,
                            partial=False)
        del pending[order_no]
    else:
        # 부분 체결
        trader._apply_fill(order_no, p, filled_qty, fill_price, decisions,
                            partial=True)
        p["filled_so_far"] = already + filled_qty

    # 즉시 서버 push
    try:
        snap = broker.account_snapshot()
        push_snapshot({
            "balance": snap.get("balance", {}),
            "positions": snap.get("positions", []),
            "decisions": decisions,
            "cycle_summary": {
                "today": date.today().isoformat(),
                "kind": "exec_notice",
            },
        })
        log.info("[order-ws] 체결 반영+push: %s %s주 @ %s원",
                  symbol, filled_qty, fill_price)
    except Exception as e:
        log.warning("[order-ws] push 실패: %s", e)


def _get_strat_def_lookup(strategies: list[dict]):
    """strategy_id → strat_def dict 매핑."""
    by_id = {str(s.get("id", "")): s.get("definition", {}) for s in strategies}
    return lambda sid: by_id.get(str(sid))


def _push_after_sell(broker: Broker, decisions: list[dict]) -> None:
    """매도 발주 직후 즉시 서버에 push — 사용자 화면 빠른 반영."""
    if not decisions:
        return
    try:
        snap = broker.account_snapshot()
        payload = {
            "balance": snap.get("balance", {}),
            "positions": snap.get("positions", []),
            "decisions": decisions,
            "cycle_summary": {
                "today": date.today().isoformat(),
                "kind": "intraday_stop_trigger",
            },
        }
        push_snapshot(payload)
    except Exception as e:
        log.warning("intraday stop push 실패: %s", e)


def start() -> dict:
    """장중 stop loss 루프 시작. 별 thread에서 WebSocket 유지.

    cycle 종료(09:10) 후 호출. 이미 실행 중이면 무동작.
    Phase 38.3: use_mock 제거 — KisBroker 전용.
    """
    from .sync_client import pull_strategies
    with _lock:
        if _state["running"]:
            log.info("intraday_loop 이미 실행 중")
            return {"status": "already_running"}

        log.info("intraday stop loop 시작")
        dataset = qc.load_dataset(with_indicators=True)
        broker = make_broker()
        trader = Trader(broker)

        try:
            strategies = pull_strategies()
        except Exception as e:
            log.warning("strategies pull 실패 (보유 종목만 추적): %s", e)
            strategies = []

        manager = IntradayStopManager(
            broker=broker,
            get_ledger=lambda: trader.ledger,
            get_strat_def=_get_strat_def_lookup(strategies),
            submit_sell_fn=trader._submit_sell,
            dataset=dataset,
        )
        manager.reset_daily()

        ws = KisWebSocket(broker, on_tick=manager.on_tick)
        try:
            ws.start()
        except Exception as e:
            log.error("WebSocket 시작 실패: %s", e)
            return {"status": "ws_start_failed", "error": str(e)}

        # 초기 구독: 현재 보유 종목
        held = manager.held_symbols()
        if held:
            ws.subscribe(list(held))
            log.info("초기 구독: %s (%d종목)", held, len(held))

        # Phase 33 — 체결 통보 WebSocket (HTS ID 설정된 경우만)
        order_ws = None
        kis_creds = load_kis() or {}
        hts_id = kis_creds.get("hts_id", "")
        if hts_id:
            try:
                order_ws = KisOrderWebSocket(
                    broker, hts_id,
                    on_exec=lambda evt: _on_exec_event(trader, broker, evt))
                order_ws.start()
                log.info("체결 통보 WebSocket 시작 (HTS ID=%s)", hts_id)
            except Exception as e:
                log.warning("체결 통보 WebSocket 시작 실패: %s", e)
                order_ws = None
        else:
            log.info("HTS ID 미설정 — 체결 통보 WebSocket skip "
                      "(setup으로 hts_id 등록 시 활성)")

        # 매도 발주 시 push hook
        original_submit = trader._submit_sell

        def _hook_submit(*args, **kwargs):
            original_submit(*args, **kwargs)
            _push_after_sell(broker, manager.decisions[-1:])

        trader._submit_sell = _hook_submit
        manager._submit_sell = _hook_submit

        # 보유 종목 변화 추적 — 60초마다 sync_subscriptions
        stop_flag = threading.Event()

        def _sync_loop():
            while not stop_flag.wait(60):
                try:
                    target = manager.held_symbols()
                    result = ws.sync_subscriptions(target)
                    if result["added"] or result["removed"]:
                        log.info("구독 sync: %s", result)
                except Exception as e:
                    log.warning("구독 sync 실패: %s", e)

        sync_thread = threading.Thread(target=_sync_loop, daemon=True,
                                          name="intraday-sync")
        sync_thread.start()

        _state.update({
            "running": True, "ws": ws, "order_ws": order_ws,
            "manager": manager,
            "trader": trader, "broker": broker,
            "stop_flag": stop_flag, "sync_thread": sync_thread,
        })
        return {"status": "started", "initial_subscribed": len(held),
                "order_ws_connected": order_ws.is_connected if order_ws else False}


def stop() -> dict:
    """장 마감(15:30) 직후 호출. WebSocket 종료, 마지막 push."""
    with _lock:
        if not _state["running"]:
            return {"status": "not_running"}

        log.info("intraday stop loop 종료")
        sf = _state["stop_flag"]
        if sf:
            sf.set()
        ws = _state["ws"]
        if ws:
            ws.stop()
        order_ws = _state["order_ws"]
        if order_ws:
            order_ws.stop()
        thr = _state["sync_thread"]
        if thr:
            thr.join(timeout=5)

        manager = _state["manager"]
        broker = _state["broker"]
        n_triggered = len(manager.decisions) if manager else 0

        # 마지막 sync push
        if broker and manager and n_triggered > 0:
            _push_after_sell(broker, manager.decisions)

        _state.update({
            "running": False, "ws": None, "manager": None,
            "trader": None, "broker": None,
            "stop_flag": None, "sync_thread": None,
        })
        return {"status": "stopped", "n_triggered": n_triggered}


def status() -> dict:
    with _lock:
        if not _state["running"]:
            return {"running": False}
        order_ws = _state["order_ws"]
        return {
            "running": True,
            "subscribed": len(_state["ws"]._symbols) if _state["ws"] else 0,
            "ws_connected": _state["ws"].is_connected if _state["ws"] else False,
            "order_ws_enabled": order_ws is not None,
            "order_ws_connected": order_ws.is_connected if order_ws else False,
            "n_triggered_today":
                len(_state["manager"].decisions) if _state["manager"] else 0,
        }
