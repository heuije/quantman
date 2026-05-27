"""모의투자 사이클 오케스트레이션 — 전략 풀 → 평가·매매 → 스냅샷 푸시.

견고성: 플랫폼 연결이 끊겨도 매매는 로컬에서 완료한다.
  - 전략 풀 실패 → 신규 진입 없이 기존 보유분 청산만 평가
  - 스냅샷 푸시 실패 → 보류 큐에 저장, 다음 사이클에 재전송

체결통보 WebSocket(intraday_loop, 08:50 시작)이 메인 사이클(08:55) 전 ready
상태가 되어야 시초가(09:00) 체결 통보를 push로 받을 수 있다. _wait_for_order_ws
가 진입 직전 한 번 확인 — 미연결 시 짧게 대기 후 경고 로그 남기고 진행
(REST 폴링으로 fallback, 데이터 누락은 없음).
"""

from __future__ import annotations

import json
import logging
import time

import quant_core as qc

from .broker import Broker
from .config import PENDING_PATH
from .file_security import restrict_to_owner
from .logging_setup import setup_logging
from .secrets_store import load_kis
from .sync_client import (pull_krx_status, pull_preview, pull_risk_limits,
                            pull_strategies, push_snapshot)
from .trader import Trader

log = logging.getLogger("localapp.runner")

_ORDER_WS_WAIT_SEC = 5
_ORDER_WS_RETRIES = 2


def make_broker() -> Broker:
    """KisBroker 생성. KIS 자격증명이 없으면 명시적 RuntimeError (Phase 38.3).

    이전엔 자격증명 없으면 조용히 MockBroker로 fallback해 사용자가 "모의투자
    중"이라고 착각할 수 있는 신뢰 위험이 있었음. 이제는 페어링·자격증명을
    먼저 마치도록 요구.
    """
    if load_kis() is None:
        raise RuntimeError(
            "KIS 자격증명이 등록되지 않았습니다. setup을 실행해 페어링·KIS 키를 "
            "먼저 등록하세요. (KIS 모의투자 가입은 무료이며 즉시 발급됩니다.)")
    from .kis_broker import KisBroker          # KIS 자격증명 필요 시에만 import
    return KisBroker()


def _flush_pending() -> None:
    """이전 사이클에서 전송 실패한 스냅샷이 있으면 재전송한다."""
    if not PENDING_PATH.exists():
        return
    try:
        payload = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        push_snapshot(payload)
        PENDING_PATH.unlink()
        log.info("보류된 스냅샷 재전송 완료")
    except Exception as e:
        log.warning("보류 스냅샷 재전송 실패 (다음 사이클 재시도): %s", e)


def _wait_for_order_ws() -> None:
    """메인 사이클 진입 직전 체결통보 WebSocket ready 확인.

    intraday_loop이 08:50에 시작했으면 08:55까지 보통 연결+AES key/iv 수신
    완료. 그러나 KIS API 지연 시 미연결 가능. 짧게 대기 후 미연결이면 경고만
    남기고 진행 — REST 폴링으로 fallback (데이터 누락 없음, push 지연만).

    HTS ID 미설정 사용자는 체결통보 WebSocket 자체가 disabled이므로 무동작.
    """
    kis = load_kis() or {}
    if not kis.get("hts_id"):
        return  # 체결통보 WebSocket disabled — 확인 불필요

    from . import intraday_loop      # 순환 import 회피 — 지연 로딩
    for attempt in range(1, _ORDER_WS_RETRIES + 2):
        st = intraday_loop.status()
        if not st.get("running"):
            log.warning("intraday_loop이 시작되지 않음 — 08:50 cron 누락 가능. "
                         "메인 사이클은 진행 (REST 폴링 fallback)")
            return
        if st.get("order_ws_connected"):
            log.info("체결통보 WebSocket ready (시도 %d)", attempt)
            return
        if attempt > _ORDER_WS_RETRIES:
            break
        log.info("체결통보 WebSocket 미연결 — %d초 후 재확인 (#%d)",
                  _ORDER_WS_WAIT_SEC, attempt + 1)
        time.sleep(_ORDER_WS_WAIT_SEC)
    log.warning("체결통보 WebSocket 미연결 상태로 메인 사이클 진행 — "
                 "시초가 체결 통보는 REST 폴링으로 반영됨 (push 지연 가능)")


