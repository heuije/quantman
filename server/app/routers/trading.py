"""자동매매 타임라인 — 사용자 투명성 패널.

[now-24h, now+24h] 윈도우 안의 자동매매 이벤트 3종(KRX cycle·US cycle·preview
결정)을 한눈에. 새 수집 없음 — 모두 기존 데이터(sync_snapshot, UserSettings,
market_calendar)에서 합성.

이벤트 status:
  done       — 정상 완료 (sync_snapshot.received_at 또는 preview.generated_at으로 추정)
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
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from quant_core import market_calendar as mc

from ..db import get_session
from ..deps import get_current_user
from ..models import SyncSnapshot, User, UserSettings

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
KRX_CYCLE_TIME = time(8, 55)
KRX_SETTLEMENT_TIME = time(15, 35)
# US cycle·settlement는 캘린더 기반 동적 — 함수 내에서 계산.


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


def _snapshots_in_window(session: Session, user_id: int,
                          start: datetime, end: datetime) -> list[SyncSnapshot]:
    """기간 안의 push 스냅샷. cycle 완료 추정에 쓴다."""
    return list(session.exec(
        select(SyncSnapshot)
        .where(SyncSnapshot.user_id == user_id)
        .where(SyncSnapshot.received_at >= start)
        .where(SyncSnapshot.received_at <= end)
        .order_by(SyncSnapshot.received_at)
    ).all())


def _match_snapshot(snaps: list[SyncSnapshot], scheduled: datetime,
                     market: str) -> Optional[SyncSnapshot]:
    """scheduled 직후 push된 첫 스냅샷이 그 cycle의 결과로 본다.

    오차 허용: scheduled-2min ~ scheduled+30min. KRX 08:55 cycle은 보통
    08:55:00~08:56:00 사이에 push됨. US는 야간이라 마진 더 줘도 안전.
    cycle_summary.market 필드가 있으면 동일 시장만 매칭.
    """
    lo = scheduled - timedelta(minutes=2)
    hi = scheduled + timedelta(minutes=30)
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
        snap_market = cs.get("market")
        if snap_market and snap_market != market:
            continue
        return s
    return None


def _summarize_cycle(snap: SyncSnapshot) -> str:
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


def _emit_scheduled(events: list[dict], sched: datetime, kind: str,
                     now: datetime, window_start: datetime, window_end: datetime,
                     done_summary: str = "", scheduled_or_done: bool = False,
                     missed_detail: str = "", holiday: tuple[str, str] | None = None
                     ) -> None:
    """공통 emitter — sched가 윈도우 안이면 status에 맞춰 event 1개 push.

    holiday=(summary, detail) 지정 시 즉시 holiday event (cycle·settlement에서 쓴다).
    scheduled_or_done=True이면 sched > now → scheduled, 아니면 외부에서 done/missed
    판정해 done_summary를 사전 채워 호출(예: cycle은 _match_snapshot, preview는
    generated_at 비교).
    """
    if not (window_start <= sched <= window_end):
        return
    if holiday is not None:
        events.append(_build_event(sched, kind, "holiday", *holiday))
        return
    if sched > now:
        events.append(_build_event(sched, kind, "scheduled"))
        return
    # 호출자가 외부 신호(snapshot match 등)로 done 판정해 done_summary를 줬으면 done,
    # 아니면 missed.
    if done_summary:
        events.append(_build_event(sched, kind, "done", done_summary))
    else:
        events.append(_build_event(sched, kind, "missed", "", missed_detail))


def _krx_events(snaps: list[SyncSnapshot], now: datetime,
                 window_start: datetime, window_end: datetime) -> list[dict]:
    """KRX 사이클(08:55) + 정산(15:35) 평일 occurrences."""
    events: list[dict] = []
    d = window_start.date()
    end_d = window_end.date()
    while d <= end_d:
        is_weekend = d.weekday() >= 5
        is_holiday = not is_weekend and not _is_session_day_safe("KR", d)
        for sched_time, kind, missed_msg, snapshot_market in [
            (KRX_CYCLE_TIME, "krx_cycle",
             "로컬앱이 시각에 실행 중이 아니었거나 grace(5분) 초과", "KRX"),
            (KRX_SETTLEMENT_TIME, "krx_settlement",
             "로컬앱이 15:35에 실행 중이 아니었습니다 — 미체결 정리·잔고 reconcile 누락", "KRX"),
        ]:
            sched = datetime.combine(d, sched_time, tzinfo=KST)
            if is_weekend:
                _emit_scheduled(events, sched, kind, now, window_start, window_end,
                                 holiday=("주말", "토·일 거래 없음"))
            elif is_holiday:
                _emit_scheduled(events, sched, kind, now, window_start, window_end,
                                 holiday=("휴장", "KRX 휴장일"))
            else:
                snap = _match_snapshot(snaps, sched, snapshot_market) if sched <= now else None
                summary = _summarize_cycle(snap) if (snap and kind == "krx_cycle") else (
                    "정산 완료" if snap else "")
                _emit_scheduled(events, sched, kind, now, window_start, window_end,
                                 done_summary=summary, missed_detail=missed_msg)
        d += timedelta(days=1)
    return events


def _us_events(snaps: list[SyncSnapshot], now: datetime,
                window_start: datetime, window_end: datetime) -> list[dict]:
    """US 사이클(open-5min) + 정산(close+5min) 캘린더 기반 동적 occurrences."""
    events: list[dict] = []
    cursor = window_start - timedelta(hours=2)  # 직전 세션이 윈도우 걸칠 수 있어 마진
    for _ in range(5):       # 24h 안에 US 세션 최대 1, 안전 5회
        try:
            sess = mc.next_session_kst("US", cursor)
        except mc.CalendarError:
            break
        if sess is None:
            break
        open_kst, close_kst = sess
        if open_kst > window_end + timedelta(hours=1):
            break

        cycle_sched = open_kst - timedelta(minutes=5)
        settle_sched = close_kst + timedelta(minutes=5)
        cycle_snap = _match_snapshot(snaps, cycle_sched, "US") if cycle_sched <= now else None
        settle_snap = _match_snapshot(snaps, settle_sched, "US") if settle_sched <= now else None

        _emit_scheduled(events, cycle_sched, "us_cycle", now, window_start, window_end,
                         done_summary=_summarize_cycle(cycle_snap) if cycle_snap else "",
                         missed_detail="로컬앱이 시각에 실행 중이 아니었거나 grace(10분) 초과")
        _emit_scheduled(events, settle_sched, "us_settlement", now, window_start, window_end,
                         done_summary="정산 완료" if settle_snap else "",
                         missed_detail="로컬앱이 close+5min에 실행 중이 아니었습니다")
        cursor = open_kst
    return events


def _preview_events(snaps: list[SyncSnapshot], now: datetime,
                     window_start: datetime, window_end: datetime) -> list[dict]:
    """매매 후보 결정 — 시장별 다른 데이터에 의존하므로 2 event로 분리.

    · krx_preview (07:30 KST): yfinance/FRED publish 직후 → 국장 cycle 8:55용
    · us_preview  (18:15 KST): KRX 종가·NAVER·technical 완성 직후 → 미장 cycle용
    """
    events: list[dict] = []
    # 가장 최신 next_day_preview generated_at + 시장별 후보 수.
    latest_gen: Optional[datetime] = None
    krx_n = us_n = 0
    for s in reversed(snaps):
        pv = (s.payload or {}).get("next_day_preview") or {}
        gen = pv.get("generated_at")
        if not gen:
            continue
        try:
            latest_gen = datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            continue
        for bs in pv.get("by_strategy") or []:
            for c in bs.get("candidates") or []:
                sym = c.get("symbol", "")
                if sym.isdigit():
                    krx_n += 1
                else:
                    us_n += 1
        break

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
                events.append(_build_event(
                    sched, kind, "missed", "",
                    "서버 preview 갱신 cron 결과를 받지 못했습니다"))
        d += timedelta(days=1)
    return events


def _is_session_day_safe(market: str, d: date) -> bool:
    """캘린더 만료·예외 시 True로 fallback — 사이클 차단 방지(AL-3 동일 정책)."""
    try:
        return mc.is_session_day(market, d)
    except Exception:
        return True


@router.get("/timeline")
def get_timeline(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """[now-24h, now+24h] 윈도우 자동매매 이벤트 + heartbeat 상태."""
    now = _now_kst()
    window_start = now - timedelta(hours=WINDOW_HOURS)
    window_end = now + timedelta(hours=WINDOW_HOURS)

    settings = session.exec(
        select(UserSettings).where(UserSettings.user_id == user.id)
    ).first()
    last_hb = settings.last_heartbeat_at if settings else None
    if last_hb and last_hb.tzinfo is None:
        last_hb = last_hb.replace(tzinfo=timezone.utc)

    snaps = _snapshots_in_window(session, user.id, window_start, window_end)

    events = (
        _krx_events(snaps, now, window_start, window_end)
        + _us_events(snaps, now, window_start, window_end)
        + _preview_events(snaps, now, window_start, window_end)
    )
    events.sort(key=lambda e: e["at"])

    return {
        "now": now.isoformat(),
        "heartbeat_at": last_hb.isoformat() if last_hb else None,
        "heartbeat_status": _heartbeat_status(last_hb, now),
        "events": events,
    }
