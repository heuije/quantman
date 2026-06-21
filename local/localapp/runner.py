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
import uuid

import quant_core as qc

from .broker import Broker
from .config import PENDING_PATH
from .state_store import save_json
from .logging_setup import setup_logging
from .secrets_store import load_kis, get_active_broker
from .sync_client import (pull_krx_status, pull_preview, pull_risk_limits,
                            pull_strategies, push_snapshot)
from .trader import Trader

log = logging.getLogger("localapp.runner")

_ORDER_WS_WAIT_SEC = 5
_ORDER_WS_RETRIES = 2


def make_broker() -> Broker:
    """활성 브로커를 생성한다. 기본은 KIS(기존 사용자 무변경); LS로 전환하면 LsBroker 반환.

    브로커 선택은 secrets_store.get_active_broker()가 SSOT("kis"|"ls"). 미설정이면 "kis".

    KIS 경로: 자격증명 없으면 명시적 RuntimeError. 선물 자격증명이 있으면 BrokerRouter로
    감싼다(심볼이 선물이면 KisFuturesBroker로 라우팅·계약코드 해석). 선물 미등록이면
    KisBroker를 그대로 반환 — 주식 전용 환경은 완전 무변경.

    LS 경로: 국내주식 단일. 선물 라우터는 후속 plan.
    """
    from .secrets_store import get_active_broker, load_ls
    if get_active_broker() == "ls":
        if load_ls() is None:
            raise RuntimeError(
                "LS 자격증명이 등록되지 않았습니다. setup에서 LS appkey/secret/계좌를 "
                "등록하세요. (LS 모의투자는 별도 키로 발급됩니다.)")
        from .ls_broker import LsBroker
        stock = LsBroker()
        from .secrets_store import load_ls_futures, load_ls_overseas_futures
        if not (load_ls_futures() or load_ls_overseas_futures()):
            return stock                         # 국내주식만 — 무변경
        from .ls_futures_broker import LsFuturesBroker
        from .ls_futures_contracts import LsContractResolver
        from .broker_router import BrokerRouter
        r = LsContractResolver(LsFuturesBroker())   # resolver가 선물 토큰으로 master fetch
        return BrokerRouter(stock, r.broker,
                            resolve=r.resolve, resolve_expiry=r.resolve_expiry,
                            dataset_for_code=r.dataset_for_code)
    # ── 기존 KIS 경로 (완전 무변경) ──────────────────────────────────────────
    if load_kis() is None:
        raise RuntimeError(
            "KIS 자격증명이 등록되지 않았습니다. setup을 실행해 페어링·KIS 키를 "
            "먼저 등록하세요. (KIS 모의투자 가입은 무료이며 즉시 발급됩니다.)")
    from .kis_broker import KisBroker          # KIS 자격증명 필요 시에만 import
    stock = KisBroker()

    from .secrets_store import load_kis_futures, load_kis_overseas_futures
    if not (load_kis_futures() or load_kis_overseas_futures()):
        return stock                           # 선물 미등록 → 기존 KisBroker 그대로(무변경)

    from .kis_futures_broker import KisFuturesBroker
    from .futures_contracts import ContractResolver
    from .broker_router import BrokerRouter
    # 단일 ContractResolver 인스턴스 — resolve(계약코드)와 resolve_expiry(M6 만기)가 같은
    # 마스터 캐시(하루 1회 다운로드)를 공유한다(중복 다운로드 방지).
    cr = ContractResolver()
    return BrokerRouter(stock, KisFuturesBroker(),
                        resolve=cr.resolve, resolve_expiry=cr.resolve_expiry)


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


def run_emergency_liquidation() -> dict:
    """비상 전량 청산 — run_cycle과 독립 경로(웹 LIQUIDATE_ALL 전용).

    dataset 번들 다운로드·preview·전략 풀·시장 스케줄 의존 없이, 브로커 실보유를
    즉시 매도/환매한다. 정규 run_cycle은 dataset 다운로드를 선행 필수로 두어
    네트워크 불안정 시 비상정지가 무한 대기(hang)하던 구조적 결함이 있었다 — 이 경로는
    그 의존을 제거해 비상정지가 항상 빠르게 동작하도록 격리한다.

    호출자(gui)가 killswitch.activate를 먼저 한다. 결과 스냅샷은 여기서 push.
    """
    setup_logging()
    _flush_pending()
    broker = make_broker()
    trader = Trader(broker)
    payload = trader.liquidate_all_held()
    try:
        push_snapshot(payload)
    except Exception as e:
        log.warning("비상청산 스냅샷 push 실패 (보류 큐 저장): %s", e)
        try:
            save_json(PENDING_PATH, payload)
        except Exception:
            pass
    return payload


