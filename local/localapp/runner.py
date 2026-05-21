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
from .sync_client import (pull_preview, pull_risk_limits, pull_strategies,
                            push_snapshot)
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


def run_cycle() -> dict:
    """1회 자동매매 사이클을 실행하고 동기화 스냅샷을 반환한다."""
    setup_logging()
    _flush_pending()

    # 체결통보 WebSocket ready 확인 (08:50 intraday_loop과 race condition 방지)
    _wait_for_order_ws()

    try:
        strategies = pull_strategies()
        log.info("배정된 전략 %d개", len(strategies))
    except Exception as e:
        log.warning("전략 풀 실패 — 신규 진입 없이 보유분 청산만 평가: %s", e)
        strategies = []

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
    payload = trader.cycle(strategies, dataset, buy_candidates=buy_candidates,
                             risk_limits=risk_limits)
    if preview_missing:
        payload.setdefault("cycle_summary", {})["preview_missing"] = True

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


def run_post_close_settlement() -> dict:
    """장 마감 후(15:35) 미체결 정리 + 잔고 reconcile + 잔고 스냅샷 push.

    Phase 32: 정규장 마감 직후 KIS에 미체결 주문 상태 조회 → 자동 취소 확인
    → ledger·pending 동기화 → 즉시 서버 push. 다음날 08:55까지 미체결 표시
    오류 없이 정확.

    Phase 40: ledger ↔ KIS 잔고 reconcile 실행 (15:35는 매매가 끝난 직후라
    안전). HTS/MTS 수동 매도분을 ledger에서 자동 차감.
    """
    from datetime import date
    setup_logging()
    _flush_pending()

    today = date.today().isoformat()
    log.info("장 마감 후 settlement 시작 (15:35)")
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
            "kind": "post_close_settlement",
            "reconcile_drift": reconcile_result.get("has_drift", False),
            "reconcile_applied": len(reconcile_result.get("applied") or []),
        },
    }

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
