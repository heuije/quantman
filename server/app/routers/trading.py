"""자동매매 타임라인 — 사용자 투명성 패널.

[now-24h, now+24h] 윈도우 안의 자동매매 이벤트 3종(KRX cycle·US cycle·preview
결정)을 한눈에. 새 수집 없음 — 모두 기존 데이터(sync_snapshot, UserSettings,
market_calendar)에서 합성.

이벤트 status:
  done       — 정상 완료 (sync_snapshot.received_at 또는 preview.generated_at으로 추정)
  warning    — 실행은 됐으나 체결 미확인 잔존 (N2 — 발주-but-미기록을 ✓로 가장 금지)
  scheduled  — 미래 예정
  missed     — 예정 시각 지났는데 완료 흔적 없음 (PC 꺼짐·grace 초과)
  holiday    — 시장 휴장 (캘린더 기준)

heartbeat status:
  normal  — last_heartbeat < 5min
  warning — 5min ~ 1h
  error   — > 1h 또는 없음
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from quant_core import instrument_region
from quant_core import market_calendar as mc

from ..db import get_session
from ..deps import get_current_user
from ..models import HeartbeatEvent, SyncSnapshot, User, UserSettings

_log = logging.getLogger("app.trading")

router = APIRouter(prefix="/trading", tags=["trading"])
KST = ZoneInfo("Asia/Seoul")

# 사용자 시야 윈도우 — 과거/미래 24시간씩.
WINDOW_HOURS = 24

# 서버 cron 시각(main.py 와 동일 출처). 두 preview는 시장별 다른 데이터에 기반:
#   · 07:30 KST: 미국 마감(06:00) 직후 yfinance/FRED publish 반영 → 국장 매매 후보
#                ("S&P 500이 X 이상이면 KRX 매수" 같은 전략이 fresh US 종가 사용)
#   · 18:15 KST: KRX 종가(15:30) + NAVER + technical + parquet 영구 저장 직후
#                → 미장 매매 후보 (KRX 종가 기반 전략용. 같은 cron이 webhook 발송)
KRX_PREVIEW_TIME = time(7, 30)
US_PREVIEW_TIME = time(18, 15)

# 로컬앱 scheduler.py 와 동일 출처. cycle = 주문 발주, settlement = 미체결 정리·reconcile.
# close = 당일매매(hold_days=0) 종가 청산 사이클(주식 15:25·선물 15:40·미장 close−5분).
KRX_CYCLE_TIME = time(8, 55)
KRX_CLOSE_STOCK_TIME = time(15, 25)
KRX_CLOSE_FUTURES_TIME = time(15, 40)
# θ: 정산은 모든 KRX 종가창(선물 단일가 ~15:45) 이후 — 로컬 cron 15:50과 정합.
KRX_SETTLEMENT_TIME = time(15, 50)
# US cycle·close·settlement는 캘린더 기반 동적 — 함수 내에서 계산.

# 이벤트 status 'warning' 대상 — 장 마감 경계 이벤트는 미체결 잔존이 비정상이라
# n_pending_unresolved>0이면 녹색 성공 대신 ⚠로 표면화한다(N2 — 미장 GOOG 261주가
# 발주-but-미기록인데 "✓ 0건"으로 보이던 거짓 녹색). 아침/개장 cycle은 DAY 지정가가
# 장중 자연 체결될 수 있어 잔존이 정상 → 경고하지 않는다.
WARN_ON_UNRESOLVED_KINDS = frozenset({
    "krx_close_stock", "krx_close_futures", "krx_settlement",
    "us_close", "us_settlement",
})


def _now_kst() -> datetime:
    return datetime.now(KST)


def _heartbeat_status(last_hb: Optional[datetime], now: datetime) -> str:
    if last_hb is None:
        return "error"
    age = now - last_hb
    if age < timedelta(minutes=5):
        return "normal"
    if age < timedelta(hours=1):
        return "warning"
    return "error"


def _build_event(at: datetime, kind: str, status: str,
                  summary: str = "", detail: str = "") -> dict:
    return {
        "at": at.isoformat(),
        "kind": kind,
        "status": status,
        "summary": summary,
        "detail": detail,
    }


class SnapLite(NamedTuple):
    """스냅샷의 timeline 소비 표면만 담는 경량 행.

    consumers(_classify_missed·_match_snapshot·_summarize_cycle·_krx/_us_events)는
    received_at·payload 두 속성만 읽으므로, full SyncSnapshot 대신 cycle_summary만
    담은 이 경량 행으로 충분하다.
    """
    received_at: datetime
    payload: dict


def _snapshots_in_window(session: Session, user_id: int,
                          start: datetime, end: datetime) -> list["SnapLite"]:
    """기간 안의 push 스냅샷(cycle_summary만). cycle 완료 추정에 쓴다.

    Neon egress 인시던트 driver D1 — 과거 여기서 full payload(잔고·체결·자산곡선
    수십~수백KB JSON 컬럼)를 통째로 SELECT한 뒤 cycle_summary만 사용했다. /trading·
    /sync timeline이 둘 다 60s 폴링이라 egress가 폭증, 월 쿼터를 소진시킨 주범.
    cycle_summary 필드만 DB-level projection(SQLite=json_extract / Postgres=->)으로
    읽어 payload 본문을 서버로 전송하지 않는다. 누락 키는 None → consumers가 모두
    `(payload or {}).get("cycle_summary") or {}`로 방어하므로 안전.
    근거: docs/incidents/2026-06-10-neon-data-transfer-quota.md
    """
    rows = session.exec(
        select(SyncSnapshot.received_at, SyncSnapshot.payload["cycle_summary"])
        .where(SyncSnapshot.user_id == user_id)
        .where(SyncSnapshot.received_at >= start)
        .where(SyncSnapshot.received_at <= end)
        .order_by(SyncSnapshot.received_at)
    ).all()
    return [SnapLite(received_at=r[0], payload={"cycle_summary": r[1]}) for r in rows]


def _latest_preview_in_window(session: Session, user_id: int,
                               start: datetime, end: datetime) -> dict | None:
    """기간 안에서 generated_at이 채워진 가장 최신 next_day_preview 1개.

    D1 동일 — preview만 쓰는데 full payload를 읽지 않도록 next_day_preview 필드만
    projection. received_at DESC로 최신부터 훑어, generated_at이 truthy인 첫 dict를
    반환(과거 `for s in reversed(snaps): pv=...; if pv.get("generated_at"): break`와
    동일 의미). 없으면 None.
    """
    rows = session.exec(
        select(SyncSnapshot.payload["next_day_preview"])
        .where(SyncSnapshot.user_id == user_id)
        .where(SyncSnapshot.received_at >= start)
        .where(SyncSnapshot.received_at <= end)
        .where(SyncSnapshot.payload["next_day_preview"].isnot(None))
        .order_by(SyncSnapshot.received_at.desc())
    ).all()
    for pv in rows:
        if isinstance(pv, dict) and pv.get("generated_at"):
            return pv
    return None


def _heartbeats_in_window(session: Session, user_id: int,
                           start: datetime, end: datetime) -> list[datetime]:
    """기간 안의 heartbeat ping 시각 list (KST). missed 원인 A vs B 판정에 사용."""
    rows = session.exec(
        select(HeartbeatEvent.at)
        .where(HeartbeatEvent.user_id == user_id)
        .where(HeartbeatEvent.at >= start)
        .where(HeartbeatEvent.at <= end)
        .order_by(HeartbeatEvent.at)
    ).all()
    out: list[datetime] = []
    for ts in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(ts.astimezone(KST))
    return out


# 누락된 cycle/settlement이 다음 자동매매 작업에 미치는 영향. detail 호버에 노출.
DOWNSTREAM_IMPACT: dict[str, str] = {
    "krx_preview":
        "→ 오늘 08:55 국장 자동매매가 어제 18:15 stale preview로 동작 — "
        "US 종가가 반영되지 않은 후보 사용 (S&P 등 미국 지표 조건이 어제 기준이라 부정확).",
    "krx_cycle":
        "→ 오늘 국장 신규 매수 0건, 보유 KRX 종목 청산 평가 누락. "
        "다음 cycle은 내일 08:55 KST.",
    "krx_close_stock":
        "→ 국장 주식 당일매매 보유가 종가에 청산되지 않음 — "
        "다음 평가는 내일 08:55 사이클.",
    "krx_close_futures":
        "→ 국장 선물 당일매매 보유가 종가에 청산되지 않음(오버나이트 위험 노출) — "
        "15:50 정산·다음 사이클에서 상태 확인 필요.",
    "krx_settlement":
        "→ 오늘 국장 미체결 정리·KIS↔ledger reconcile 누락. "
        "다음 KRX cycle 시작 시 _resolve_pending이 자동으로 처리 — 큰 지장 없음.",
    "us_close":
        "→ 미장 당일매매 보유가 종가 전에 청산되지 않음 — "
        "다음 평가는 다음 미장 사이클.",
    "us_preview":
        "→ 오늘 미장 자동매매(개장 20분 전 시작)가 stale preview로 동작 — "
        "오늘 KRX 종가·NAVER·technical 반영 안 된 후보 사용.",
    "us_cycle":
        "→ 오늘 미장 신규 매수 0건, 보유 US 종목 청산 평가 누락. "
        "다음 미장 cycle은 캘린더상 다음 거래일 개장 20분 전.",
    "us_settlement":
        "→ 미장 미체결 정리 누락. 다음 미장 cycle 시작 시 자동 reconcile — 큰 지장 없음.",
}


def _classify_missed(sched: datetime, kind: str,
                      heartbeats: list[datetime],
                      snaps: list["SnapLite"],
                      market: Optional[str]) -> tuple[str, str]:
    """missed 이벤트의 원인을 A/B/C/D로 분류해 (원인 카테고리, detail 문자열) 반환.

    A — 앱 OFF: 예정 시각 ±15min 안에 heartbeat 없음
    B — cycle 미발동: heartbeat 있음, snapshot 없음 (cron 미등록·grace 초과 가능성)
    C — cycle 실패: snapshot 존재하지만 cycle_summary.error 마커 있음
    D — push 실패: 서버 측에선 사실상 A·B와 구분 불가, 일단 A·B로 흡수
    """
    lo = sched - timedelta(minutes=15)
    hi = sched + timedelta(minutes=30)
    had_heartbeat = any(lo <= hb <= hi for hb in heartbeats)

    # C 검사 — 같은 시장의 에러 마커 있는 snapshot
    for s in snaps:
        ra = s.received_at
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=timezone.utc)
        ra_kst = ra.astimezone(KST)
        if not (lo <= ra_kst <= hi):
            continue
        cs = (s.payload or {}).get("cycle_summary") or {}
        if market and cs.get("market") and cs["market"] != market:
            continue
        if cs.get("error"):
            err_msg = str(cs.get("error"))[:120]
            return ("C", f"cycle은 실행됐으나 실패함 — {err_msg}")

    impact = DOWNSTREAM_IMPACT.get(kind, "")
    if had_heartbeat:
        cause = ("B",
                 "로컬앱은 alive였으나 cycle이 발동되지 않았습니다 "
                 "(cron 미등록·grace 초과·KIS 점검 시간대 가능성).")
    else:
        cause = ("A",
                 "예정 시각에 로컬앱이 실행 중이 아니었습니다 "
                 "(PC 종료·앱 종료·네트워크 단절).")
    return (cause[0], cause[1] + ("  " + impact if impact else ""))


def _match_snapshot(snaps: list["SnapLite"], scheduled: datetime,
                     market: str, kinds: tuple[str, ...],
                     instrument_class: Optional[str] = None) -> Optional["SnapLite"]:
    """scheduled 직후 push된, kind가 맞는 첫 스냅샷이 그 슬롯의 결과로 본다.

    오차 허용: scheduled-2min ~ scheduled+30min. KRX 08:55 cycle은 보통
    08:55:00~08:56:00 사이에 push됨. US는 야간이라 마진 더 줘도 안전.

    kinds(필수) — cycle_summary.kind가 이 중 하나여야 매칭(N2). kind 무관 매칭은
    state_sync·비상청산 push가 cycle 슬롯을 "완료"로 오표시하고, 15:35~16:05
    윈도우가 겹치는 정산/종가청산 push를 서로 바꿔 매칭하는 거짓 녹색의 원인.
    instrument_class — day_trade_close 슬롯의 주식(15:25)/선물(15:40) 분리.
    구버전 로컬앱 push엔 이 필드가 없어 윈도우 시각으로만 분리(absent=수용).
    market 'ALL'도 수용 — 구버전 day_trade_close가 'ALL' 하드코딩(롤링 업그레이드
    호환; kind+윈도우 필터가 오매칭을 막는다).

    v0.9.13 G-8 — 2차 fallback: 같은 거래일·같은 market·같은 kind의 늦은 실행
    (catch-up·지연 정산)도 정시 슬롯의 결과로 인정. 사용자 PC가 08:55 cron 시점
    OFF였다가 09:38 부팅 시 catch-up이 같은 trading day 분량을 실행했다면, UI에
    "누락" 표시는 거짓 음성. 자금 안전 영향 0 (UI 표시만).
    """
    def _accept(cs: dict) -> bool:
        if cs.get("kind") not in kinds:
            return False
        snap_market = cs.get("market")
        if snap_market and snap_market not in (market, "ALL"):
            return False
        if instrument_class is not None:
            ic = cs.get("instrument_class")
            if ic is not None and ic != instrument_class:
                return False
        return True

    sched_date = scheduled.date()
    lo = scheduled - timedelta(minutes=2)
    hi = scheduled + timedelta(minutes=30)
    # 1차: ±30min 윈도우 — 정시 push 매칭
    for s in snaps:
        # received_at은 tz-naive로 저장될 수 있어 KST로 정규화.
        ra = s.received_at
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=timezone.utc).astimezone(KST)
        else:
            ra = ra.astimezone(KST)
        if not (lo <= ra <= hi):
            continue
        cs = (s.payload or {}).get("cycle_summary") or {}
        if not _accept(cs):
            continue
        return s
    # 2차 (G-8): 같은 거래일의 늦은 실행(catch-up·지연 정산)도 정시 결과로 인정
    for s in snaps:
        ra = s.received_at
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=timezone.utc).astimezone(KST)
        else:
            ra = ra.astimezone(KST)
        cs = (s.payload or {}).get("cycle_summary") or {}
        if not _accept(cs):
            continue
        # cycle_summary.today가 scheduled 거래일과 일치해야 — catch-up이 어느
        # trading day 분량을 cover하는지 명시적 매칭. today 누락 entry는 무시.
        cs_today = cs.get("today")
        if cs_today != sched_date.isoformat():
            continue
        # received_at도 같은 KST 거래일 안에 있어야 — 5/29 부팅으로 5/28 cycle을
        # 늦게 catch-up한 케이스를 5/28 timeline에 끼지 않도록.
        if ra.date() != sched_date:
            continue
        return s
    return None


def _summarize_cycle(snap: "SnapLite") -> str:
    cs = (snap.payload or {}).get("cycle_summary") or {}
    bought = cs.get("n_bought", 0) or 0
    sold = cs.get("n_sold", 0) or 0
    if bought == 0 and sold == 0:
        return "0건"
    parts = []
    if bought:
        parts.append(f"{bought}건 매수")
    if sold:
        parts.append(f"{sold}건 매도")
    return " · ".join(parts)


def _unresolved_count(snap: Optional["SnapLite"]) -> int:
    """매칭된 push의 미확인 잔존 건수(N2) — 구버전 push(필드 부재)는 0."""
    if snap is None:
        return 0
    cs = (snap.payload or {}).get("cycle_summary") or {}
    try:
        return int(cs.get("n_pending_unresolved") or 0)
    except (TypeError, ValueError):
        return 0


def _emit_scheduled(events: list[dict], sched: datetime, kind: str,
                     now: datetime, window_start: datetime, window_end: datetime,
                     done_summary: str = "",
                     missed_detail: str = "",
                     holiday: tuple[str, str] | None = None,
                     warn_count: int = 0) -> None:
    """공통 emitter — sched가 윈도우 안이면 status에 맞춰 event 1개 push.

    holiday=(summary, detail) 지정 시 즉시 holiday event.
    호출자가 done 판정 시(snapshot 매칭 등) done_summary를 채워 부르면 done,
    빈 done_summary면 missed로 분류 — missed_detail은 _classify_missed가 채운 결과.
    warn_count>0이면 done 대신 warning(N2) — 발주-but-체결미확인을 ✓로 가장하지
    않는다(장 마감 경계 이벤트 한정 — 호출자가 WARN_ON_UNRESOLVED_KINDS로 게이트).
    """
    if not (window_start <= sched <= window_end):
        return
    if holiday is not None:
        events.append(_build_event(sched, kind, "holiday", *holiday))
        return
    if sched > now:
        events.append(_build_event(sched, kind, "scheduled"))
        return
    if done_summary:
        if warn_count > 0:
            events.append(_build_event(
                sched, kind, "warning",
                f"{done_summary} · 미체결 추적 {warn_count}건",
                "발주됐지만 체결이 확인되지 않은 주문이 남아 있습니다. "
                "정산·다음 사이클이 자동 재확인하며, 지속되면 수동 확인이 필요합니다."))
        else:
            events.append(_build_event(sched, kind, "done", done_summary))
    else:
        events.append(_build_event(sched, kind, "missed", "", missed_detail))


def _krx_events(snaps: list["SnapLite"], heartbeats: list[datetime], now: datetime,
                 window_start: datetime, window_end: datetime) -> list[dict]:
    """KRX 사이클(08:55) + 종가청산(주식 15:25·선물 15:40) + 정산(15:50) 평일 occurrences.

    종가청산 슬롯(N2): 당일매매 자금이 움직이는 마일스톤인데 타임라인에 없어
    "선물 종가청산이 됐는지"를 로그를 파야 알 수 있었다(2026-06-12). kind·
    instrument_class 매칭으로 정산/종가청산 push의 교차 오매칭을 막는다.
    """
    events: list[dict] = []
    d = window_start.date()
    end_d = window_end.date()
    while d <= end_d:
        is_weekend = d.weekday() >= 5
        is_holiday = not is_weekend and not _is_session_day_safe("KR", d)
        for sched_time, kind, kinds, iclass in [
            (KRX_CYCLE_TIME, "krx_cycle", ("cycle", "catchup_cycle"), None),
            (KRX_CLOSE_STOCK_TIME, "krx_close_stock", ("day_trade_close",), "stock"),
            (KRX_CLOSE_FUTURES_TIME, "krx_close_futures", ("day_trade_close",), "futures"),
            (KRX_SETTLEMENT_TIME, "krx_settlement", ("post_close_settlement",), None),
        ]:
            sched = datetime.combine(d, sched_time, tzinfo=KST)
            if is_weekend:
                _emit_scheduled(events, sched, kind, now, window_start, window_end,
                                 holiday=("주말", "토·일 거래 없음"))
            elif is_holiday:
                _emit_scheduled(events, sched, kind, now, window_start, window_end,
                                 holiday=("휴장", "KRX 휴장일"))
            else:
                snap = (_match_snapshot(snaps, sched, "KRX", kinds, iclass)
                        if sched <= now else None)
                summary = ""
                if snap:
                    summary = ("정산 완료" if kind == "krx_settlement"
                               else _summarize_cycle(snap))
                missed_detail = ""
                if not summary and sched <= now:
                    _, missed_detail = _classify_missed(sched, kind, heartbeats, snaps, "KRX")
                warn = (_unresolved_count(snap)
                        if kind in WARN_ON_UNRESOLVED_KINDS else 0)
                _emit_scheduled(events, sched, kind, now, window_start, window_end,
                                 done_summary=summary, missed_detail=missed_detail,
                                 warn_count=warn)
        d += timedelta(days=1)
    return events


def _us_events(snaps: list["SnapLite"], heartbeats: list[datetime], now: datetime,
                window_start: datetime, window_end: datetime) -> list[dict]:
    """US 사이클(개장−20분) + 종가청산(close−5min) + 정산(close+5min) — 캘린더 기반 동적."""
    events: list[dict] = []
    cursor = window_start - timedelta(hours=2)  # 직전 세션이 윈도우 걸칠 수 있어 마진
    for _ in range(5):
        try:
            sess = mc.next_session_kst("US", cursor)
        except mc.CalendarError:
            break
        if sess is None:
            break
        open_kst, close_kst = sess
        if open_kst > window_end + timedelta(hours=1):
            break

        # 로컬 scheduler.py의 us_cycle(개장−20분, 예약 접수창 마감 전 발주 완료용)과
        # 반드시 정합 — 한쪽만 바꾸면 timeline이 실제 실행 시각을 거짓 표시하고
        # _match_snapshot 윈도우가 어긋나 성공 사이클을 missed로 오분류한다
        # (2026-06-10 "UI 22:25 vs 실행 22:10" 사고, 리뷰 D6-1).
        cycle_sched = open_kst - timedelta(minutes=20)
        close_sched = close_kst - timedelta(minutes=5)   # us_close_cycle과 정합
        settle_sched = close_kst + timedelta(minutes=5)
        cycle_snap = (_match_snapshot(snaps, cycle_sched, "US",
                                      ("cycle", "catchup_cycle"))
                      if cycle_sched <= now else None)
        close_snap = (_match_snapshot(snaps, close_sched, "US",
                                      ("day_trade_close",), "stock")
                      if close_sched <= now else None)
        settle_snap = (_match_snapshot(snaps, settle_sched, "US",
                                       ("post_close_settlement",))
                       if settle_sched <= now else None)

        cycle_missed_detail = ""
        if not cycle_snap and cycle_sched <= now:
            _, cycle_missed_detail = _classify_missed(cycle_sched, "us_cycle",
                                                       heartbeats, snaps, "US")
        close_missed_detail = ""
        if not close_snap and close_sched <= now:
            _, close_missed_detail = _classify_missed(close_sched, "us_close",
                                                       heartbeats, snaps, "US")
        settle_missed_detail = ""
        if not settle_snap and settle_sched <= now:
            _, settle_missed_detail = _classify_missed(settle_sched, "us_settlement",
                                                        heartbeats, snaps, "US")

        _emit_scheduled(events, cycle_sched, "us_cycle", now, window_start, window_end,
                         done_summary=_summarize_cycle(cycle_snap) if cycle_snap else "",
                         missed_detail=cycle_missed_detail)
        _emit_scheduled(events, close_sched, "us_close", now, window_start, window_end,
                         done_summary=_summarize_cycle(close_snap) if close_snap else "",
                         missed_detail=close_missed_detail,
                         warn_count=_unresolved_count(close_snap))
        _emit_scheduled(events, settle_sched, "us_settlement", now, window_start, window_end,
                         done_summary="정산 완료" if settle_snap else "",
                         missed_detail=settle_missed_detail,
                         warn_count=_unresolved_count(settle_snap))
        cursor = open_kst
    return events


def _preview_events(preview: dict | None, now: datetime,
                     window_start: datetime, window_end: datetime) -> list[dict]:
    """매매 후보 결정 — 시장별 분리. 서버 cron이므로 heartbeat·로컬앱과 무관.

    · krx_preview (07:30 KST): yfinance/FRED publish 직후 → 국장 cycle 8:55용
    · us_preview  (18:15 KST): KRX 종가·NAVER·technical 완성 직후 → 미장 cycle용

    preview = 윈도우 내 최신 next_day_preview 1개(_latest_preview_in_window). D1
    egress 절감으로 전체 snaps를 받지 않고 단일 preview만 받는다.
    """
    events: list[dict] = []
    latest_gen: Optional[datetime] = None
    krx_n = us_n = 0
    pv = preview or {}
    gen = pv.get("generated_at")
    if gen:
        try:
            latest_gen = datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            latest_gen = None
        if latest_gen is not None:
            for bs in pv.get("by_strategy") or []:
                for c in bs.get("candidates") or []:
                    sym = c.get("symbol", "")
                    # 세션 그룹으로 분리(국장/미장). instrument_region이 SSOT —
                    # sym.isdigit()는 한글 선물 심볼('코스피200선물')을 미장으로 오집계했다
                    # (국내선물은 KRX 08:55 사이클에 진입하므로 국장 후보).
                    if instrument_region(sym) == "KRX":
                        krx_n += 1
                    else:
                        us_n += 1

    d = window_start.date()
    end_d = window_end.date()
    while d <= end_d:
        for sched_time, kind, market_n in [
            (KRX_PREVIEW_TIME, "krx_preview", krx_n),
            (US_PREVIEW_TIME, "us_preview", us_n),
        ]:
            sched = datetime.combine(d, sched_time, tzinfo=KST)
            if not (window_start <= sched <= window_end):
                continue
            if sched > now:
                events.append(_build_event(sched, kind, "scheduled"))
            elif latest_gen and latest_gen >= sched - timedelta(minutes=2):
                events.append(_build_event(
                    sched, kind, "done",
                    f"{market_n}건" if market_n else "후보 0건"))
            else:
                # 서버 cron 실패 — 운영 사이드 문제. 로컬앱 무관.
                impact = DOWNSTREAM_IMPACT.get(kind, "")
                events.append(_build_event(
                    sched, kind, "missed", "",
                    "서버 preview 갱신 cron 결과를 받지 못했습니다.  " + impact))
        d += timedelta(days=1)
    return events


def _is_session_day_safe(market: str, d: date) -> bool:
    """캘린더 만료·예외 시 True로 fallback — 사이클 차단 방지(AL-3 동일 정책)."""
    try:
        return mc.is_session_day(market, d)
    except Exception:
        return True


def _timeline_etag(latest_received: Optional[datetime],
                   last_hb: Optional[datetime], now: datetime) -> str:
    """timeline 응답의 약한 ETag — `W/"<data_ms>-<bucket>"`.

    data_ms = (최신 snapshot.received_at, last_heartbeat_at) 중 max의 epoch ms.
      → snapshot push·heartbeat 갱신마다 자동으로 새 tag.
    bucket = now.timestamp() // 300 (5분 버킷).
      → timeline 내용은 `now`에도 의존한다(scheduled→missed 전이, heartbeat_status
        escalation). 버킷이 없으면 로컬앱이 죽어 heartbeat·snapshot이 끊긴 동안
        tag가 영원히 고정돼 timeline이 stale 상태에 박제된다. 300s는 heartbeat
        주기·5분 상태 임계와 일치 → stale은 최대 5분으로 bounded.
    """
    parts: list[float] = []
    for ts in (latest_received, last_hb):
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parts.append(ts.timestamp())
    data_ms = int(max(parts) * 1000) if parts else 0
    bucket = int(now.timestamp() // 300)
    return f'W/"{data_ms}-{bucket}"'


@router.get("/timeline")
def get_timeline(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """[now-24h, now+24h] 윈도우 자동매매 이벤트 + heartbeat 상태.

    tag-first ETag(인시던트 driver D2) — 윈도우 조회 전에 scalar 2개(최신
    received_at·last_heartbeat_at)로 ETag를 계산해, If-None-Match 매칭 시 무거운
    window 조회·이벤트 합성을 모두 건너뛰고 304만 반환한다. 웹 클라이언트는 60s
    폴링하지만 변경이 없으면 304로 끝나 Neon egress가 사실상 0이 된다(web/src/api.ts
    etagCache가 이미 If-None-Match 송신·304 캐시 처리 → 웹 API 변경 불요).
    근거: docs/incidents/2026-06-10-neon-data-transfer-quota.md
    """
    now = _now_kst()

    settings = session.exec(
        select(UserSettings).where(UserSettings.user_id == user.id)
    ).first()
    last_hb = settings.last_heartbeat_at if settings else None
    if last_hb and last_hb.tzinfo is None:
        last_hb = last_hb.replace(tzinfo=timezone.utc)

    latest_received = session.exec(
        select(SyncSnapshot.received_at)
        .where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()

    etag = _timeline_etag(latest_received, last_hb, now)
    if request.headers.get("if-none-match") == etag:
        # window 조회 0회 — egress 절감의 핵심 경로.
        return Response(status_code=304)
    response.headers["ETag"] = etag

    window_start = now - timedelta(hours=WINDOW_HOURS)
    window_end = now + timedelta(hours=WINDOW_HOURS)

    snaps = _snapshots_in_window(session, user.id, window_start, window_end)
    heartbeats = _heartbeats_in_window(session, user.id, window_start, window_end)
    preview = _latest_preview_in_window(session, user.id, window_start, window_end)

    events = (
        _krx_events(snaps, heartbeats, now, window_start, window_end)
        + _us_events(snaps, heartbeats, now, window_start, window_end)
        + _preview_events(preview, now, window_start, window_end)
    )
    events.sort(key=lambda e: e["at"])

    return {
        "now": now.isoformat(),
        "heartbeat_at": last_hb.isoformat() if last_hb else None,
        "heartbeat_status": _heartbeat_status(last_hb, now),
        "events": events,
    }