def push_state_snapshot() -> dict | None:
    """현 상태 스냅샷을 즉시 push — 상태 변경(kill-switch 해제·일시정지·재개·주문취소 등)을
    웹에 실시간 반영. 거래/사이클 없이 잔고·포지션·kill_switch·auto_status만 갱신.

    **best-effort**: 자격증명 부재·KIS·네트워크 실패해도 호출자(이미 로컬 상태는 반영됨)에
    영향 없음 — 웹은 다음 사이클에 어차피 따라잡는다. 동기화 지연만 줄이는 보조 경로.
    """
    try:
        broker = make_broker()
        trader = Trader(broker)
        payload = trader.state_snapshot()
        push_snapshot(payload)
        return payload
    except Exception as e:
        log.warning("상태 스냅샷 push 실패 (무시 — 다음 사이클 반영): %s", e)
        return None


def _wait_for_order_ws() -> None:
    """메인 사이클 진입 직전 체결통보 WebSocket ready 확인.

    intraday_loop이 08:50에 시작했으면 08:55까지 보통 연결+AES key/iv 수신
    완료. 그러나 KIS API 지연 시 미연결 가능. 짧게 대기 후 미연결이면 경고만
    남기고 진행 — REST 폴링으로 fallback (데이터 누락 없음, push 지연만).

    HTS ID 미설정 사용자는 체결통보 WebSocket 자체가 disabled이므로 무동작.

    LS 브로커 활성 시: KIS 체결통보 WS가 없으므로 즉시 반환(LS WS는 Phase 3 후속 계획).
    REST 폴링이 체결 인지 fallback이므로 데이터 누락 없음.
    """
    # (b) KIS 전용: LS 활성 시 KIS WS 경로는 완전 무관 — 즉시 반환.
    if get_active_broker() != "kis":
        return

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


_RETRY_BACKOFF_SEC = (60, 300, 900)       # KRX 정규장 6.5h 윈도우 — 긴 backoff 무방
_RESERVED_BACKOFF_SEC = (30, 60)          # US 예약: 접수창(개장-10분) 내 발주 마감 필요


def _cycle_backoffs(reserved: bool) -> tuple[int, ...]:
    """cycle 재시도 backoff 시퀀스. 예약(US) cycle은 개장-20분에 시작해 접수창(개장
    -10분)이 닫히기 전 발주를 끝내야 한다 — 긴 backoff(300·900s)는 접수창을 넘겨
    예약주문을 누락시키므로 짧은 시퀀스만. KRX는 정규장 윈도우가 길어 무방."""
    return _RESERVED_BACKOFF_SEC if reserved else _RETRY_BACKOFF_SEC


