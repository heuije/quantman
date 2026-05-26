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

# Preview 최종 결정 cron(서버 main.py) — 매매 후보 webhook 발송 시각.
# 07:30·17:15도 preview를 갱신하지만 18:15 직후가 최종이며 webhook은 여기서만.
# 사용자에게 노출하는 "후보 결정" 표상은 단일 시각으로 단순화.
PREVIEW_DECISION_TIME = time(18, 15)

# KRX 자동매매 사이클 (로컬앱 scheduler.py 와 동일 출처).
KRX_CYCLE_TIME = time(8, 55)


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


def _krx_cycle_events(snaps: list[SyncSnapshot], now: datetime,
                       window_start: datetime, window_end: datetime) -> list[dict]:
    """KRX 08:55 KST 평일 cycle — 윈도우 안의 occurrences."""
    events: list[dict] = []
    # 윈도우 시작 날짜부터 끝 날짜까지 순회
    d = window_start.date()
    end_d = window_end.date()
    while d <= end_d:
        sched = datetime.combine(d, KRX_CYCLE_TIME, tzinfo=KST)
        if window_start <= sched <= window_end:
            if d.weekday() >= 5:                         # 토·일
                events.append(_build_event(sched, "krx_cycle", "holiday",
                                            "주말", "토·일 거래 없음"))
            elif not _is_session_day_safe("KR", d):
                events.append(_build_event(sched, "krx_cycle", "holiday",
                                            "휴장", "KRX 휴장일"))
            elif sched > now:
                events.append(_build_event(sched, "krx_cycle", "scheduled"))
            else:
                snap = _match_snapshot(snaps, sched, "KRX")
                if snap:
                    events.append(_build_event(sched, "krx_cycle", "done",
                                                _summarize_cycle(snap)))
                else:
                    events.append(_build_event(
                        sched, "krx_cycle", "missed", "",
                        "로컬앱이 시각에 실행 중이 아니었거나 grace(5분) 초과"))
        d += timedelta(days=1)
    return events


def _us_cycle_events(snaps: list[SyncSnapshot], now: datetime,
                      window_start: datetime, window_end: datetime) -> list[dict]:
    """US cycle — open-5min, 캘린더 기반 동적. 윈도우 안의 occurrences."""
    events: list[dict] = []
    # 윈도우 안의 US 세션을 모두 모은다 — next_session_kst를 cursor로 사용.
    cursor = window_start - timedelta(minutes=10)  # 직전 세션이 윈도우에 걸쳐있을 가능성
    for _ in range(5):  # 24h에 US 세션은 최대 1개. 안전 마진 5회.
        try:
            sess = mc.next_session_kst("US", cursor)
        except mc.CalendarError:
            break
        if sess is None:
            break
        open_kst, _close = sess
        if open_kst > window_end + timedelta(hours=1):
            break
        sched = open_kst - timedelta(minutes=5)        # scheduler.py와 동일
        if window_start <= sched <= window_end:
            if sched > now:
                events.append(_build_event(sched, "us_cycle", "scheduled"))
            else:
                snap = _match_snapshot(snaps, sched, "US")
                if snap:
                    events.append(_build_event(sched, "us_cycle", "done",
                                                _summarize_cycle(snap)))
                else:
                    events.append(_build_event(
                        sched, "us_cycle", "missed", "",
                        "로컬앱이 시각에 실행 중이 아니었거나 grace(10분) 초과"))
        cursor = open_kst  # next iter는 다음 세션
    return events


def _preview_decision_events(snaps: list[SyncSnapshot], now: datetime,
                              window_start: datetime,
                              window_end: datetime) -> list[dict]:
    """매일 18:15 KST 매매 후보 결정 cron — 가장 최신 snapshot의 preview를 본다."""
    events: list[dict] = []
    d = window_start.date()
    end_d = window_end.date()
    # 최신 next_day_preview generated_at — 모든 sched와 비교에 사용
    latest_gen: Optional[datetime] = None
    candidates_summary = ""
    for s in reversed(snaps):
        pv = (s.payload or {}).get("next_day_preview") or {}
        gen = pv.get("generated_at")
        if not gen:
            continue
        try:
            gdt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
            latest_gen = gdt.astimezone(KST)
        except ValueError:
            continue
        # 후보 수 요약
        by_strat = pv.get("by_strategy") or []
        krx_n = us_n = 0
        for bs in by_strat:
            for c in bs.get("candidates") or []:
                sym = c.get("symbol", "")
                if sym.isdigit():
                    krx_n += 1
                else:
                    us_n += 1
        parts = []
        if krx_n:
            parts.append(f"KRX {krx_n}건")
        if us_n:
            parts.append(f"US {us_n}건")
        candidates_summary = " · ".join(parts) if parts else "후보 0건"
        break

    while d <= end_d:
        sched = datetime.combine(d, PREVIEW_DECISION_TIME, tzinfo=KST)
        if window_start <= sched <= window_end:
            if sched > now:
                events.append(_build_event(sched, "preview", "scheduled"))
            else:
                # latest_gen이 sched 이후라면 이 시각의 결과로 본다.
                if latest_gen and latest_gen >= sched - timedelta(minutes=2):
                    events.append(_build_event(sched, "preview", "done",
                                                candidates_summary))
                else:
                    events.append(_build_event(
                        sched, "preview", "missed", "",
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
        _krx_cycle_events(snaps, now, window_start, window_end)
        + _us_cycle_events(snaps, now, window_start, window_end)
        + _preview_decision_events(snaps, now, window_start, window_end)
    )
    events.sort(key=lambda e: e["at"])

    return {
        "now": now.isoformat(),
        "heartbeat_at": last_hb.isoformat() if last_hb else None,
        "heartbeat_status": _heartbeat_status(last_hb, now),
        "events": events,
    }