def run_cycle(market: str = "KRX", catchup: bool = False) -> dict:
    """1회 자동매매 사이클을 실행하고 동기화 스냅샷을 반환한다.

    market: 이번 사이클이 다룰 시장 그룹('KRX' 또는 'US'). 스케줄러가 각 시장의
    정규장 시각에 맞춰 호출한다. 청산·진입은 해당 시장 종목만 처리.

    catchup: PC가 꺼져 있어 missed된 cycle을 기동 시 뒤늦게 실행하는 경우 True.
    catchup.run_catchup_on_startup이 호출하며, trader가 시장가 매수를 시초가
    limit으로 자동 변환 (백테스트 alignment + selection bias 없음).
    """
    setup_logging()
    _flush_pending()

    # L-03: KRX 휴장일(공휴일·임시휴장)이면 사이클 중단 — 휴장에 매도 발주·
    # stale 시세 평가 방지. US는 동적 야간 플래너가 비세션일을 이미 건너뛴다.
    if market == "KRX":
        from quant_core import market_calendar as _mc
        from .trader import kst_today
        today = kst_today()
        # Q2+Q8: 캘린더 만료 임박 시 경고 로그(AL-3: 사이클은 차단 안 함 — KIS가
        # 휴장이면 거부, 잘못 차단 시 기회손실이 더 큼).
        fresh, msg = _mc.check_fresh("KR", today, lookahead_days=7)
        if not fresh:
            log.warning("[calendar] %s", msg)
        if not _mc.is_session_day("KR", today):
            log.info("KRX 휴장일 — 사이클 skip (today=%s)", today.isoformat())
            return {"status": "skipped_holiday", "market": "KRX",
                    "today": today.isoformat()}

    # 체결통보 WebSocket ready 확인 (08:50 intraday_loop과 race condition 방지)
    _wait_for_order_ws()

    # L-01: 직전 사이클에서 'submitting'으로 끝난 intent(=발주 직전 크래시)를 KIS
    # 당일 주문 조회로 매칭해 submitted/failed로 마감. 매칭되면 중복 발주 차단,
    # 미매칭이면 정상 재시도 허용. 자세한 설계는 intents.py.
    from . import intents as _intents
    from .trader import kst_today as _kst_today
    try:
        _broker_for_reconcile = make_broker()
        rec = _intents.reconcile_submitting(_broker_for_reconcile,
                                            _kst_today().isoformat())
        if any(rec.get(k) for k in ("matched", "no_fill", "ambiguous",
                                    "kis_query_failed")):
            log.info("[L-01] intent reconcile: %s", rec)
    except Exception as e:
        # reconcile 실패해도 cycle은 진행 — 게이트는 submitting 상태 유지하여
        # 중복 발주 위험을 보수적으로 차단.
        log.warning("[L-01] intent reconcile 실패(보수적 차단 유지): %s", e)

    try:
        strategies = pull_strategies()
        log.info("배정된 전략 %d개", len(strategies))
    except Exception as e:
        log.warning("전략 풀 실패 — 신규 진입 없이 보유분 청산만 평가: %s", e)
        strategies = []

    # 본 사이클 실행 — 데이터 fetch·broker·trader. 어디서 예외가 나도 서버에
    # error snapshot push해 서버가 missed를 case C(cycle 실행 실패)로 정확히
    # 분류할 수 있게 함. 이전엔 예외가 그대로 propagate해 서버는 그냥 push
    # 없음만 봤고, 사용자는 "왜 안 됐는지" 추적 불가했다.
    try:
        from .datafetch import refresh_market_data
        refresh_market_data()
        dataset = qc.load_dataset(with_indicators=True)
        broker = make_broker()
        trader = Trader(broker)

        # Phase 38.4 — preview 신뢰 + 누락 시 청산만. legacy 평가 경로 제거.
        preview = None
        try:
            preview = pull_preview()
        except Exception as e:
            log.warning("preview pull 예외 — 신규 진입 차단: %s", e)
        preview_missing = preview is None
        buy_candidates = (preview or {}).get("by_strategy") if preview else []
        if preview_missing:
            log.warning("preview 없음 — 신규 진입 보류, 청산만 진행")
        elif buy_candidates:
            n_total = sum(len(e.get("candidates") or []) for e in buy_candidates)
            log.info("preview 경로 — by_strategy=%d, 총 후보 종목=%d (신호 재평가 skip)",
                      len(buy_candidates), n_total)
        else:
            log.info("preview 후보 없음 — 매수 0, 청산만 진행")

        # Phase 38.7/38.10 — 사용자 위험 한도. 실패 시 빈 dict → default fallback.
        risk_limits = pull_risk_limits()
        # Phase 48 — KRX 종목 상태 (거래정지·관리). 매수 직전 trader가 차단 판단.
        krx_status = pull_krx_status()
        payload = trader.cycle(strategies, dataset, buy_candidates=buy_candidates,
                                 risk_limits=risk_limits, market=market,
                                 krx_status=krx_status, catchup=catchup)
        if preview_missing:
            payload.setdefault("cycle_summary", {})["preview_missing"] = True
    except Exception as cycle_err:
        log.exception("cycle 실행 중 예외 — 서버에 error snapshot push: %s", cycle_err)
        payload = {
            "balance": {"cash": 0, "total_eval": 0},
            "positions": [], "equity": [], "trades": [], "decisions": [],
            "cycle_summary": {
                "market": market,
                "error": f"{type(cycle_err).__name__}: {cycle_err}",
                "n_bought": 0, "n_sold": 0,
            },
        }

    try:
        push_snapshot(payload)
        log.info("동기화 완료 — 평가금액 %s원", f"{payload['balance']['total_eval']:,}")
    except Exception as e:
        PENDING_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                                encoding="utf-8")
        # 잔고·포지션·체결 정보는 같은 PC의 다른 사용자가 읽으면 안 됨.
        restrict_to_owner(PENDING_PATH)
        log.warning("동기화 실패 — 보류 큐 저장 (다음 사이클 재전송): %s", e)

    return payload


