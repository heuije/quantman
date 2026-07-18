"""로컬앱 스케줄러 (KST).

국내(KRX) — 고정 cron:
- 평일 08:50  장중 loop 시작 (시세+체결통보 WebSocket)
- 평일 08:55  메인 사이클 (KRX 매수/청산 평가 + 발주)
- 평일 15:25  종가 매도 사이클 (주식·당일매매 hold_days=0)
- 평일 15:30  장중 loop 종료
- 평일 15:40  종가 매도 사이클 (선물·당일매매 hold_days=0)
- 평일 15:50  장 마감 후 settlement (θ: 모든 종가창 이후 — 선물 단일가 15:45 커버)

미국(US) — 동적 야간 플래너:
- 매일 12:00  오늘 밤 미국 세션을 시장 캘린더로 계산해 one-shot 잡 등록
  · open−20분 미국 예약발주 사이클 (run_cycle market="US", reserved=True)
  · open−10분 미국 장중 손절 loop 시작
  · open+5분  개장 후 예약체결 reconcile
  · close−5분 종가 매도 사이클 (주식·당일매매 hold_days=0, 라이브 지정가)
  · close+5분 미국 장 마감 후 settlement
- 미국 개장은 DST로 22:30↔23:30 KST 이동 + 휴장일이 한국과 달라 고정 cron이
  불가능하므로, 매일 캘린더에 물어 그날치 잡을 등록한다(휴장이면 무동작).

08:50 → 08:55의 5분 갭은 WebSocket 연결·AES key/iv 수신 안정화 마진.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from quant_core import market_calendar as mc

from . import intraday_loop
from .runner import (run_close_cycle, run_cycle, run_post_close_settlement,
                     run_post_open_reconcile)

log = logging.getLogger("localapp.scheduler")
KST = ZoneInfo("Asia/Seoul")


def _plan_us_session(sched: BlockingScheduler, now: datetime | None = None) -> None:
    """오늘 밤 미국 세션을 계산해 one-shot 잡(사이클·정산)을 등록.

    매일 정오 cron + 기동 시 1회 호출. 동일 id로 replace_existing 하므로 중복
    호출이 안전하다. 휴장이면 무동작. 캘린더 데이터 만료 시 명시적 에러 로그.
    now: 테스트용 주입(기본 현재 KST).
    """
    now = now or datetime.now(KST)
    try:
        sess = mc.next_session_kst("US", now)
    except mc.CalendarError as e:
        log.error("미국 세션 스케줄 실패 — 캘린더 데이터 갱신 필요: %s", e)
        return
    if sess is None:
        log.info("예정된 미국 세션 없음")
        return

    open_kst, close_kst = sess
    # 오늘 밤(약 20시간 이내) 열리는 세션만 지금 스케줄 — 그 이후는 다음 정오 plan이 처리
    if open_kst - now > timedelta(hours=20):
        log.info("다음 미국 세션 %s — 아직 멀어 보류 (다음 정오 재계산)",
                 open_kst.strftime("%m-%d %H:%M"))
        return

    # 예약주문 발주는 개장 전 접수창(개장−10분 마감) 안에 끝나야 한다. dataset
    # 다운로드(~130초)+평가 여유로 개장−20분에 사이클 시작 → 개장−10분 전 발주 완료.
    cycle_at = open_kst - timedelta(minutes=20)
    loop_start_at = open_kst - timedelta(minutes=10)
    reconcile_at = open_kst + timedelta(minutes=5)
    # 종가 청산(당일매매 hold_days=0) — 폐장 5분 전 라이브 지정가 매도(시장가 근사).
    # 미국은 MOC가 모의 미지원이라 연속장 막판 지정가가 최선의 모의=실전 종가 근사.
    close_cycle_at = close_kst - timedelta(minutes=5)
    settle_at = close_kst + timedelta(minutes=5)

    # 예약주문 발주 사이클 — 개장 전. reserved=True로 매수·매도 모두 지정가 예약(00).
    sched.add_job(
        run_cycle, DateTrigger(run_date=cycle_at),
        kwargs={"market": "US", "reserved": True}, id="us_cycle",
        name="미국 자동매매 사이클(예약발주)",
        replace_existing=True, misfire_grace_time=600)
    # 손절 loop을 개장 전 시작 — 개장 체결통보 수신 마진 (KRX와 동일 패턴)
    sched.add_job(
        intraday_loop.start, DateTrigger(run_date=loop_start_at),
        kwargs={"market": "US"}, id="us_loop_start", name="미국 장중 손절 loop 시작",
        replace_existing=True, misfire_grace_time=600)
    # 개장 직후 예약주문 체결 reconcile — 체결통보 WS 미수신분도 ledger·서버 반영.
    sched.add_job(
        run_post_open_reconcile, DateTrigger(run_date=reconcile_at),
        kwargs={"market": "US"}, id="us_post_open_reconcile",
        name="미국 개장 후 예약체결 reconcile",
        replace_existing=True, misfire_grace_time=900)
    # 종가 청산 사이클 — 폐장 5분 전. 라이브 지정가 매도(_reserved_us=False 신규 Trader)로
    # 당일매매 보유분을 종가 무렵 청산 → backtest 종가청산과 정렬(주식 종가창과 동일 패턴).
    sched.add_job(
        run_close_cycle, DateTrigger(run_date=close_cycle_at),
        kwargs={"market": "US", "instrument_class": "stock"},
        id="us_close_cycle", name="미국 종가 매도 사이클 (주식·당일매매)",
        replace_existing=True, misfire_grace_time=300)
    # US-F4: 해외선물(CME) 당일매매 종가청산 — 종전엔 주식만 청산돼 선물 day-trade가
    # 오버나이트 방치됐다(미배선). **폐장−20분**에 둔다(주식 종가청산 폐장−5분과 15분 분리):
    #   ① CME 일일정산이 미국주식 폐장보다 이르다(예: 원유 14:30 ET vs 주식 16:00 ET)
    #      → 폐장−20분이 정산 시각에 더 근접한 종가청산 프록시.
    #   ② 주식 종가청산과 **시각 분리**해 두 ephemeral close-cycle Trader의 동시 실행을 회피한다.
    #      같은 시각이면 각자 ledger를 통째 로드·저장해 한쪽 매도 제거가 다른 쪽에 덮여 포지션이
    #      부활하는 lost-update(reload_state docstring의 라이브 실증 부류)가 난다 — KRX가
    #      stock 15:25·futures 15:40으로 15분 분리한 것과 동형(선물이 먼저 끝나 stock이 신선 로드).
    # 상품별 CME 정산시각 정밀정렬은 라이브 정밀화 대상.
    close_cycle_futures_at = close_kst - timedelta(minutes=20)
    sched.add_job(
        run_close_cycle, DateTrigger(run_date=close_cycle_futures_at),
        kwargs={"market": "US", "instrument_class": "futures"},
        id="us_close_cycle_futures", name="미국 종가 매도 사이클 (해외선물·당일매매)",
        replace_existing=True, misfire_grace_time=300)
    sched.add_job(
        intraday_loop.stop, DateTrigger(run_date=close_kst),
        id="us_loop_stop", name="미국 장중 손절 loop 종료",
        replace_existing=True, misfire_grace_time=600)
    sched.add_job(
        run_post_close_settlement, DateTrigger(run_date=settle_at),
        kwargs={"market": "US"}, id="us_settlement", name="미국 장마감 정산",
        replace_existing=True, misfire_grace_time=1800)

    log.info("미국 세션 스케줄 — 사이클(예약) %s · loop %s · 개장후reconcile %s · "
             "종가청산 %s · loop종료 %s · 정산 %s (KST)",
             cycle_at.strftime("%H:%M"), loop_start_at.strftime("%H:%M"),
             reconcile_at.strftime("%H:%M"), close_cycle_at.strftime("%H:%M"),
             close_kst.strftime("%m-%d %H:%M"), settle_at.strftime("%H:%M"))


def register_jobs(sched) -> None:
    """모든 자동매매 cron을 sched에 등록 — BlockingScheduler / BackgroundScheduler 공용.

    Phase 60+ — 이전엔 BlockingScheduler 가정으로 start()에 인라인됐다. GUI 모드
    (BackgroundScheduler)에서 동일 cron이 필요해 helper로 추출. cron 정의는 단일
    출처(여기) — gui._start_scheduler·headless start() 모두 이걸 호출.

    등록 항목:
      · KRX 08:50 loop / 08:55 cycle / 15:25 종가매도(주식) / 15:30 loop stop /
        15:40 종가매도(선물) / 15:50 settlement
      · US 매일 12:00 야간 플래너 + 기동 시 즉시 1회 plan
      · 캘린더 04:00 일일 sync
      · heartbeat 5분 주기 + 기동 시 1회
      · dataset 08:00·08:20·08:40 시도 + 기동 시 1회
    """
    # APScheduler 이벤트 표면화 — 미스파이어 skip·동시실행 skip·잡 예외가 이전엔
    # apscheduler 로거(핸들러 미부착, windowed 앱이라 stderr도 소실)에만 남아
    # "사이클이 트리거조차 안 됐는데 아무 흔적 없음"의 관측 공백이었다(리뷰 D6-4).
    from apscheduler.events import (EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES,
                                    EVENT_JOB_MISSED)

    def _on_job_event(event):
        if event.code == EVENT_JOB_MISSED:
            log.warning("[sched] job 미스파이어 skip: %s (예정 %s)",
                        event.job_id, getattr(event, "scheduled_run_time", "?"))
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            log.warning("[sched] job skip — 이전 인스턴스 아직 실행 중: %s",
                        event.job_id)
        else:  # EVENT_JOB_ERROR
            log.error("[sched] job 예외: %s — %s", event.job_id,
                      getattr(event, "exception", "?"))

    sched.add_listener(_on_job_event,
                       EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)

    # ── 국내(KRX) 고정 cron ──────────────────────────────────────────────────
    # 로드맵 E — 장중 감시를 장별 동시호가 시작(08:30)부터로 확장. 종전 08:50은
    # 선물 개장(08:45) 뒤 5분 감시 공백을 만들었다(CY-5). 동시호가 구간은 체결가
    # 이벤트가 없어 손절 평가는 자연 무동작(무해) — WS 연결·체결통보만 선행 준비.
    sched.add_job(
        intraday_loop.start,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone="Asia/Seoul"),
        id="krx_loop_start", name="KRX 장중 loop 시작 (WebSocket·동시호가부터)",
        misfire_grace_time=600)
    # 아침 사이클 자산군 분리(파이프라인 문제 10 — kr-target-reconciliation.md §15 Phase 4):
    # 선물 정규거래 개장은 08:45(개장 동시호가 08:30~08:45)인데 단일 08:55 사이클은
    # 주식 개장(09:00)에 맞춰져 선물 시가진입이 연속가에 밀렸다(07-14 실측 ~30p 불리).
    # · 08:35 선물 전용 — 동시호가 창 내 발주 → 08:45 시가 단일가 체결(파리티).
    # · 08:40·08:42 재실행 = 창내 재시도(문제 12) — 목표수렴 사이클은 멱등(net 재계산 +
    #   intent 저널 게이트)이라 재실행이 안전: 첫 발주가 접수됐으면 무행동, 예외로
    #   미접수(reconcile_submitting이 판정)면 재발주.
    # · 08:55 주식 전용 — 종전 시각 유지(선물은 08:35이 전담).
    for _mm, _jid in [(35, "krx_cycle_futures"), (40, "krx_cycle_futures_r1"),
                      (42, "krx_cycle_futures_r2")]:
        sched.add_job(
            run_cycle, kwargs={"market": "KRX", "instrument_class": "futures"},
            trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=_mm,
                                timezone="Asia/Seoul"),
            id=_jid, name=f"KRX 선물 개장 사이클 (08:{_mm:02d})",
            misfire_grace_time=120)
    sched.add_job(
        run_cycle, kwargs={"market": "KRX", "instrument_class": "stock"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=55, timezone="Asia/Seoul"),
        id="krx_cycle", name="KRX 자동매매 사이클 (주식)", misfire_grace_time=300)
    # §19 개장후 수렴 (방향무관 안전망) — 동시호가 체결(선물 08:45·주식 09:00) 반영 후
    # run_cycle 재실행 = 실체결 기준 멱등 수렴(net=target−broker). A1(08:35/15:40) 넷팅이
    # 못 잡은 취소-race·늦은 수동·반대/초과 방향 수동을 **방향무관 drift**로 당일 교정한다.
    # 로드맵 E — 개장+1분(선물 08:46·주식 09:01)으로 당김(종전 08:52/09:05): 수렴 지연
    # 5~7분 동안 어긋난 상태가 방치되던 것을 축소. loop는 08:30부터라 WS 안정화 선행
    # 조건은 유지. ⚠ 개장 단일가 체결의 잔고 API 반영 지연이 크면 drift 오판 가능 —
    # 멱등(저널 게이트·보유 게이트)이라 발주 안전은 유지되나, 반영 지연 실측을 릴리스
    # 후 관찰 항목으로 둔다(과다 no-op/오경보 시 +1분 조정).
    # 멱등(L-01 재진입 차단·보유 게이트·drift만)이라 교정할 것 없으면 no-op. 마감측은
    # 시장종료(선물 15:45·주식 15:30)로 불가 → 익일 개장 carry(§19.2 원리적 한계).
    sched.add_job(
        run_cycle, kwargs={"market": "KRX", "instrument_class": "futures"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=46, timezone="Asia/Seoul"),
        id="krx_converge_futures", name="KRX 개장후 수렴 (선물·08:46)",
        misfire_grace_time=120)
    sched.add_job(
        run_cycle, kwargs={"market": "KRX", "instrument_class": "stock"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=1, timezone="Asia/Seoul"),
        id="krx_converge_stock", name="KRX 개장후 수렴 (주식·09:01)",
        misfire_grace_time=120)
    # 로드맵 E — loop 종료를 선물 마감(15:45)까지 연장(종전 15:30): 선물 마감측
    # 15분 감시 공백(CY-5) 해소. 주식은 15:30 이후 체결 이벤트가 없어 무해(리뷰 R4-④).
    sched.add_job(
        intraday_loop.stop,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone="Asia/Seoul"),
        id="krx_loop_stop", name="KRX 장중 loop 종료(선물 마감까지)",
        misfire_grace_time=300)
    # θ: 정산은 모든 KRX 종가창이 끝난 뒤 1회 — 옛 15:35는 선물 종가청산(15:40
    # 발주 → 단일가 15:35~15:45 체결)보다 먼저 돌아, 그 체결을 그날 안에 확인할
    # 패스가 없었다(2026-06-12 0000004525 미기록 → 수동 복구). 15:50 = 선물
    # 장종료(15:45) + 체결조회 여유 5분. catchup.py의 정산 임계와 정합 유지.
    sched.add_job(
        run_post_close_settlement, kwargs={"market": "KRX"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=50, timezone="Asia/Seoul"),
        id="krx_settlement", name="KRX 장 마감 후 settlement", misfire_grace_time=600)
    # R4-② — 정산 당일 재시도. 15:50 정산이 blip·misfire로 실패하면 종전엔 재시도
    # 0으로 그날을 넘겼다(당일매매 미청산 감시 I5·체결 최종확정이 하루 지연).
    # 성공 기록이 있으면 no-op(runner.run_settlement_retry가 cycles.jsonl로 판정).
    from .runner import run_settlement_retry
    for _hh, _mm, _jid in [(16, 5, "krx_settlement_retry1"),
                           (16, 30, "krx_settlement_retry2")]:
        sched.add_job(
            run_settlement_retry, kwargs={"market": "KRX"},
            trigger=CronTrigger(day_of_week="mon-fri", hour=_hh, minute=_mm,
                                timezone="Asia/Seoul"),
            id=_jid, name=f"KRX 정산 재시도 ({_hh}:{_mm:02d})",
            misfire_grace_time=300)

    # 종가 매도 사이클(당일매매 hold_days=0) — 주식 종가단일가 15:20~15:30 / 선물 15:35~15:45
    # 구간 내 발주(폐장 5분 전) → 단일가 체결. 진입은 아침 메인 cycle, 청산만 종가창에서 전담.
    # 폐장 5분 전: 사이클 지연(스냅샷·발주)을 흡수할 여유. (선물 종전 15:43 → 15:40, 여유 2분→5분.)
    sched.add_job(
        run_close_cycle, kwargs={"market": "KRX", "instrument_class": "stock"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=25, timezone="Asia/Seoul"),
        id="krx_close_cycle_stock", name="KRX 종가 매도 사이클 (주식·당일매매)",
        misfire_grace_time=300)
    sched.add_job(
        run_close_cycle, kwargs={"market": "KRX", "instrument_class": "futures"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone="Asia/Seoul"),
        id="krx_close_cycle_futures", name="KRX 종가 매도 사이클 (선물·당일매매)",
        misfire_grace_time=300)

    # ── 동시호가 가드(#16 · 2026-07-19) — 창내 수동 개입 실시간 정합 ────────────
    # 자동 발주 직후~단일가 수십 초 전까지 10초 폴링: 자동 주문 유저 변경은 취소+
    # 재발주로 복원, 목표 초과 수동 미체결은 최신 주문부터 취소(상세 auction_guard.py).
    # start_hm = 그 창의 자동 발주 시작 직전(이후 own intent만 이번 창 소속).
    from . import auction_guard
    for _cls, _win, _start, _at, _until, _jid in [
        ("futures", "open", (8, 34), (8, 36), (8, 44, 30), "guard_open_futures"),
        ("stock", "open", (8, 54), (8, 56), (8, 59, 30), "guard_open_stock"),
        ("stock", "close", (15, 24), (15, 26), (15, 29, 30), "guard_close_stock"),
        ("futures", "close", (15, 39), (15, 41), (15, 44, 30), "guard_close_futures"),
    ]:
        sched.add_job(
            auction_guard.run_auction_guard,
            CronTrigger(day_of_week="mon-fri", hour=_at[0], minute=_at[1],
                        timezone="Asia/Seoul"),
            kwargs={"instrument_class": _cls, "window": _win,
                    "start_hm": _start, "until_hms": _until},
            id=_jid, name=f"동시호가 가드 ({_win}·{_cls})", misfire_grace_time=60)

    # ── 미국(US) 동적 야간 플래너 ────────────────────────────────────────────
    sched.add_job(
        _plan_us_session, kwargs={"sched": sched},
        trigger=CronTrigger(hour=12, minute=0, timezone="Asia/Seoul"),
        id="us_planner", name="미국 세션 야간 플래너", misfire_grace_time=3600)
    # 기동 시 1회 — 정오를 지나 시작했어도 오늘 밤 세션을 놓치지 않도록
    _plan_us_session(sched)

    # ── Q2+Q8: 캘린더 일일 sync (04:00 KST — 서버 03:00 cron 이후 안전 마진) ─
    from . import calendar_sync
    sched.add_job(
        calendar_sync.pull_all,
        CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"),
        id="calendar_sync", name="시장 캘린더 일일 sync",
        misfire_grace_time=3600)

    # ── Phase 58 — heartbeat (5분 주기, alive 신호) ─────────────────────────
    # cycle 외 시간(새벽 등)에도 웹앱 "끊김" 표시 회피.
    # KIS API 호출 X — 단순 alive ping. 페어링 안 됐으면 silent fail.
    from . import sync_client
    sched.add_job(
        sync_client.push_heartbeat,
        CronTrigger(minute="*/5", timezone="Asia/Seoul"),
        id="heartbeat", name="로컬앱 alive heartbeat",
        misfire_grace_time=120)
    import threading
    threading.Thread(target=sync_client.push_heartbeat,
                      daemon=True, name="heartbeat-initial").start()

    # ── Phase 58-C — dataset bundle 예열 (로드맵 B 재정렬) ─────────────────
    # 서버 재포장은 07:30(글로벌 아침판)·08:02+재시도(선물 반영판) — 종전
    # 시각(08:00/20/40)의 "07:45 포장" 주석은 구체제 유산이었고, 08:40분은
    # 선물 발주(08:35)보다 늦어 무의미했다. 재정렬: 08:05(선물 반영판 직후)·
    # 08:20(서버 재시도 흡수)·08:32(선물 발주 전 마지노선). 마지막 방어선은
    # 종전과 동일하게 각 회차 시작 시 refresh_market_data(변경 없으면 304).
    # 기동 시 1회 background 실행 — 첫 가동/PC 재부팅 시 즉시 fresh.
    from . import datafetch
    for hm in [(8, 5), (8, 20), (8, 32)]:
        sched.add_job(
            datafetch.refresh_market_data,
            CronTrigger(hour=hm[0], minute=hm[1], timezone="Asia/Seoul"),
            id=f"dataset_sync_{hm[0]:02d}{hm[1]:02d}",
            name=f"dataset bundle sync ({hm[0]:02d}:{hm[1]:02d})",
            misfire_grace_time=1800)
    threading.Thread(target=datafetch.refresh_market_data,
                      daemon=True, name="dataset-initial").start()

    # ── Phase 7 — Catch-up on startup ────────────────────────────────────
    # PC가 꺼져 있어 missed된 cycle/settlement/장중 손절을 기동 시 자동 보완.
    # cycles.jsonl 기반 idempotency — 이미 실행된 cycle은 다시 안 함.
    # background thread (다른 initial들과 동일 패턴) — UI block 방지.
    from . import catchup
    threading.Thread(target=catchup.run_catchup_on_startup,
                      daemon=True, name="catchup-startup").start()

    # R4-③ — 절전(sleep)-wake 감지. APScheduler는 절전을 "시작"으로 보지 않아
    # misfire된 창이 조용히 사라졌다(F-6 실측: wake 후 catchup 미발동 = 유실 복구
    # 없음). 5분 주기로 벽시계 점프(>10분)를 감지하면 startup과 동일한 catchup
    # 스캔을 배경으로 돌린다 — 창내면 재실행·창밖이면 plan이 경보만(멱등:
    # net=target−broker + intent 저널 게이트가 재실행 안전을 보장).
    _wake_state = {"last": datetime.now(KST)}

    def _wake_probe():
        now = datetime.now(KST)
        gap = (now - _wake_state["last"]).total_seconds()
        _wake_state["last"] = now
        if gap > 600:
            log.warning("[wake] 벽시계 점프 %.0f분 감지 — 절전 해제 추정, "
                         "catchup 스캔 실행", gap / 60)
            threading.Thread(target=catchup.run_catchup_on_startup,
                              daemon=True, name="catchup-wake").start()

    sched.add_job(_wake_probe, IntervalTrigger(minutes=5),
                  id="wake_probe", name="절전-wake 감지(5분)",
                  misfire_grace_time=None)

    log.info("scheduler jobs registered — KRX cron + US planner + heartbeat + "
              "dataset + catchup + wake-probe")


def start() -> None:
    """headless 모드 진입점 — `python -m localapp.scheduler` 또는 별도 process."""
    # Q1: 잔고 push 실패분의 백그라운드 retry thread.
    from . import sync_retry
    sync_retry.start()

    # 캘린더 기동 시 1회 sync
    from . import calendar_sync
    import threading
    threading.Thread(target=calendar_sync.pull_all, daemon=True,
                      name="calendar-sync-initial").start()

    sched = BlockingScheduler(timezone="Asia/Seoul")
    register_jobs(sched)

    print("=" * 52)
    print("  로컬앱 스케줄러 시작 (KST)")
    print("  [KRX] 08:50 loop · 08:55 사이클 · 15:25 종가매도(주식) · 15:30 loop종료 · "
          "15:40 종가매도(선물) · 15:50 정산")
    print("  [US ] 매일 12:00 야간 플래너 → open−20분 예약발주 / open+5분 reconcile / "
          "close−5분 종가매도 / close+5분 정산")
    print("        (DST·휴장 자동 반영, 오늘 밤 세션은 기동 시 즉시 등록)")
    print("  [캘린더] 04:00 시장 캘린더 일일 sync (임시공휴일 반영)")
    print("  브로커: KIS (실전/모의투자)")
    print("  Ctrl+C 로 종료")
    print("=" * 52)
    sched.start()