def run_cycle(market: str = "KRX", catchup: bool = False,
              reserved: bool = False, trigger: str = "cron") -> dict:
    """1회 자동매매 사이클을 실행하고 동기화 스냅샷을 반환한다.

    market: 이번 사이클이 다룰 시장 그룹('KRX' 또는 'US'). 스케줄러가 각 시장의
    정규장 시각에 맞춰 호출한다. 청산·진입은 해당 시장 종목만 처리.

    catchup: PC가 꺼져 있어 missed된 cycle을 기동 시 뒤늦게 실행하는 경우 True.
    catchup.run_catchup_on_startup이 호출하며, trader가 시장가 매수를 시초가
    limit으로 자동 변환 (백테스트 alignment + selection bias 없음).

    reserved: 미국 개장 전 entry cycle(스케줄러 us_cycle)에서만 True. trader가
    매수=지정가 예약·매도=MOO 예약으로 라우팅(개장 시 자동전송). 장중 손절/킬
    스위치 청산은 즉시 시장주문이어야 하므로 항상 False.

    trigger: 사이클 트리거 출처("cron"/"manual"/"web"/"catchup"/"cli") —
    cycles.jsonl 시작 레코드에 기록돼 다중 트리거 표면(리뷰 D6-5)을 사후 식별.
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

    # 사이클 lifecycle 저널 — 시작을 먼저 기록해 "시작 안 함 vs 시작 후 정지"를
    # 구분 가능하게 한다(리뷰 D6-2: stall된 사이클이 cycles.jsonl에 무흔적이라
    # 진단이 py-spy까지 필요했던 관측 공백). kind="cycle_started"는 catchup
    # idempotency(_classify_entry의 kind 매칭)와 충돌하지 않는다 — catchup은
    # "cycle"/"post_close_settlement" 류 완료 kind만 매칭한다.
    from . import order_log
    cycle_id = uuid.uuid4().hex[:12]
    order_log.log_cycle([], {"market": market, "kind": "cycle_started",
                             "cycle_id": cycle_id, "trigger": trigger,
                             "reserved": reserved, "catchup": catchup})

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

    # 본 사이클 실행 — 데이터 fetch·broker·trader. 어디서 예외가 나도 서버에
    # error snapshot push해 서버가 missed를 case C(cycle 실행 실패)로 정확히
    # 분류할 수 있게 함. 이전엔 예외가 그대로 propagate해 서버는 그냥 push
    # 없음만 봤고, 사용자는 "왜 안 됐는지" 추적 불가했다.
    #
    # v0.9.13 D-3 (C) — outer exception 발생 시 1분·5분·15분 backoff 3회 재시도.
    # KIS 일시 거부·네트워크 transient·서버 일시 장애에 자동 회복. 총 4 시도
    # (최대 ~21분). 모두 실패해야 error snapshot. 자금 안전은 L-01 intent
    # journal idempotency가 차단 (이미 발주된 매수는 KIS 당일 주문 매칭으로
    # 중복 방지). KRX 정규장 6시간 30분 윈도우라 21분 backoff 안전 마진 충분.
    backoffs = _cycle_backoffs(reserved)  # 예약(US)은 접수창 내 짧게, KRX는 길게
    payload = None
    cycle_err: Exception | None = None
    n_attempts = len(backoffs) + 1
    strategies_pull_failed = False
    for attempt in range(1, n_attempts + 1):
        try:
            # 전략 풀 — backoff 루프 안에서 수행해 transient 서버 장애가 cycle
            # 재시도로 회복되게 한다(리뷰 D4-3: 이전엔 루프 밖 1회 시도라 일시
            # 502가 즉시 "신규 진입 없음"으로 무음 확정). 마지막 시도에서도
            # 실패하면 보유분 청산만 진행하되 cycle_summary에 표면화.
            strategies_pull_failed = False
            try:
                strategies = pull_strategies()
                log.info("배정된 전략 %d개", len(strategies))
            except Exception:
                if attempt <= len(backoffs):
                    raise  # transient — cycle backoff 재시도로 회복
                strategies_pull_failed = True
                strategies = []
                log.warning("전략 풀 %d회 모두 실패 — 신규 진입 없이 보유분 "
                            "청산만 평가", n_attempts)

            from .datafetch import refresh_market_data
            _t0 = time.monotonic()
            log.info("[cycle] ① 시세 데이터 갱신 시작 (market=%s, reserved=%s, catchup=%s)",
                      market, reserved, catchup)
            refresh_market_data()
            log.info("[cycle] ② 시세 갱신 완료 %.1fs — broker 준비", time.monotonic() - _t0)
            broker = make_broker()
            trader = Trader(broker)

            # Phase 38.4 — preview 신뢰 + 누락 시 청산만. legacy 평가 경로 제거.
            preview = None
            _t0 = time.monotonic()
            log.info("[cycle] ③ preview pull 시작")
            try:
                preview = pull_preview()
            except Exception as e:
                log.warning("preview pull 예외 — 신규 진입 차단: %s", e)
            log.info("[cycle] ③ preview pull 완료 %.1fs (missing=%s)",
                      time.monotonic() - _t0, preview is None)
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

            # B1 — dataset을 실제 사용 종목만 로드 (전체 4468 지표 계산 5분+ → 수초).
            # broker·preview 이후로 옮긴 이유: 보유(ledger)·후보가 있어야 needed
            # 집합을 계산할 수 있기 때문. 매수는 신호 재평가 안 하고(매수후보=preview),
            # 매도는 IR position.exit.condition이 조건 참조 종목을 보므로 dataset_scope가
            # macro ∪ 타겟/후보 ∪ 보유 ∪ 조건참조를 모두 포함.
            from . import dataset_scope
            needed = dataset_scope.needed_symbols(
                strategies, buy_candidates, trader.ledger)
            _t0 = time.monotonic()
            log.info("[cycle] ④ dataset 로드 시작 (%d종목 needed, 지표계산 포함)", len(needed))
            dataset = qc.load_dataset_for(needed, with_indicators=True)
            log.info("[cycle] ④ dataset 로드 완료 — %d종목 %.1fs", len(dataset), time.monotonic() - _t0)

            # Phase 38.7/38.10 — 사용자 위험 한도. 실패 시 빈 dict → default fallback.
            risk_limits = pull_risk_limits()
            # Phase 48 — KRX 종목 상태 (거래정지·관리). 매수 직전 trader가 차단 판단.
            krx_status = pull_krx_status()
            log.info("[cycle] ⑤ 본문 실행 시작 (trader.cycle, market=%s)", market)
            _t0 = time.monotonic()
            payload = trader.cycle(strategies, dataset, buy_candidates=buy_candidates,
                                     risk_limits=risk_limits, market=market,
                                     krx_status=krx_status, catchup=catchup,
                                     reserved=reserved, cycle_id=cycle_id)
            _cs = (payload or {}).get("cycle_summary") or {}
            log.info("[cycle] ⑤ 본문 완료 %.1fs — bought=%s sold=%s skip_held=%s errors=%s",
                      time.monotonic() - _t0, _cs.get("n_bought"), _cs.get("n_sold"),
                      _cs.get("n_skip_held"), _cs.get("n_errors"))
            if preview_missing:
                payload.setdefault("cycle_summary", {})["preview_missing"] = True
            if strategies_pull_failed:
                payload.setdefault("cycle_summary", {})["strategies_pull_failed"] = True
            cycle_err = None
            break
        except Exception as e:
            cycle_err = e
            if attempt <= len(backoffs):
                wait_sec = backoffs[attempt - 1]
                log.warning("cycle 실행 예외 (시도 %d/%d) — %d초 후 재시도: %s",
                              attempt, n_attempts, wait_sec, e)
                time.sleep(wait_sec)
                continue
            log.exception("cycle 실행 중 예외 (시도 %d/%d) — 서버에 error snapshot push: %s",
                          attempt, n_attempts, e)

    if cycle_err is not None or payload is None:
        _err_str = (f"{type(cycle_err).__name__}: {cycle_err}"
                    if cycle_err else "unknown")
        payload = {
            "balance": {"cash": 0, "total_eval": 0},
            "positions": [], "equity": [], "trades": [], "decisions": [],
            "cycle_summary": {
                "market": market, "cycle_id": cycle_id,
                "error": _err_str,
                "n_bought": 0, "n_sold": 0,
            },
        }
        # 전 재시도 실패도 로컬 진실(cycles.jsonl)에 남긴다 — 이전엔 서버 push만
        # 시도해 서버가 죽어 있으면 로컬 무흔적이었다(리뷰 D1-4). summary.error가
        # 있는 entry는 catchup의 _last_of가 "완료"로 보지 않으므로(v0.9.13 D-1)
        # catch-up 자동 재시도와 정합.
        order_log.log_cycle([], {"market": market, "kind": "cycle_error",
                                 "cycle_id": cycle_id, "error": _err_str})

    try:
        push_snapshot(payload)
        log.info("동기화 완료 — 평가금액 %s원", f"{payload['balance']['total_eval']:,}")
    except Exception as e:
        # 잔고·포지션·체결 정보는 같은 PC의 다른 사용자가 읽으면 안 됨 (R5: 원자+ACL).
        save_json(PENDING_PATH, payload)
        log.warning("동기화 실패 — 보류 큐 저장 (다음 사이클 재전송): %s", e)

    return payload


def run_close_cycle(market: str = "KRX", instrument_class: str = "stock") -> dict:
    """종가 청산 사이클 — 당일매매(hold_days==0) 포지션만 종가 기준 청산 (Stage B).

    아침 메인 cycle(08:55)은 시가 진입까지만 담당하고, 당일매매 청산은 이 사이클이
    종가 발주창에 전담한다(국내주식 15:25·국내선물 15:40·미국주식·해외선물 폐장−5분). 매수·
    preview·KIS intent reconcile은 불필요 — 청산은 ledger(보유)+dataset(ref/clamp)만 있으면
    되므로 run_cycle의 무거운 진입 경로를 재사용하지 않고 격리한다(Over-engineering 회피).

    market ∈ {"KRX","US"}, instrument_class ∈ {"stock","futures"} — 시장·클래스별 종가창이
    달라 스케줄러가 분리 잡으로 호출한다(US-F4: 해외선물도 폐장−5분에 청산 — 종전 stock만이라
    선물 day-trade가 오버나이트 방치됐다. CME 정산시각 정밀정렬은 라이브 정밀화). 신규 Trader라
    _reserved_us=False → trader.liquidate_day_trades가 클래스로 라우팅(국내=시장가 단일가,
    미국=라이브 지정가 시장가근사).
    """
    setup_logging()
    _flush_pending()

    # L-03: KRX 휴장일이면 종가 청산도 무의미(체결 없음) — run_cycle과 동일 가드.
    if market == "KRX":
        from quant_core import market_calendar as _mc
        from .trader import kst_today
        today = kst_today()
        if not _mc.is_session_day("KR", today):
            log.info("KRX 휴장일 — 종가청산 사이클 skip (today=%s)", today.isoformat())
            return {"status": "skipped_holiday", "market": "KRX",
                    "today": today.isoformat()}

    broker = make_broker()
    trader = Trader(broker)

    # B1 — dataset을 보유 종목만 로드(매수 후보 없음 — 종가 청산은 진입 안 함).
    # ref_price는 _safe_price(현재가) 우선, dataset Close는 fallback.
    from . import dataset_scope
    needed = dataset_scope.needed_symbols([], [], trader.ledger)
    dataset = qc.load_dataset_for(needed, with_indicators=True)
    log.info("종가청산 dataset 로드 — %d종목 (보유 scoped)", len(dataset))

    payload = trader.liquidate_day_trades(dataset, instrument_class, market=market)
    try:
        push_snapshot(payload)
        log.info("종가청산 사이클 완료 (market=%s, class=%s)", market, instrument_class)
    except Exception as e:
        save_json(PENDING_PATH, payload)
        log.warning("종가청산 동기화 실패 — 보류 큐 저장: %s", e)
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
    from .trader import _CYCLE_LOCK
    setup_logging()
    with _CYCLE_LOCK:
        return _run_settlement_locked(market, kind="post_close_settlement",
                                       label="장 마감 후 settlement")


def run_post_open_reconcile(market: str = "US") -> dict:
    """미국 개장 직후(open+5분) 예약주문 체결 reconcile + 스냅샷 push.

    예약 지정가 매수·MOO 매도가 개장(22:30/23:30)에 전송·체결된 직후 ledger·
    서버를 동기화한다. 미체결 working 매수는 취소하지 않는다(_resolve_pending은
    상태 조회만 — 장중 자연 체결 허용). MOO 매도는 개장에 완결되고 working 매수는
    체결분만 ledger에 들어오므로 reconcile_with_kis가 in-flight drift로 오작동하지
    않는다. settlement와 동일 본문 — kind만 다르다.
    """
    from .trader import _CYCLE_LOCK
    setup_logging()
    with _CYCLE_LOCK:
        return _run_settlement_locked(market, kind="post_open_reconcile",
                                       label="개장 후 reconcile")


def _run_settlement_locked(market: str, kind: str, label: str) -> dict:
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
    log.info("%s 시작 (market=%s)", label, market)
    broker = make_broker()
    trader = Trader(broker)

    decisions: list[dict] = []
    trader._resolve_pending(decisions)

    # Phase 40 — ledger ↔ KIS 정합성 자동 정정 (매매 직전 08:55엔 위험, 15:50에 실행)
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
            "kind": kind,
            "reconcile_drift": reconcile_result.get("has_drift", False),
            "reconcile_applied": len(reconcile_result.get("applied") or []),
            # N2 — 정산 후에도 남은 미체결(장 마감 뒤라 전부 비정상 잔존).
            # 서버 타임라인이 0이 아니면 ⚠로 표면화한다.
            "n_pending_unresolved": len(trader.pending),
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
        save_json(PENDING_PATH, payload)
        log.warning("settlement 동기화 실패 — 보류 큐 저장: %s", e)

    return payload