def run_post_close_settlement(market: str = "KRX") -> dict:
    """장 마감 후 미체결 정리 + 잔고 reconcile + 잔고 스냅샷 push.

    market: 어느 시장 마감 후 정산인지(KRX/US) — 로깅용. _resolve_pending과
    reconcile은 계좌 전체(국내+해외)를 대상으로 하므로 동작은 시장 무관.

    Phase 32: 정규장 마감 직후 KIS에 미체결 주문 상태 조회 → 자동 취소 확인
    → ledger·pending 동기화 → 즉시 서버 push.

    Phase 40: ledger ↔ KIS 잔고 reconcile 실행 (매매가 끝난 직후라 안전).
    HTS/MTS 수동 매도분을 ledger에서 자동 차감.

    Q5(AL-4): trader._CYCLE_LOCK으로 cycle·장중 ks 트리거와 직렬화. 장 마감
    직전에 ks 트리거가 cycle을 돌리는 중이라면 settlement는 잠시 대기 후 진입.
    """
    from .trader import kst_today, _CYCLE_LOCK
    setup_logging()
    with _CYCLE_LOCK:
        return _run_post_close_settlement_locked(market)


def _run_post_close_settlement_locked(market: str) -> dict:
    from .trader import kst_today
    _flush_pending()

    today_d = kst_today()  # L-06: PC tz와 무관한 KST 거래일
    # L-03: KRX 휴장일에는 정산도 무의미(체결 없음)·KIS 잔고 reconcile 부작용 우려.
    if market == "KRX":
        from quant_core import market_calendar as _mc
        if not _mc.is_session_day("KR", today_d):
            log.info("KRX 휴장일 — settlement skip (today=%s)", today_d.isoformat())
            return {"status": "skipped_holiday", "market": "KRX",
                    "today": today_d.isoformat()}
    today = today_d.isoformat()
    log.info("장 마감 후 settlement 시작 (market=%s)", market)
    broker = make_broker()
    trader = Trader(broker)

    decisions: list[dict] = []
    trader._resolve_pending(decisions)

    # Phase 40 — ledger ↔ KIS 정합성 자동 정정 (매매 직전 08:55엔 위험, 15:35에 실행)
    reconcile_result = trader.reconcile_with_kis(today_iso=today)
    if reconcile_result.get("has_drift"):
        log.warning("reconcile drift 감지 — applied=%d, external_extras=%d",
                     len(reconcile_result.get("applied") or []),
                     reconcile_result.get("external_extras_count", 0))

    try:
        snap = broker.account_snapshot()
    except Exception as e:
        log.error("잔고 조회 실패: %s", e)
        return {"error": str(e)}

    payload = {
        "balance": snap.get("balance", {}),
        "positions": snap.get("positions", []),
        "decisions": decisions,
        "reconciliation": reconcile_result,
        "cycle_summary": {
            "today": today,
            "market": market,                     # Phase 7 catch-up — 시장 식별
            "kind": "post_close_settlement",
            "reconcile_drift": reconcile_result.get("has_drift", False),
            "reconcile_applied": len(reconcile_result.get("applied") or []),
        },
    }
    # post_close_settlement은 cycle entry처럼 cycles.jsonl에 명시적 기록.
    # trader.cycle은 자체적으로 log_cycle 호출하지만 settlement는 trader 외부에서
    # 일어나므로 여기서 명시. catch-up이 cycles.jsonl로 idempotency 판단.
    try:
        from . import order_log
        order_log.log_cycle(decisions, payload["cycle_summary"])
    except Exception as e:
        log.warning("settlement cycle 기록 실패 (catch-up 판단에 영향): %s", e)

    try:
        push_snapshot(payload)
        log.info("settlement 동기화 완료 — 미체결 정리 %d건",
                  sum(1 for d in decisions if d.get("action") == "timeout"))
    except Exception as e:
        PENDING_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        restrict_to_owner(PENDING_PATH)
        log.warning("settlement 동기화 실패 — 보류 큐 저장: %s", e)

    return payload
