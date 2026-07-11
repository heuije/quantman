"""운영자 대시보드 집계 — 로그인 유저의 제품 행동·활성·운영 지표.

익명 방문 트래픽(가입 전)은 서버를 거치지 않으므로 3rd-party 클라이언트 분석
도구(Vercel Analytics 등)가 담당한다. 이 모듈은 **서버가 유니크하게 아는**
로그인 유저의 활동만 집계한다 — 기존 안전정보 테이블만 읽는다:
User·Strategy·Device·BacktestRun·ChatTurnMetric·CompileLog·HeartbeatEvent.
계정·전략 메타뿐이며 KIS 자격증명·계좌번호·원시주문은 없다(보안 불변식).

chat_analytics.compute_stats 패턴 미러 — 순수 함수(session in, dict out)라
hermetic 테스트가 가능하다. 베타 규모(수십~수백 유저)라 전건 로드 + 파이썬
집계로 충분하다(행이 크게 늘면 소스별 grouped 쿼리로 전환).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from .models import (BacktestRun, ChatTurnMetric, CompileLog, Device,
                     HeartbeatEvent, Strategy, User)

_KST = timezone(timedelta(hours=9))

# 활성 유저·최근활동 판정 소스 — (user_id 컬럼, 타임스탬프 컬럼).
# 제품 행동(백테스트·챗봇·전략 컴파일) + 로컬앱 alive(heartbeat). Device.last_seen_at는
# 컬럼명이 달라 별도로 합친다.
_ACTIVITY = [
    (BacktestRun.user_id, BacktestRun.created_at),
    (ChatTurnMetric.user_id, ChatTurnMetric.created_at),
    (CompileLog.user_id, CompileLog.created_at),
    (HeartbeatEvent.user_id, HeartbeatEvent.at),
]


def _as_utc(ts: datetime) -> datetime:
    """naive 타임스탬프는 UTC로 간주(_now가 UTC 저장). tz-aware는 그대로."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _kst_date(ts: datetime) -> str:
    """UTC 타임스탬프 → KST 달력 날짜(YYYY-MM-DD). 한국 서비스라 '하루'=KST 기준."""
    return _as_utc(ts).astimezone(_KST).date().isoformat()


