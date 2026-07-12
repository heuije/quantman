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

from .models import (ActivityEvent, BacktestRun, ChatTurnMetric, CompileLog,
                     Conversation, Device, HeartbeatEvent, Message, Strategy,
                     User)

_KST = timezone(timedelta(hours=9))

# 활성 유저·최근활동 판정 소스 — (user_id 컬럼, 타임스탬프 컬럼). **사람의 행동만**:
# 백테스트 실행·챗봇 질문·전략 컴파일·화면/종목 조회. 로컬앱 heartbeat(5분 자동 ping)·
# Device.last_seen(sync 갱신)은 기계 신호라 여기 넣지 않는다 — 자동매매만 켜둔 유저가
# "방금 활동"으로 표시되는 오해(2026-07-11 mwmw1119 사례)의 근본 원인이었다.
# 로컬앱 가동 여부는 유저 행의 local_app_at으로 분리 표시한다.
_HUMAN_ACTIVITY = [
    (BacktestRun.user_id, BacktestRun.created_at),
    (ChatTurnMetric.user_id, ChatTurnMetric.created_at),
    (CompileLog.user_id, CompileLog.created_at),
    (ActivityEvent.user_id, ActivityEvent.at),
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

    # ── 사람 활동 (user_id, ts) 쌍을 소스별로 로드 → 활성·최근활동·일별을 단일 패스로 파생 ──
    activity_pairs: list[tuple[int, datetime]] = []
    for uid_col, ts_col in _HUMAN_ACTIVITY:
        for uid, ts in session.exec(select(uid_col, ts_col)).all():
            if uid is not None and ts is not None:
                activity_pairs.append((uid, _as_utc(ts)))

    # ── 로컬앱 alive (기계 신호) — heartbeat·device sync의 유저별 최신. 별도 표시용 ──
    local_app: dict[int, datetime] = {}
    for uid_col, ts_col in ((HeartbeatEvent.user_id, HeartbeatEvent.at),
                            (Device.user_id, Device.last_seen_at)):
        for uid, ts in session.exec(
                select(uid_col, func.max(ts_col)).group_by(uid_col)).all():
            if uid is not None and ts is not None:
                ts = _as_utc(ts)
                if uid not in local_app or ts > local_app[uid]:
                    local_app[uid] = ts

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
        lap = local_app.get(u.id)
        user_rows.append({
            "id": u.id,
            "email": u.email,
            "created_at": _as_utc(u.created_at).isoformat(),
            "last_active_at": la.isoformat() if la else None,       # 사람 행동만
            "local_app_at": lap.isoformat() if lap else None,       # 로컬앱 heartbeat/sync
            "backtests": bt_ct.get(u.id, 0),
            "chat_turns": chat_ct.get(u.id, 0),
            "strategies": strat_ct.get(u.id, 0),
            "live_strategies": live_ct.get(u.id, 0),
            "devices": dev_ct.get(u.id, 0),
            "auth": _auth_of(u),
        })
    # 최근 활동순(활동 없는 유저는 뒤로). last_active_at ISO 문자열 역순 정렬.
    user_rows.sort(key=lambda r: r["last_active_at"] or "", reverse=True)

    # ── 활동 계측(ActivityEvent) — 인기 종목·화면별 사용량 (window 내) ──
    sym_counts = session.exec(
        select(ActivityEvent.symbol, func.count())
        .where(ActivityEvent.symbol.is_not(None), ActivityEvent.at >= window_start)
        .group_by(ActivityEvent.symbol)).all()
    top_symbols = sorted(
        [{"symbol": s, "views": n} for s, n in sym_counts if s is not None],
        key=lambda r: r["views"], reverse=True)[:20]
    path_counts = session.exec(
        select(ActivityEvent.path, func.count())
        .where(ActivityEvent.at >= window_start)
        .group_by(ActivityEvent.path)).all()
    screen_usage = sorted(
        [{"path": p, "views": n} for p, n in path_counts],
        key=lambda r: r["views"], reverse=True)[:20]

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "totals": totals,
        "active_users": active_users,
        "signups": signups,
        "auth_breakdown": auth_breakdown,
        "daily": daily,
        "users": user_rows[:top_users],
        "top_symbols": top_symbols,
        "screen_usage": screen_usage,
    }


def _msg_text(parts) -> str:
    """Message.parts(블록 배열)에서 사람이 읽는 텍스트만 추출(도구 블록 제외)."""
    return " ".join(
        p.get("text", "") for p in (parts or [])
        if isinstance(p, dict) and p.get("type") == "text").strip()


def recent_chat_inputs(session: Session, *, limit: int = 100) -> list[dict]:
    """최근 유저 챗봇 입력 N건 — 시각·이메일·질문 + (있으면) 봇 답변.

    운영자 대시보드용. Message(role='user')를 최신순으로 뽑고, 각 질문의 봇 답변은
    같은 대화에서 그 질문 바로 다음 assistant 메시지로 페어링한다. 안전정보만
    (질문·답변 텍스트) — 자격증명·계좌번호 없음.
    """
    user_msgs = session.exec(
        select(Message).where(Message.role == "user")
        .order_by(Message.id.desc()).limit(limit)).all()
    if not user_msgs:
        return []
    conv_ids = {m.conversation_id for m in user_msgs}
    convs = {c.id: c for c in session.exec(
        select(Conversation).where(Conversation.id.in_(conv_ids))).all()}
    emails = {u.id: u.email for u in session.exec(
        select(User).where(User.id.in_({c.user_id for c in convs.values()}))).all()}
    # 답변 페어링 — 관련 대화의 assistant 메시지를 id 오름차순으로 모아, 질문 id 다음의 첫 답변.
    asst_by_conv: dict[int, list[Message]] = defaultdict(list)
    for a in session.exec(
            select(Message).where(Message.role == "assistant",
                                  Message.conversation_id.in_(conv_ids))
            .order_by(Message.id)).all():
        asst_by_conv[a.conversation_id].append(a)

    rows = []
    for m in user_msgs:
        conv = convs.get(m.conversation_id)
        answer = next((a for a in asst_by_conv.get(m.conversation_id, []) if a.id > m.id), None)
        rows.append({
            "id": m.id,
            "conversation_id": m.conversation_id,
            "email": emails.get(conv.user_id) if conv else None,
            "at": _as_utc(m.created_at).isoformat(),
            "question": _msg_text(m.parts),
            "answer": _msg_text(answer.parts) if answer else None,
        })
    return rows
