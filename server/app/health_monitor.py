"""자동매매 건강 모니터 — per-user · end-to-end 관측.

mwmw CON.parquet 인시던트(자동매매 설치 이래 발주 0이 며칠간 미탐지)의 근본은
자동매매 건강 텔레메트리가 `SyncSnapshot.payload`에 갇혀 운영자에게 안 보인 것이다.
이 모듈은 그 payload(+heartbeat·라이브 전략 수)를 읽어 유저별 MECE 건강 조건을
평가한다 — 침묵하는 총체적 실패(0발주·데이터결손·앱다운·동기화실패)를 신호등으로.

`admin_metrics`/`chat_analytics` 패턴 미러 — evaluate_health는 순수함수(입력→dict)라
hermetic 테스트가 가능하다(특히 mwmw 과거 스냅샷 replay). 보안 불변식: payload에서
읽는 키는 전부 안전정보(플래그·카운트·심볼·타임스탬프)뿐 — 자격증명·계좌번호·원시주문
미접근.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from .models import (HealthAlertState, HeartbeatEvent, Strategy, SyncSnapshot,
                     User)

# 상태 등급 (신호등)
GREEN = "green"      # 정상
AMBER = "amber"      # 주의(대시보드 표시, 자동 페이징 안 함)
RED = "red"          # 이상(운영자 페이징 대상 — 단 alertable인 조건만)
UNKNOWN = "unknown"  # 신호 부재(판정 불가)

# 임계 상수 — 초기값. 운영 관측으로 튜닝(설계문서 §4).
STALE_MIN = 30         # heartbeat가 이 분(min) 넘게 끊기면 앱 다운 의심
PUSH_GAP_HR = 6        # heartbeat 정상인데 스냅샷 push가 이 시간(hr) 넘게 없으면 동기화 실패
LOAD_RATIO_RED = 0.5   # 데이터 로드율(loaded/needed)이 이 아래면 데이터 결손

# "실제 매매 사이클"로 간주하는 kind — 여기서 0발주+데이터결손이면 dead-man's-switch.
# state_sync·settlement·reconcile 등은 진입 사이클이 아니라 제외(정상 0발주).
_TRADING_CYCLE_KINDS = frozenset({"cycle", "day_trade", "day_trade_close"})


def _as_utc(ts: datetime | None) -> datetime | None:
    """naive는 UTC로 간주(_now가 UTC 저장), tz-aware는 그대로."""
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _cond(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail}


def evaluate_health(
    payload: dict | None,
    *,
    heartbeat_at: datetime | None,
    snapshot_at: datetime | None,
    live_strategies: int,
    now: datetime,
    prior: dict | None = None,
) -> dict:
    """유저 1명의 최신 스냅샷을 MECE 건강 조건으로 평가한다(순수함수).

    payload: SyncSnapshot.payload (최신 1건). heartbeat_at: 최신 HeartbeatEvent.at.
    snapshot_at: 최신 SyncSnapshot.received_at. live_strategies: 라이브 전략 수.
    now: 평가 시각(UTC). prior: 직전 평가 결과(전이 판정용, 선택).

    반환: {conditions:{키:{status,detail}}, overall, alert_reasons:[{code,message}]}.
    alert_reasons = 캘린더 무관·고신뢰 RED만(운영자 페이징 대상). C1/C2(캘린더 민감)·
    C9(양성 잦음)는 대시보드엔 뜨되 자동 페이징 안 함.
    """
    p = payload or {}
    now = _as_utc(now)
    cs = p.get("cycle_summary") or {}
    diag = p.get("diagnostics") or {}
    conditions: dict[str, dict] = {}
    alerts: list[dict] = []

    has_live = live_strategies >= 1

    # ── C1 런타임 (앱 실행·heartbeat) ──
    hb = _as_utc(heartbeat_at)
    if not has_live:
        conditions["runtime"] = _cond(GREEN, "라이브 전략 없음 — 실행 대상 아님")
    elif hb is None:
        conditions["runtime"] = _cond(AMBER, "heartbeat 기록 없음")
    else:
        age_min = (now - hb).total_seconds() / 60
        if age_min > STALE_MIN:
            conditions["runtime"] = _cond(
                AMBER, f"heartbeat {age_min:.0f}분 전 — 앱/PC 꺼짐 가능")
        else:
            conditions["runtime"] = _cond(GREEN, f"heartbeat {age_min:.0f}분 전")

    # ── C2 연결 (스냅샷 push) ──
    sa = _as_utc(snapshot_at)
    if sa is None:
        conditions["connectivity"] = _cond(AMBER, "스냅샷 수신 이력 없음")
    else:
        gap_hr = (now - sa).total_seconds() / 3600
        hb_fresh = hb is not None and (now - hb).total_seconds() / 60 <= STALE_MIN
        if gap_hr > PUSH_GAP_HR and hb_fresh:
            conditions["connectivity"] = _cond(
                AMBER, f"push {gap_hr:.1f}시간 공백(heartbeat 정상) — 동기화 실패 가능")
        else:
            conditions["connectivity"] = _cond(GREEN, f"최근 push {gap_hr:.1f}시간 전")

    # ── C3 데이터 신선도 (번들 추출·로드율) ──
    bundle = diag.get("bundle") or {}
    dataset = diag.get("dataset") or {}
    data_red = False
    if not diag:
        conditions["data_freshness"] = _cond(
            UNKNOWN, "진단 미탑재(구버전 클라 또는 풀사이클 前)")
    elif bundle.get("result") == "failed":
        data_red = True
        conditions["data_freshness"] = _cond(
            RED, f"번들 추출 실패: {bundle.get('error_type') or bundle.get('error') or '?'}")
        alerts.append({"code": "bundle_failed", "message": conditions["data_freshness"]["detail"]})
    elif dataset:
        needed = int(dataset.get("needed") or 0)
        loaded = int(dataset.get("loaded") or 0)
        ratio = (loaded / needed) if needed else 1.0
        if needed > 0 and ratio < LOAD_RATIO_RED:
            data_red = True
            conditions["data_freshness"] = _cond(
                RED, f"데이터 로드율 {loaded}/{needed} ({ratio:.0%}) — 추출/신선도 결손")
        else:
            conditions["data_freshness"] = _cond(GREEN, f"데이터 로드 {loaded}/{needed}")
    else:
        conditions["data_freshness"] = _cond(GREEN, "번들 정상")

    # ── C7 판단 산출 (skip_no_data) ──
    # cycle_summary.n_skip_no_data 집계 우선(P2 승격 — decisions 리스트 없어도 판정 가능),
    # 없으면 decisions 스캔(구버전 하위호환).
    decisions = p.get("decisions") or []
    cs_n_skip = cs.get("n_skip_no_data")
    skip_no_data_n = cs_n_skip if cs_n_skip is not None else len(
        [d for d in decisions if d.get("action") == "skip_no_data"])
    had_orders = (cs.get("n_bought") or 0) > 0 or any(
        d.get("action") in ("buy", "sell", "enter", "exit") for d in decisions)
    decision_red = False
    if skip_no_data_n and not had_orders:
        decision_red = True
        conditions["decision_output"] = _cond(
            RED, f"{skip_no_data_n}종목 데이터결손으로 skip_no_data(발주 판단 불가)")
    elif decisions or had_orders:
        conditions["decision_output"] = _cond(GREEN, "판단 정상 산출")
    else:
        conditions["decision_output"] = _cond(UNKNOWN, "결정 이력 없음(무후보일 가능)")

    # ── C6 사이클 실행 ──
    if cs.get("error"):
        conditions["cycle_execution"] = _cond(RED, f"사이클 에러: {cs.get('error')}")
        alerts.append({"code": "cycle_error", "message": conditions["cycle_execution"]["detail"]})
    elif cs.get("kind"):
        conditions["cycle_execution"] = _cond(GREEN, f"사이클 완료(kind={cs.get('kind')})")
    else:
        conditions["cycle_execution"] = _cond(UNKNOWN, "사이클 요약 없음")

    # ── C8 발주 + dead-man's-switch (핵심 복합 신호) ──
    kind = cs.get("kind")
    cycle_ran = kind in _TRADING_CYCLE_KINDS
    n_bought = cs.get("n_bought")
    no_orders = (n_bought or 0) == 0
    if has_live and cycle_ran and no_orders and (data_red or decision_red):
        conditions["order_placement"] = _cond(
            RED, "데이터/발주 실패 의심 — 매매 사이클 실행됐으나 데이터 결손으로 0발주")
        alerts.append({
            "code": "no_orders_data_starved",
            "message": "라이브 전략 있는데 매매 사이클이 데이터 결손으로 0발주 "
                       "(설치 이래 무발주 사고 시그니처)"})
    elif cs.get("n_rejected"):
        conditions["order_placement"] = _cond(
            AMBER, f"주문 거부 {cs.get('n_rejected')}건")
    elif (n_bought or 0) > 0:
        conditions["order_placement"] = _cond(GREEN, f"발주 {n_bought}건 체결")
    else:
        conditions["order_placement"] = _cond(GREEN, "발주 없음(정상 무후보 또는 진입창 밖)")

    # ── C5 브로커/계좌 준비 ──
    # P2 emit: health.broker_ready(bool)·active_broker + 기존 account_handles + 토큰경고(warnings).
    # 토큰 만료 판정은 클라가 자기 tz로 계산한 verdict(health.warnings)를 그대로 소비한다
    # — 서버가 naive-local 타임스탬프를 UTC로 재해석하면 어긋나므로(클라가 tz의 진실).
    health = p.get("health") or {}
    broker_ready = health.get("broker_ready")
    handles = health.get("account_handles")
    token_warn = [w for w in (health.get("warnings") or []) if "토큰" in str(w)]
    if has_live and broker_ready is False:
        conditions["broker_ready"] = _cond(RED, "라이브 전략 있는데 브로커 미설정/자격증명 실패")
        alerts.append({"code": "broker_missing", "message": conditions["broker_ready"]["detail"]})
    elif has_live and broker_ready is None and handles is not None and len(handles) == 0:
        conditions["broker_ready"] = _cond(RED, "라이브 전략 있는데 브로커 계좌 미등록")
        alerts.append({"code": "broker_missing", "message": conditions["broker_ready"]["detail"]})
    elif broker_ready or handles:
        if token_warn:
            conditions["broker_ready"] = _cond(AMBER, f"브로커 준비됨 · {token_warn[0]}")
        else:
            conditions["broker_ready"] = _cond(GREEN, "브로커 준비됨")
    else:
        conditions["broker_ready"] = _cond(UNKNOWN, "브로커 상태 신호 없음(구버전 클라)")

    # ── C4 전략 유효성 (서버 preview 기반) ──
    # preview.skipped는 **보수적 투영**(실 발주 결정은 클라)이라 데이터 신선도(stale) 스킵도 포함한다.
    # 데이터 stale은 전략 무효가 아니다 — 특히 선물 연속물은 T+1 아침(08:10) 재구성이라 **저녁엔 항상
    # stale로 보이지만 08:55 사이클 전 해소**된다(main.py by-design). 진짜 데이터 문제는 클라
    # authoritative 신호(C7 skip_no_data·C8 0발주·dead-man's-switch)가 잡으므로, C4는 **구조적 전략
    # 무효**(유니버스 공집합·IR 파싱·참조 미해소)만 판정하고 stale-only 스킵은 무시한다 — 2026-07-13
    # mwmw 저녁 선물 stale이 '전략 무효 AMBER'로 오표시되던 false-positive 근본수정.
    prev = p.get("next_day_preview") or {}
    by_strat = prev.get("by_strategy") or []

    def _has_structural_skip(strat: dict) -> bool:
        """이 전략의 스킵 중 **데이터 신선도가 아닌**(=구조적 무효) 사유가 있나."""
        for sk in (strat.get("skipped") or []):
            reason = str(sk.get("reason", ""))
            if "stale" not in reason and "지연" not in reason:
                return True
        return False

    if has_live and by_strat:
        invalid = [s for s in by_strat if _has_structural_skip(s)]
        stale_only = [s for s in by_strat
                      if s.get("skipped") and not _has_structural_skip(s)]
        if invalid:
            conditions["strategy_validity"] = _cond(
                AMBER, f"라이브 전략 {len(invalid)}개 구조적 무효(유니버스·IR·참조) — preview skip")
        elif stale_only:
            # 전부 데이터 신선도 대기 — 전략 자체는 유효(선물은 아침 재구성 전 저녁 정상 상태).
            conditions["strategy_validity"] = _cond(
                GREEN, f"전략 {len(by_strat)}개 유효 (데이터 신선도 대기 — 전략 무효 아님)")
        else:
            conditions["strategy_validity"] = _cond(GREEN, f"전략 {len(by_strat)}개 preview 유효")
    else:
        conditions["strategy_validity"] = _cond(UNKNOWN, "preview 이력 없음")

    # ── C9 사후 정합 (reconcile drift — 대시보드 표시, 자동 페이징 안 함) ──
    rec = p.get("reconciliation") or {}
    extras = rec.get("external_extras") or []
    if rec.get("has_drift") or extras:
        conditions["reconciliation"] = _cond(
            AMBER, f"포지션 정합 drift(외부/미등록 {len(extras)}건)")
    else:
        conditions["reconciliation"] = _cond(GREEN, "원장↔브로커 정합")

    # ── 종합 ──
    statuses = [c["status"] for c in conditions.values()]
    if RED in statuses:
        overall = RED
    elif AMBER in statuses:
        overall = AMBER
    else:
        overall = GREEN

    return {"conditions": conditions, "overall": overall, "alert_reasons": alerts}


def compute_user_health(session: Session, *, now: datetime | None = None) -> list[dict]:
    """유저별 자동매매 건강 1회 스냅샷 — /admin/health · dead-man's-switch cron의 소스.

    라이브 전략 有 유저 ∪ 스냅샷 有 유저를 대상으로, **유저당 최신 스냅샷 1건만** 로드해
    egress를 억제한다(30일 전체 스캔 금지 — Neon egress 인시던트 교훈). 라이브 전략이
    있는데 스냅샷이 아예 없는 유저(설치했으나 한 번도 push 안 함)도 payload={}로 평가한다.
    """
    now = _as_utc(now or datetime.now(timezone.utc))

    # 라이브 전략 수 (유저별)
    live_counts = {
        uid: n for uid, n in session.exec(
            select(Strategy.user_id, func.count())
            .where(Strategy.run_mode == "live").group_by(Strategy.user_id)).all()
        if uid is not None}

    # 최신 heartbeat (유저별)
    hb: dict[int, datetime] = {}
    for uid, ts in session.exec(
            select(HeartbeatEvent.user_id, func.max(HeartbeatEvent.at))
            .group_by(HeartbeatEvent.user_id)).all():
        if uid is not None and ts is not None:
            hb[uid] = _as_utc(ts)

    # 최신 스냅샷 (유저당 최신 id 1건 — id는 삽입순 단조증가라 latest)
    latest_ids = [mx for _, mx in session.exec(
        select(SyncSnapshot.user_id, func.max(SyncSnapshot.id))
        .group_by(SyncSnapshot.user_id)).all() if mx is not None]
    snaps = {sn.user_id: sn for sn in session.exec(
        select(SyncSnapshot).where(SyncSnapshot.id.in_(latest_ids))).all()} if latest_ids else {}

    uids = set(live_counts) | set(snaps)
    emails = {u.id: u.email for u in session.exec(
        select(User).where(User.id.in_(uids))).all()} if uids else {}

    rows = []
    for uid in uids:
        sn = snaps.get(uid)
        res = evaluate_health(
            sn.payload if sn else {},
            heartbeat_at=hb.get(uid),
            snapshot_at=_as_utc(sn.received_at) if sn else None,
            live_strategies=live_counts.get(uid, 0),
            now=now)
        rows.append({
            "user_id": uid,
            "email": emails.get(uid),
            "live_strategies": live_counts.get(uid, 0),
            "snapshot_at": _as_utc(sn.received_at).isoformat() if sn else None,
            "heartbeat_at": hb[uid].isoformat() if uid in hb else None,
            "overall": res["overall"],
            "conditions": res["conditions"],
            "alert_reasons": res["alert_reasons"],
        })
    # RED 먼저, 그 다음 AMBER — 운영자가 문제 유저를 위에서 본다.
    _rank = {RED: 0, AMBER: 1, UNKNOWN: 2, GREEN: 3}
    rows.sort(key=lambda r: (_rank.get(r["overall"], 4), -(r["live_strategies"])))
    return rows


def _format_operator_alert(row: dict) -> str:
    who = row.get("email") or f"user {row['user_id']}"
    reasons = "\n".join(f"  · {a['message']}" for a in row["alert_reasons"])
    return (f"🚨 [Quant 운영] 자동매매 건강 이상 — {who}\n"
            f"  라이브 전략 {row['live_strategies']}개 · 상태 {row['overall'].upper()}\n"
            f"{reasons}\n"
            f"  → /admin 건강 패널에서 확인")


def scan_and_alert(session: Session, *, now: datetime | None = None,
                   send=None) -> dict:
    """전 유저 건강 평가 → 페이징 대상(alert_reasons)의 **전이 시에만** 운영자 알림.

    dead-man's-switch cron(main.py)이 주기 호출한다. on-ingest로는 못 잡는 "push 없는
    침묵 유저"까지 cron이 잡는다. send: callable(str)->bool(운영자 webhook 발송). None이면
    상태만 갱신(발송 안 함 — 테스트·webhook 미설정). 같은 alert 집합 반복은 재발송 안 함(스팸 방지).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    rows = compute_user_health(session, now=now)
    sent = 0
    for r in rows:
        codes = ",".join(sorted(a["code"] for a in r["alert_reasons"]))
        st = session.get(HealthAlertState, r["user_id"])
        prev = st.alert_codes if st else ""
        if codes and codes != prev:
            # 신규 또는 변경된 이상 → 발송(1회)
            if send is not None and send(_format_operator_alert(r)):
                sent += 1
            if st is None:
                st = HealthAlertState(user_id=r["user_id"])
            st.alert_codes = codes
            st.since = now
            st.last_alerted_at = now
            st.recovered_at = None
            session.add(st)
        elif not codes and st and st.alert_codes:
            # 회복 → 상태 클리어(회복은 조용히, 재이상 시 다시 발송되도록)
            st.alert_codes = ""
            st.since = None
            st.recovered_at = now
            session.add(st)
    session.commit()
    return {"evaluated": len(rows), "alerts_sent": sent}