def compute_admin_metrics(session: Session, *, now: datetime | None = None,
                          days: int = 30, top_users: int = 200) -> dict:
    """운영자 대시보드 지표 1회 스냅샷.

    now/days/top_users는 테스트·범위 제어용. now 미지정 시 현재(UTC).
    반환: totals(누적)·active_users(DAU/WAU/MAU 롤링)·signups·auth_breakdown·
    daily(KST 일별 추세)·users(유저별 롤업, 최근활동순).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    d1, d7, d30 = (now - timedelta(days=n) for n in (1, 7, 30))
    window_start = now - timedelta(days=days)

    # ── 누적 총계 ──
    def _count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return session.exec(stmt).one()

    totals = {
        "users": _count(User),
        "strategies": _count(Strategy),
        "live_strategies": _count(Strategy, Strategy.run_mode == "live"),
        "paper_strategies": _count(Strategy, Strategy.run_mode == "paper"),
        "devices": _count(Device),
        "backtests": _count(BacktestRun),
        "chat_turns": _count(ChatTurnMetric),
        "compiles": _count(CompileLog),
    }

    # ── 유저 목록 + 가입 인증수단 분포 ──
    users = session.exec(select(User)).all()

    def _auth_of(u: User) -> str:
        return "google" if u.google_sub else "naver" if u.naver_sub else "password"

    auth_breakdown = {"google": 0, "naver": 0, "password": 0}
    for u in users:
        auth_breakdown[_auth_of(u)] += 1

    signups = {
        "last_24h": sum(1 for u in users if _as_utc(u.created_at) >= d1),
        "last_7d": sum(1 for u in users if _as_utc(u.created_at) >= d7),
        "last_30d": sum(1 for u in users if _as_utc(u.created_at) >= d30),
    }

    # ── 활동 (user_id, ts) 쌍을 소스별로 로드 → 활성·최근활동·일별을 단일 패스로 파생 ──
    activity_pairs: list[tuple[int, datetime]] = []
    for uid_col, ts_col in _ACTIVITY:
        for uid, ts in session.exec(select(uid_col, ts_col)).all():
            if uid is not None and ts is not None:
                activity_pairs.append((uid, _as_utc(ts)))
    for uid, ts in session.exec(select(Device.user_id, Device.last_seen_at)).all():
        if uid is not None and ts is not None:
            activity_pairs.append((uid, _as_utc(ts)))

    last_active: dict[int, datetime] = {}
    active_d1: set[int] = set()
    active_d7: set[int] = set()
    active_d30: set[int] = set()
    active_by_day: dict[str, set[int]] = defaultdict(set)
    for uid, ts in activity_pairs:
        prev = last_active.get(uid)
        if prev is None or ts > prev:
            last_active[uid] = ts
        if ts >= d1:
            active_d1.add(uid)
        if ts >= d7:
            active_d7.add(uid)
        if ts >= d30:
            active_d30.add(uid)
        if ts >= window_start:
            active_by_day[_kst_date(ts)].add(uid)

    active_users = {"dau": len(active_d1), "wau": len(active_d7), "mau": len(active_d30)}

    # ── 일별 추세 (KST 날짜 버킷, 오래된→최신 days개) ──
    today_kst = now.astimezone(_KST).date()
    day_keys = [(today_kst - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    signup_by_day: dict[str, int] = defaultdict(int)
    for u in users:
        if _as_utc(u.created_at) >= window_start:
            signup_by_day[_kst_date(u.created_at)] += 1
    bt_by_day: dict[str, int] = defaultdict(int)
    for ts in session.exec(
            select(BacktestRun.created_at).where(BacktestRun.created_at >= window_start)).all():
        bt_by_day[_kst_date(ts)] += 1
    chat_by_day: dict[str, int] = defaultdict(int)
    for ts in session.exec(
            select(ChatTurnMetric.created_at).where(ChatTurnMetric.created_at >= window_start)).all():
        chat_by_day[_kst_date(ts)] += 1

    daily = [{
        "date": d,
        "signups": signup_by_day.get(d, 0),
        "active_users": len(active_by_day.get(d, ())),
        "backtests": bt_by_day.get(d, 0),
        "chat_turns": chat_by_day.get(d, 0),
    } for d in day_keys]

    # ── 유저별 롤업 (grouped COUNT) ──
    def _count_by_user(uid_col, *where) -> dict[int, int]:
        stmt = select(uid_col, func.count()).group_by(uid_col)
        for w in where:
            stmt = stmt.where(w)
        return {uid: n for uid, n in session.exec(stmt).all() if uid is not None}

    bt_ct = _count_by_user(BacktestRun.user_id)
    chat_ct = _count_by_user(ChatTurnMetric.user_id)
    strat_ct = _count_by_user(Strategy.user_id)
    live_ct = _count_by_user(Strategy.user_id, Strategy.run_mode == "live")
    dev_ct = _count_by_user(Device.user_id)

    user_rows = []
    for u in users:
        la = last_active.get(u.id)
        user_rows.append({
            "id": u.id,
            "email": u.email,
            "created_at": _as_utc(u.created_at).isoformat(),
            "last_active_at": la.isoformat() if la else None,
            "backtests": bt_ct.get(u.id, 0),
            "chat_turns": chat_ct.get(u.id, 0),
            "strategies": strat_ct.get(u.id, 0),
            "live_strategies": live_ct.get(u.id, 0),
            "devices": dev_ct.get(u.id, 0),
            "auth": _auth_of(u),
        })
    # 최근 활동순(활동 없는 유저는 뒤로). last_active_at ISO 문자열 역순 정렬.
    user_rows.sort(key=lambda r: r["last_active_at"] or "", reverse=True)

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "totals": totals,
        "active_users": active_users,
        "signups": signups,
        "auth_breakdown": auth_breakdown,
        "daily": daily,
        "users": user_rows[:top_users],
    }
