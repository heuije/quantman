"""장중 stop loss 루프 오케스트레이션.

평일 09:15 ~ 15:30 사이 KIS WebSocket 시세를 받아 보유 종목에 대해 익절/손절/
트레일링 자동 매도. 메인 사이클(08:55) 종료 후 시작, 정규장 마감(15:30) 시 종료.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .trader import kst_today  # L-06: PC tz와 무관한 KST 거래일

import quant_core as qc

from .broker import Broker
from .config import PENDING_PATH
from .intraday_stop import IntradayStopManager
from .kis_order_websocket import KisOrderWebSocket
from .kis_websocket import KisWebSocket
from .runner import make_broker
from .secrets_store import load_kis
from .sync_client import pull_risk_limits, push_snapshot
from .trader import Trader

log = logging.getLogger("localapp.intraday_loop")

# 모듈 전역 상태 — scheduler가 start/stop 호출
_state = {
    "running": False,
    "market": "KRX",          # 이번 loop이 다루는 시장 그룹 (KRX/US)
    "ws": None,
    "order_ws": None,         # Phase 33 — 체결 통보 WebSocket
    "manager": None,
    "trader": None,
    "broker": None,
    "stop_flag": None,
    "sync_thread": None,
    "polling_thread": None,   # Q3 — REST 폴링 fallback thread
    # 해외 실시간 시세 entitlement 감지 (US loop 전용)
    "started_at": 0.0,
    "us_subscribed": 0,       # 시작 시 구독한 미국 보유종목 수
    "last_overseas_tick": 0.0,
}
_lock = threading.Lock()

# US loop 시작 후 이 시간(초) 안에 해외 tick이 한 건도 없고 미국 보유가 있으면
# 해외 실시간 시세 미신청으로 판정 → 사용자 고지 (실시간 손절 미제공).
_US_REALTIME_GRACE_SEC = 120


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

    # L-09 — 체결 통보 중복 dedup. KIS가 같은 H0STCNI0 이벤트를 두 번
    # 보내면 ledger qty가 이중 가산되어 over-position이 된다. KIS spec엔
    # 별도 시퀀스/누계 필드가 없으므로 (체결시각, 수량, 가격) 3-tuple로
    # dedup. 정상 부분 체결은 시각이 달라 정상 누적되고, 진짜 중복은 같은
    # 시각·가격·수량이라 차단된다. pending[order_no]에 직접 보관하므로
    # pending이 disk에 영속될 때 같이 저장 → 재기동 직후 중복 도착도 차단.
    # 전량 체결 시 del pending[order_no]로 자동 회수.
    dedup_key = [evt.get("STCK_CNTG_HOUR", ""), filled_qty, fill_price]
    seen_keys = p.setdefault("_dedup_keys", [])
    if dedup_key in seen_keys:
        log.info("[order-ws] 중복 체결 통보 dedup: ODER_NO=%s key=%s",
                  order_no, dedup_key)
        return
    seen_keys.append(dedup_key)

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
                "today": kst_today().isoformat(),
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
                "today": kst_today().isoformat(),
                "kind": "intraday_stop_trigger",
            },
        }
        push_snapshot(payload)
    except Exception as e:
        log.warning("intraday stop push 실패: %s", e)


# 미국 실시간 시세 미신청 경고를 세션당 1회만 push하기 위한 플래그
_us_realtime_warned = False


# Q3: REST 폴링 fallback ─────────────────────────────────────────────────────
#
# WebSocket 정상 시 폴링 thread는 5초마다 ws.is_connected를 체크하고 skip.
# 끊김 감지 시 보유 종목 현재가를 broker.price()로 주기 조회. 받은 가격을
# manager.on_tick으로 전달하면 기존 stop loss 평가가 그대로 동작.
#
# 폴링 주기는 보유 종목 수에 따라 동적(KIS API 초당 ~20회 한도 보호).
# 시세 호출 외 잔고·체결·ks monitor도 같은 한도를 공유하므로 시세 예산은
# 초당 ~5회 안전 마진으로 잡는다.

_POLLING_HEALTH_CHECK_SEC = 5.0   # WebSocket 정상일 때 ws.is_connected 폴링 주기


def _polling_round_interval(n_held: int) -> float:
    """보유 종목 수별 1라운드 소요 목표 시간(초). 종목당 호출 간격 = round / n.

    | 종목수 | 라운드 | 종목당 | 초당 호출 |
    |--------|--------|--------|-----------|
    | ≤5     | 3s     | ≥0.6s  | ~1.67     |
    | 6~15   | 5s     | ≥0.33s | ~3.0      |
    | 16~30  | 10s    | ≥0.33s | ~3.0      |
    | 31~50  | 15s    | ≥0.3s  | ~3.3      |
    | >50    | 20s    | <0.4s  | <2.5      |
    """
    if n_held <= 5:
        return 3.0
    if n_held <= 15:
        return 5.0
    if n_held <= 30:
        return 10.0
    if n_held <= 50:
        return 15.0
    return 20.0


def _rest_polling_loop(ws, broker, manager, in_market_fn,
                        stop_flag: threading.Event, market: str) -> None:
    """REST 폴링 fallback thread 본체.

    상태 머신:
      1. ws.is_connected=True → _POLLING_HEALTH_CHECK_SEC(5초) 대기 후 재확인
      2. ws.is_connected=False → 보유 종목 전체에 broker.price() 1라운드
         라운드 중 매 종목 후 ws.is_connected 재확인(복구 즉시 중단)
         라운드 끝나면 다음 라운드 시작(rate limit 보호 위해 종목당 sleep)

    stop_flag.set()으로 종료 (intraday_loop.stop이 호출).
    장 마감까지 영구 실행 — WebSocket 복구 안 되면 그동안 계속 폴링.

    예외 처리: broker.price 실패는 log만, 다음 종목 진행. KIS 토큰 만료 등
    체계적 실패도 다음 iteration에서 다시 시도.
    """
    log.info("[%s] REST 폴링 fallback thread 시작", market)
    try:
        while not stop_flag.is_set():
            # WebSocket 정상이면 폴링 skip + health check 주기로 대기
            if ws is not None and ws.is_connected:
                if stop_flag.wait(_POLLING_HEALTH_CHECK_SEC):
                    break
                continue

            # WebSocket 끊김 → 1라운드 폴링
            held = [s for s in manager.held_symbols() if in_market_fn(s)]
            if not held:
                # 보유 없음 — health check 주기로 대기
                if stop_flag.wait(_POLLING_HEALTH_CHECK_SEC):
                    break
                continue

            round_sec = _polling_round_interval(len(held))
            per_symbol_sleep = round_sec / len(held)

            for sym in held:
                if stop_flag.is_set():
                    break
                # WebSocket 복구 감지 시 즉시 라운드 중단
                if ws is not None and ws.is_connected:
                    log.info("[%s] WebSocket 복구 감지 — 폴링 라운드 중단", market)
                    break
                try:
                    price = broker.price(sym)
                    if price > 0:
                        manager.on_tick(sym, price)
                except Exception as e:
                    log.debug("[polling-fallback] %s price 실패: %s", sym, e)
                if stop_flag.wait(per_symbol_sleep):
                    return
    except Exception as e:
        # 본 thread는 daemon이라 예외로 종료해도 프로세스에 영향 없지만,
        # 그 시점부터 fallback이 사라지므로 명시적으로 로그.
        log.exception("[%s] REST 폴링 thread 종료 (예외): %s", market, e)
    log.info("[%s] REST 폴링 fallback thread 종료", market)


def _check_us_realtime(broker: Broker, manager) -> None:
    """미국 보유분이 있는데 grace 내내 해외 tick이 0이면 실시간 시세 미신청으로
    판정 → 사용자에게 '실시간 손절 미제공' 1회 고지(서버 push). 세션당 1회.
    """
    global _us_realtime_warned
    if _us_realtime_warned:
        return
    from . import market_index
    now = time.time()
    with _lock:
        started = _state["started_at"]
        last_tick = _state["last_overseas_tick"]
    if now - started < _US_REALTIME_GRACE_SEC:
        return
    if last_tick > 0:
        return                              # tick 수신됨 — 정상
    us_held = [s for s in manager.held_symbols() if market_index.is_us(s)]
    if not us_held:
        return                              # 미국 보유 없음 — 감지 불가/불필요

    _us_realtime_warned = True
    log.warning("미국 실시간 시세 tick 미수신(%ds) — 해외 실시간 시세 미신청 추정. "
                "실시간 손절 불가. KIS HTS [7781] 시세신청 필요.",
                _US_REALTIME_GRACE_SEC)
    try:
        snap = broker.account_snapshot()
        push_snapshot({
            "balance": snap.get("balance", {}),
            "positions": snap.get("positions", []),
            "decisions": [],
            "cycle_summary": {
                "today": kst_today().isoformat(),
                "kind": "us_realtime_unavailable",
                "us_realtime_unavailable": True,
                "message": "미국 해외 실시간 시세가 수신되지 않습니다. KIS HTS "
                           "[7781] 해외 실시간 시세 신청 전까지 미국 종목의 "
                           "장중 실시간 손절(익절/손절/트레일링)이 제공되지 "
                           "않습니다. (장 마감 후 사이클에서만 청산 평가)",
            },
        })
    except Exception as e:
        log.warning("us_realtime 경고 push 실패: %s", e)


def start(market: str = "KRX") -> dict:
    """장중 stop loss 루프 시작. 별 thread에서 WebSocket 유지.

    market: 이번 loop이 추적할 시장 그룹('KRX'|'US'). 해당 시장 보유분만 구독·
    손절한다. 미국은 KIS 해외 실시간 시세 신청(HTS [7781])이 있어야 tick이 흐르며,
    미신청 시 구독은 되지만 데이터가 안 와 실시간 손절이 불가 → 감지해 고지한다.

    cycle 종료 후 호출. 이미 실행 중이면 무동작.
    Phase 38.3: use_mock 제거 — KisBroker 전용.
    """
    global _us_realtime_warned
    from .sync_client import pull_strategies

    # L-03: KRX 휴장일이면 loop 시작 자체를 skip — 시세 없는 시간대에 stop loss
    # 평가가 stale 데이터로 동작하는 사고 차단. US는 동적 플래너가 게이트.
    if market == "KRX":
        from quant_core import market_calendar as _mc
        today = kst_today()
        if not _mc.is_session_day("KR", today):
            log.info("KRX 휴장일 — intraday loop skip (today=%s)", today.isoformat())
            return {"status": "skipped_holiday", "market": "KRX",
                    "today": today.isoformat()}

    with _lock:
        if _state["running"]:
            log.info("intraday_loop 이미 실행 중")
            return {"status": "already_running"}

        _us_realtime_warned = False         # 세션마다 재감지
        log.info("intraday stop loop 시작 (market=%s)", market)
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

        from . import market_index

        def _in_market(sym: str) -> bool:
            try:
                return market_index.market_group_of(sym) == market
            except Exception:
                return market == "KRX"

        # 해외 tick 수신 시각 기록 (entitlement 감지용) 후 매니저로 전달
        def _on_tick_detect(sym: str, price: float) -> None:
            if market_index.is_us(sym):
                with _lock:
                    _state["last_overseas_tick"] = time.time()
            manager.on_tick(sym, price)

        ws = KisWebSocket(broker, on_tick=_on_tick_detect)
        ws_started = False
        try:
            ws.start()
            ws_started = True
        except Exception as e:
            # Q3: WebSocket 시작 자체 실패해도 loop 중단하지 않음. REST 폴링 fallback
            # thread가 ws.is_connected를 보고 폴링으로 stop loss 평가 유지.
            log.error("[%s] WebSocket 시작 실패 — REST 폴링 fallback만으로 동작: %s",
                       market, e)

        # 초기 구독: 이번 시장의 보유 종목만 (WebSocket 미동작이면 skip)
        held = [s for s in manager.held_symbols() if _in_market(s)]
        if held and ws_started:
            ws.subscribe(list(held))
            log.info("[%s] 초기 구독: %s (%d종목)", market, held, len(held))
        elif held:
            log.info("[%s] 초기 보유 %d종목 — WebSocket 미동작, 폴링 fallback이 평가",
                      market, len(held))
        us_subscribed = len(held) if market == "US" else 0

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

        # Q5 Tier 1+2 — 체결 직후 + 60초 monitor 평가 활성화.
        # risk_limits 받아서 일일 손실 한도 결정 (글로벌 default fallback).
        try:
            rl = pull_risk_limits()
        except Exception as e:
            log.warning("risk_limits pull 실패 — 글로벌 default 사용: %s", e)
            rl = {}
        from quant_core.exec_defaults import DEFAULT_EXECUTION
        daily_loss_limit_pct = (rl.get("kill_switch_daily_loss_pct")
                                  if rl.get("kill_switch_daily_loss_pct") is not None
                                  else DEFAULT_EXECUTION["daily_loss_limit_pct"])
        trader._daily_loss_limit_pct = float(daily_loss_limit_pct)

        def _on_ks_trigger(reason_source: str = "monitor") -> None:
            """Q5: kill switch 발동 시 호출. 미체결 cancel + 빈 cycle 재호출(청산
            패스) + 서버 push. reason_source는 'apply_fill' 또는 'monitor'."""
            log.critical("[ks-trigger] 발동 source=%s — 즉시 청산 cycle 시작",
                          reason_source)
            try:
                trader.cancel_all_pending(decisions=[])
            except Exception as e:
                log.error("[ks-trigger] cancel_all_pending 예외: %s", e)
            try:
                # 빈 strategies + 빈 candidates → 진입 0, 청산 패스만 실행.
                # trader.cycle은 _CYCLE_LOCK을 acquire (현 thread가 이미 락을
                # 쥐지 않은 상태로 호출).
                trader.cycle(strategies=[], dataset=qc.load_dataset(
                    with_indicators=True),
                              buy_candidates=[], risk_limits=rl, market=market)
            except Exception as e:
                log.error("[ks-trigger] cycle 예외: %s", e)
            try:
                snap = broker.account_snapshot()
                push_snapshot({
                    "balance": snap.get("balance", {}),
                    "positions": snap.get("positions", []),
                    "decisions": [],
                    "cycle_summary": {
                        "today": kst_today().isoformat(),
                        "kind": "kill_switch_triggered",
                        "source": reason_source,
                    },
                })
            except Exception as e:
                log.warning("[ks-trigger] push 실패: %s", e)

        # _apply_fill 끝에서도 동일 트리거 사용 (Tier 1).
        trader._ks_trigger_hook = _on_ks_trigger
        # 60초 monitor 시작 (Tier 2).
        manager.start_monitor(daily_loss_limit_pct, _on_ks_trigger,
                               period_sec=60.0)

        # 보유 종목 변화 추적 — 60초마다 sync_subscriptions
        stop_flag = threading.Event()

        def _sync_loop():
            while not stop_flag.wait(60):
                try:
                    target = {s for s in manager.held_symbols() if _in_market(s)}
                    result = ws.sync_subscriptions(target)
                    if result["added"] or result["removed"]:
                        log.info("[%s] 구독 sync: %s", market, result)
                    # 미국 실시간 시세 entitlement 감지 — 보유 있는데 grace 내 tick 0이면 미신청
                    if market == "US":
                        _check_us_realtime(broker, manager)
                except Exception as e:
                    log.warning("구독 sync 실패: %s", e)

        sync_thread = threading.Thread(target=_sync_loop, daemon=True,
                                          name="intraday-sync")
        sync_thread.start()

        # Q3: REST 폴링 fallback thread. WebSocket 끊김 감지 시 보유 종목들의
        # 현재가를 broker.price()로 주기 조회 → manager.on_tick으로 전달 → 기존
        # stop loss 평가 그대로 동작. WebSocket 복구 시 폴링 자동 skip.
        polling_thread = threading.Thread(
            target=_rest_polling_loop, daemon=True, name="intraday-polling",
            kwargs={"ws": ws, "broker": broker, "manager": manager,
                     "in_market_fn": _in_market, "stop_flag": stop_flag,
                     "market": market})
        polling_thread.start()

        _state.update({
            "running": True, "market": market, "ws": ws, "order_ws": order_ws,
            "manager": manager,
            "trader": trader, "broker": broker,
            "stop_flag": stop_flag, "sync_thread": sync_thread,
            "polling_thread": polling_thread,
            "started_at": time.time(), "us_subscribed": us_subscribed,
            "last_overseas_tick": 0.0,
        })
        return {"status": "started", "market": market,
                "initial_subscribed": len(held),
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
        # Q3 — REST 폴링 thread 종료 대기
        pt = _state.get("polling_thread")
        if pt:
            pt.join(timeout=5)

        manager = _state["manager"]
        broker = _state["broker"]
        # Q5: ks monitor 종료
        if manager is not None:
            try:
                manager.stop_monitor()
            except Exception as e:
                log.warning("ks-monitor stop 예외: %s", e)
        n_triggered = len(manager.decisions) if manager else 0

        # 마지막 sync push
        if broker and manager and n_triggered > 0:
            _push_after_sell(broker, manager.decisions)

        _state.update({
            "running": False, "ws": None, "manager": None,
            "trader": None, "broker": None,
            "stop_flag": None, "sync_thread": None,
            "polling_thread": None,
        })
        return {"status": "stopped", "n_triggered": n_triggered}


def status() -> dict:
    with _lock:
        if not _state["running"]:
            return {"running": False}
        order_ws = _state["order_ws"]
        # 미국 실시간 시세 수신 여부 (US loop에서만 의미). 미수신 경고가 떴으면 False.
        us_realtime_ok = not (_state["market"] == "US" and _us_realtime_warned)
        return {
            "running": True,
            "market": _state["market"],
            "subscribed": len(_state["ws"]._symbols) if _state["ws"] else 0,
            "ws_connected": _state["ws"].is_connected if _state["ws"] else False,
            "order_ws_enabled": order_ws is not None,
            "order_ws_connected": order_ws.is_connected if order_ws else False,
            "us_realtime_ok": us_realtime_ok,
            "n_triggered_today":
                len(_state["manager"].decisions) if _state["manager"] else 0,
        }
