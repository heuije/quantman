"""웹 ↔ 로컬앱 동기화 라우터.

안전정보만 오간다 — 전략(설정)·잔고·포지션·자산곡선·체결로그.
API키·계좌번호·원시주문은 이 경로를 절대 통과하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status, BackgroundTasks, Request, Response
from sqlmodel import Session, select

from ..db import engine, get_session
from ..deps import get_current_device, get_current_user
from ..models import Device, HeartbeatEvent, Strategy, SyncSnapshot, User, UserSettings
from ..schemas import (StrategyOut, SyncPushIn, SyncSnapshotOut,
                       TradableSymbolsSyncIn)

_log = logging.getLogger("app.sync")

router = APIRouter(prefix="/sync", tags=["sync"])


# ── 로컬앱 → 서버 (기기 토큰 인증) ─────────────────────────────────────────────

@router.post("/push")
def push_snapshot(
    body: SyncPushIn,
    background_tasks: BackgroundTasks,
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """로컬앱이 잔고·포지션·자산곡선·체결로그를 푸시.

    S-03 — _check_alerts는 외부 webhook(Discord/Slack)을 호출하므로 응답 경로에
    인라인이면 webhook 1건당 최대 5s씩 누적된다. BackgroundTasks로 응답 후
    실행 → push 응답 시간은 webhook 지연과 분리된다. 단 같은 프로세스에서
    돌아가므로 진정한 격리는 아님(Redis 큐는 베타엔 과설계).
    """
    snap = SyncSnapshot(user_id=device.user_id, device_id=device.id,
                        payload=body.payload)
    session.add(snap)
    session.commit()
    background_tasks.add_task(_check_alerts_bg, device.user_id, body.payload)
    return {"ok": True}


def _check_alerts_bg(user_id: int, payload: dict) -> None:
    """BackgroundTask용 — 별도 Session으로 알림 검사. 응답 후 실행되므로
    원래 요청의 session은 이미 닫혀 있다.
    """
    try:
        with Session(engine) as bg_session:
            _check_alerts(bg_session, user_id, payload)
    except Exception as e:
        _log.warning("알림 검사 실패: %s", e)


def _post_webhook(url: str, text: str) -> bool:
    """Discord/Slack 호환 형태로 알림 발송 (Discord는 content=, Slack은 text=).

    S-03 — timeout 8s → 5s. 여러 알림이 누적될 때 push 응답 시간이 8s/건씩 늘어나는
    문제 완화. 5s는 Discord/Slack의 정상 응답엔 충분하고 장애 시 빠른 fail.
    완전 fire-and-forget(BackgroundTasks)으로 전환하려면 DB 성공-갱신 결합을
    리팩토링해야 하므로 별도 작업.
    """
    if not url:
        return False
    try:
        body = {"content": text, "text": text}
        r = requests.post(url, json=body, timeout=5)
        return 200 <= r.status_code < 300
    except Exception as e:
        _log.warning("webhook 전송 실패: %s", e)
        return False


def _check_alerts(session: Session, user_id: int, payload: dict) -> None:
    """snapshot의 kill_switch·손실률·미체결 카운트를 사용자 임계치와 비교해 알림."""
    s = session.get(UserSettings, user_id)
    if s is None or not s.alert_webhook_url:
        return
    now = datetime.now(timezone.utc)
    cooldown = timedelta(hours=1)

    # 1. Kill switch 상태 변화 알림 (Phase 38.8 — 활성/해제 양방향)
    ks = (payload or {}).get("kill_switch") or {}
    if s.alert_on_killswitch:
        is_active = bool(ks.get("active"))
        was_alerted = s.last_alerted_killswitch is not None
        if is_active and not was_alerted:
            # 발동 알림 (cooldown 없이 즉시 1회)
            reason = ks.get("reason", "(없음)")
            since = ks.get("since", "")
            ok = _post_webhook(
                s.alert_webhook_url,
                f"🚨 [Quant] Kill Switch 발동\n"
                f"  사유: {reason}\n"
                f"  발동시각: {since}\n"
                f"  → 사용자가 명시적으로 해제할 때까지 신규 진입 차단. 청산은 계속.")
            if ok:
                s.last_alerted_killswitch = now
                session.add(s); session.commit()
        elif not is_active and was_alerted:
            # 해제 알림
            ok = _post_webhook(
                s.alert_webhook_url,
                "✅ [Quant] Kill Switch 해제 — 다음 사이클부터 신규 진입 재개")
            if ok:
                s.last_alerted_killswitch = None
                session.add(s); session.commit()

    # 1b. Drawdown 한도 알림 (Phase 38.10) — last_alerted_loss를 공유해 스팸 방지
    cs = (payload or {}).get("cycle_summary") or {}
    dd_active = bool(cs.get("drawdown_active"))
    if dd_active:
        last = s.last_alerted_loss
        if last is None or now - last > cooldown:
            dd_pct = cs.get("drawdown_pct", 0.0)
            peak = cs.get("peak_equity", 0)
            limit = cs.get("max_drawdown_limit_pct", 0)
            ok = _post_webhook(
                s.alert_webhook_url,
                f"⚠️ [Quant] 누적 Drawdown {dd_pct:.2f}% 한도 도달\n"
                f"  자본 고점: {peak:,.0f}원 · 한도: -{limit:.1f}%\n"
                f"  → 신규 진입 차단 (peak 회복 시 자동 해제, 청산은 계속)")
            if ok:
                s.last_alerted_loss = now
                session.add(s); session.commit()

    # 1c. Preview 누락 연속 카운터 + 알림 (Phase 38.5)
    preview_missing = bool(cs.get("preview_missing"))
    if preview_missing:
        s.preview_missing_streak = (s.preview_missing_streak or 0) + 1
        threshold = max(1, int(s.preview_missing_alert_threshold or 3))
        if s.preview_missing_streak >= threshold:
            last = s.last_alerted_preview_missing
            if last is None or now - last > timedelta(hours=12):
                ok = _post_webhook(
                    s.alert_webhook_url,
                    f"⚠️ [Quant] Preview 연속 누락 {s.preview_missing_streak}회\n"
                    f"  → 신규 진입이 며칠째 보류 중. 서버 cron·페어링 점검 필요.")
                if ok:
                    s.last_alerted_preview_missing = now
        session.add(s); session.commit()
    elif (s.preview_missing_streak or 0) > 0:
        # 회복: 카운터 리셋
        s.preview_missing_streak = 0
        s.last_alerted_preview_missing = None
        session.add(s); session.commit()

    # 1d. 미국 실시간 시세 미신청 (장중 실시간 손절 미제공) — kind=us_realtime_unavailable
    #     스냅샷에만 실려 세션당 1회 도착하므로 별도 cooldown 불필요.
    if cs.get("us_realtime_unavailable"):
        _post_webhook(
            s.alert_webhook_url,
            "⚠️ [Quant] 미국 실시간 시세 미수신 — 장중 실시간 손절(익절/손절/"
            "트레일링) 미제공. KIS HTS [7781] 해외 실시간 시세 신청 필요. "
            "(미신청 시 미국 종목은 장 마감 후 사이클에서만 청산 평가)")

    # 2. 일일 손실 임계 도달
    ks_start = ks.get("day_start_equity")
    bal = (payload or {}).get("balance") or {}
    cur_eval = bal.get("total_eval")
    if (ks_start and cur_eval and ks_start > 0
            and s.alert_on_daily_loss_pct > 0):
        change_pct = (cur_eval - ks_start) / ks_start * 100
        if change_pct <= -abs(s.alert_on_daily_loss_pct):
            last = s.last_alerted_loss
            if last is None or now - last > cooldown:
                ok = _post_webhook(
                    s.alert_webhook_url,
                    f"[Quant 알림] 일일 손실 {change_pct:.2f}% 도달 "
                    f"(임계 -{s.alert_on_daily_loss_pct}%)")
                if ok:
                    s.last_alerted_loss = now
                    session.add(s); session.commit()

    # 3. Phase 40 — 잔고 정합성 drift 알림 (HTS/MTS 수동 매매 추정)
    rec = (payload or {}).get("reconciliation") or {}
    if s.alert_on_reconcile_drift and rec.get("has_drift"):
        last = s.last_alerted_reconcile
        if last is None or now - last > cooldown:
            applied = rec.get("applied") or []
            extras = rec.get("external_extras") or []
            lines = ["📋 [Quant] 잔고 정합성 drift 감지 (HTS/MTS 수동 매매 추정)"]
            if applied:
                lines.append(f"  자동 차감: {len(applied)}건")
                for a in applied[:5]:
                    lines.append(
                        f"    · {a['symbol']} {a['old_qty']}→{a['new_qty']}주"
                        f" (-{a['removed_qty']})"
                        + (" [전량 청산]" if a.get("fully_closed") else ""))
                if len(applied) > 5:
                    lines.append(f"    · 외 {len(applied) - 5}건…")
            if extras:
                lines.append(f"  외부 매수(미관여): {len(extras)}건")
                for e in extras[:5]:
                    lines.append(
                        f"    · {e['symbol']} 초과 {e['excess']}주"
                        + (" (자동매매 보유분에 추가)" if e.get("in_ledger") else " (신규)"))
                if len(extras) > 5:
                    lines.append(f"    · 외 {len(extras) - 5}건…")
            ok = _post_webhook(s.alert_webhook_url, "\n".join(lines))
            if ok:
                s.last_alerted_reconcile = now
                session.add(s); session.commit()

    # 4. Phase 48 P1-C — 슬리피지 임계 초과 알림 (avg_bps)
    slip = (payload or {}).get("slippage") or {}
    avg_bps = slip.get("avg_bps")
    sample_n = int(slip.get("n") or 0)
    threshold = int(s.alert_on_slippage_bps or 0)
    # 표본 5건 이상 + 임계값 활성 시만 평가 (소수 표본은 노이즈)
    if (threshold > 0 and avg_bps is not None and sample_n >= 5
            and float(avg_bps) > threshold):
        last = s.last_alerted_slippage
        if last is None or now - last > cooldown:
            ok = _post_webhook(
                s.alert_webhook_url,
                f"📉 [Quant] 평균 슬리피지 {float(avg_bps):.1f}bps 초과 "
                f"(임계 {threshold}bps, 표본 {sample_n}건). "
                f"체결가가 의도가에서 평균 {float(avg_bps)/100:.2f}%p 벗어남.")
            if ok:
                s.last_alerted_slippage = now
                session.add(s); session.commit()


@router.get("/strategies", response_model=list[StrategyOut])
def pull_strategies(
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """로컬앱이 모의/실전으로 배정된 전략을 풀(pull)."""
    rows = session.exec(
        select(Strategy).where(
            Strategy.user_id == device.user_id,
            Strategy.run_mode.in_(["paper", "live"]),
        )
    ).all()
    # engine을 definition에도 주입 — 로컬앱(trader·intraday_stop)은 definition.get("engine")
    # 으로 IR/operand를 디스패치한다. StrategyIR.model_dump엔 engine 필드가 없어 stored
    # definition엔 빠져 있으므로, 자기완결 spec이 되도록 serve 시점에 column 값을 합친다.
    return [StrategyOut(id=s.id, name=s.name, run_mode=s.run_mode, engine=s.engine,
                        definition={**(s.definition or {}), "engine": s.engine},
                        created_at=s.created_at, updated_at=s.updated_at) for s in rows]


@router.get("/risk_limits")
def pull_risk_limits(
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """로컬앱이 사용자별 위험 한도 설정(kill switch·drawdown)을 풀(pull).

    Phase 38.7/38.10 — null 필드는 글로벌 default로 동작.
    """
    from ..models import UserSettings
    s = session.get(UserSettings, device.user_id)
    return {
        "kill_switch_daily_loss_pct": (
            s.kill_switch_daily_loss_pct if s else None),
        "max_drawdown_pct": s.max_drawdown_pct if s else None,
        "us_buying_power_mode": (
            s.us_buying_power_mode if s else "integrated"),
        # Phase 48 P1-D — 일일 거래 한도 (0이면 비활성)
        "daily_turnover_limit_krw": (
            int(s.daily_turnover_limit_krw or 0) if s else 0),
        "daily_trade_count_limit": (
            int(s.daily_trade_count_limit or 0) if s else 0),
    }


@router.get("/preview")
def pull_preview(
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
) -> dict:
    """로컬앱이 자기 계정의 최신 next_day_preview를 풀(pull) — 디바이스 인증.

    웹용 `/preview/next-day`(유저 JWT)와 동일 데이터를 디바이스 인증으로 노출한다.
    로컬앱 사이클(runner.run_cycle)이 매수 후보(candidates)를 받기 위해 호출하며,
    preview는 데이터 cron이 `SyncSnapshot.payload.next_day_preview`에 merge한다.

    역할 분리: 디바이스(로컬앱)는 `/sync/*`·디바이스 토큰, 웹은 `/preview/*`·유저 JWT.
    이전엔 로컬앱이 유저 전용 `/preview/next-day`를 호출해 항상 401 → 신규 진입 차단
    버그가 있었다(이 엔드포인트로 교체해 해결).

    v0.9.13 — preview catch-up on-demand build:
    사용자 PC가 server cron 시점(07:30·18:15)에 꺼져 있어도 KRX 매수 catch-up이
    동작하도록, snapshot에 preview 없으면 *즉시* build_user_preview 호출 + merge.
    cron 의존성 제거. build_user_preview는 KIS 호출 0회 (수십ms).
    """
    snap = session.exec(
        select(SyncSnapshot).where(SyncSnapshot.user_id == device.user_id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    if snap is None or not snap.payload:
        return {"available": False, "reason": "스냅샷 없음 — 로컬앱 sync 필요"}
    preview = (snap.payload or {}).get("next_day_preview")
    if preview is not None:
        return preview

    # On-demand build (v0.9.13) — preview 미준비 시 자리에서 생성.
    from .. import preview_engine
    try:
        preview = preview_engine.build_user_preview(
            session, device.user_id, "on_demand_pull")
    except Exception as e:
        _log.exception("on-demand preview build 실패 (user=%s): %s",
                        device.user_id, e)
        return {"available": False,
                "reason": f"preview 생성 실패: {type(e).__name__}"}
    if preview.get("available"):
        # SyncSnapshot에 merge — 다음 호출은 빠른 경로로 빠짐.
        new_payload = dict(snap.payload or {})
        new_payload["next_day_preview"] = preview
        snap.payload = new_payload
        session.add(snap)
        session.commit()
    return preview


@router.get("/timeline")
def pull_timeline(
    request: Request,
    response: Response,
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
) -> dict:
    """로컬앱이 자기 계정의 자동매매 timeline을 풀(pull) — 디바이스 인증.

    웹용 `/trading/timeline`(유저 JWT)과 동일 데이터를 디바이스 인증으로 노출.
    로컬앱 GUI가 hero 아래 패널에 어제·오늘·내일 6 종류 event(국장/미장 ×
    후보결정·시작·종료) + heartbeat 상태를 렌더할 때 호출.

    역할 분리: 디바이스(로컬앱)는 `/sync/*`·디바이스 토큰, 웹은 `/trading/*`·
    유저 JWT. 동일 헬퍼(_krx_events·_us_events·_preview_events) 재사용으로
    웹·로컬 표시 결과 항상 동일 보장.
    """
    from datetime import datetime, timedelta, timezone as _tz
    from zoneinfo import ZoneInfo
    from ..models import HeartbeatEvent
    from . import trading
    KST = ZoneInfo("Asia/Seoul")
    now = datetime.now(KST)
    window_start = now - timedelta(hours=trading.WINDOW_HOURS)
    window_end = now + timedelta(hours=trading.WINDOW_HOURS)

    settings = session.exec(
        select(UserSettings).where(UserSettings.user_id == device.user_id)
    ).first()
    last_hb = settings.last_heartbeat_at if settings else None
    if last_hb and last_hb.tzinfo is None:
        last_hb = last_hb.replace(tzinfo=_tz.utc)

    # tag-first ETag(D2) — /trading/timeline과 동일 헬퍼·동일 동인. window 조회 *전에*
    # scalar(최신 received_at + last_hb)로 ETag 계산, If-None-Match 매칭 시 304만 반환해
    # window 조회·이벤트 합성을 건너뛴다(Neon egress 절감). 로컬앱 sync_client.pull_timeline이
    # If-None-Match를 송신·304 캐시 처리한다. 근거: docs/incidents/2026-06-10-neon-data-transfer-quota.md
    latest_received = session.exec(
        select(SyncSnapshot.received_at)
        .where(SyncSnapshot.user_id == device.user_id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    etag = trading._timeline_etag(latest_received, last_hb, now)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag

    snaps = trading._snapshots_in_window(session, device.user_id, window_start, window_end)
    heartbeats = trading._heartbeats_in_window(session, device.user_id,
                                                 window_start, window_end)
    preview = trading._latest_preview_in_window(session, device.user_id,
                                                 window_start, window_end)

    events = (
        trading._krx_events(snaps, heartbeats, now, window_start, window_end)
        + trading._us_events(snaps, heartbeats, now, window_start, window_end)
        + trading._preview_events(preview, now, window_start, window_end)
    )
    events.sort(key=lambda e: e["at"])
    return {
        "now": now.isoformat(),
        "heartbeat_at": last_hb.isoformat() if last_hb else None,
        "heartbeat_status": trading._heartbeat_status(last_hb, now),
        "events": events,
    }


@router.post("/tradable_symbols", deprecated=True)
def push_tradable_symbols(
    body: TradableSymbolsSyncIn,
    device: Device = Depends(get_current_device),
):
    """[Deprecated] 서버가 KIS 마스터를 직접 캐싱하므로 로컬앱 push 불필요.

    구버전 로컬앱 호환을 위해 200 OK를 반환하지만 아무것도 저장하지 않는다.
    """
    return {"ok": True, "n": 0, "deprecated": True,
            "note": "서버가 KIS 공식 마스터를 매일 06:00 KST 자동 갱신합니다."}


# ── 서버 → 웹 (JWT 인증) ───────────────────────────────────────────────────────

@router.post("/heartbeat")
def push_heartbeat(
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """Phase 58+ — 로컬앱 alive 신호. 5분 주기, KIS API 호출 없음.

    UserSettings.last_heartbeat_at(latest 1건) + HeartbeatEvent(history row 1건).
    Latest는 "현재 살아있는지" 빠른 조회용, history는 "과거 임의 시점에 살아있었는지"
    판정용(missed cycle 원인 A vs B 분류에 필수).

    Phase A(latest)·Phase B(history) 분리 commit — history 테이블 누락·DDL 지연 시
    latest 갱신은 무조건 성공시켜 웹앱 "끊김" false alarm 방지(근본 원인: history
    insert 한 번이 실패하면 sqlmodel transaction 전체 rollback돼 latest까지 못 받는다).
    """
    now = datetime.now(timezone.utc)
    # ── Phase A: latest 갱신 (primary, 절대 실패시키면 안 됨) ──────────────
    settings = session.exec(
        select(UserSettings).where(UserSettings.user_id == device.user_id)
    ).first()
    if settings is None:
        settings = UserSettings(user_id=device.user_id, last_heartbeat_at=now)
        session.add(settings)
    else:
        settings.last_heartbeat_at = now
    session.commit()

    # ── Phase B: history row (보조, 실패 허용 — 진단용 데이터) ──────────────
    try:
        session.add(HeartbeatEvent(user_id=device.user_id, device_id=device.id, at=now))
        session.commit()
    except Exception as e:
        # 테이블 미생성·DDL 지연·DB 일시 장애 등 — latest는 이미 commit됨, alive 신호엔 문제 없음.
        # missed cycle 진단 정확도가 잠시 떨어지지만 핵심 기능엔 영향 X.
        session.rollback()
        _log.warning("[heartbeat] history insert 실패 (진단 정확도만 영향): %s", e)
    return {"ok": True}


@router.get("/snapshot", response_model=SyncSnapshotOut | None)
def latest_snapshot(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """웹 대시보드가 로컬앱이 보낸 최신 스냅샷을 조회.

    ETag (audit P0-1) — Monitor 페이지가 폴링하는 가장 큰 endpoint.
    snapshot.received_at + last_heartbeat_at 중 max를 ms epoch으로 ETag화 →
    클라이언트 If-None-Match 매칭 시 304 (body 0). egress·DB JSON serialization
    부담 큰 폭 절감. snapshot push 또는 heartbeat 갱신마다 자동으로 새 ETag.

    tag-first(인시던트 driver D3) — ETag를 received_at scalar로 먼저 계산해, 304
    경로에서는 큰 payload JSON 컬럼을 *아예 SELECT하지 않는다*. 15s 폴링이라
    과거엔 full snapshot row(payload 포함)를 매번 읽고 ETag를 계산해 304여도 egress가
    발생했다. 단 ETag 공식·값은 종전과 byte-identical(received_at·last_hb의 .timestamp()
    max) — 기존 클라이언트 캐시가 그대로 유효하다.
    근거: docs/incidents/2026-06-10-neon-data-transfer-quota.md
    """
    latest_received = session.exec(
        select(SyncSnapshot.received_at)
        .where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    settings = session.exec(
        select(UserSettings).where(UserSettings.user_id == user.id)
    ).first()
    last_hb = settings.last_heartbeat_at if settings else None
    if latest_received is None and last_hb is None:
        return None

    ts_components = []
    if latest_received:
        ts_components.append(latest_received.timestamp())
    if last_hb:
        ts_components.append(last_hb.timestamp())
    etag = f'W/"{int(max(ts_components) * 1000)}"'
    if request.headers.get("if-none-match") == etag:
        # payload 컬럼 미열람 — egress 절감의 핵심.
        return Response(status_code=304)
    response.headers["ETag"] = etag

    # tag 불일치(실제 변경) — 이제서야 full row를 로드해 응답.
    snap = session.exec(
        select(SyncSnapshot)
        .where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    if snap is None:
        # snapshot 없지만 heartbeat 있음 — 새 가동 + 첫 cycle 전 케이스
        return SyncSnapshotOut(payload={}, received_at=last_hb,
                                device_id=None, last_heartbeat_at=last_hb)
    return SyncSnapshotOut(payload=snap.payload, received_at=snap.received_at,
                           device_id=snap.device_id, last_heartbeat_at=last_hb)


# ── 로컬앱 → 서버 Parquet 데이터 동기화 업로드 ─────────────────────────────────────

@router.post("/upload_parquet")
async def upload_parquet(
    category: str = "price",  # "price" or "fundamentals"
    file: UploadFile = File(...),
    device: Device = Depends(get_current_device),
):
    """로컬앱이 수집 완료된 .parquet 파일을 서버에 업로드하여 무결하게 적재."""
    from quant_core import data_fetcher

    filename = file.filename
    if not filename or not filename.endswith(".parquet"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="오직 .parquet 확장자의 파일만 업로드할 수 있습니다."
        )

    # 상위 경로 참조 공격(Path Traversal) 방지
    safe_name = Path(filename).name

    if category == "fundamentals":
        dest_dir = data_fetcher.FUNDAMENTALS_DIR
    else:
        dest_dir = data_fetcher.DATA_DIR

    dest_path = dest_dir / safe_name

    import io
    import anyio
    import pandas as pd
    from quant_core.parquet_io import write_parquet_atomic

    content = await file.read()

    # 1) 무결성 게이트: 업로드 바이트가 실제로 유효한 parquet인지 검증. 잘리거나 손상된
    #    업로드(네트워크 중단·로컬 파일 손상)를 디스크에 적재하지 않는다(부류 결함의 유입 차단).
    try:
        df = await anyio.to_thread.run_sync(lambda: pd.read_parquet(io.BytesIO(content)))
    except Exception as e:
        _log.warning("유효하지 않은 parquet 업로드 거부 [%s]: %s", safe_name, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"손상되었거나 유효하지 않은 parquet 파일입니다: {e}",
        )

    # 2) 원자적 저장: 재배포·OOM이 쓰기 도중 끼어들어도 최종 경로엔 완결 파일만 남는다
    #    (직접 write_bytes는 중단 시 footer 없는 잘린 파일을 남겨 load_all 전체를 깨뜨렸다).
    try:
        await anyio.to_thread.run_sync(write_parquet_atomic, df, dest_path)
    except Exception as e:
        _log.error("Parquet 저장 실패 [%s]: %s", safe_name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"서버에 Parquet 파일을 저장하는 중 오류가 발생했습니다: {e}",
        )

    _log.info("Parquet 동기화 성공: %s (%s, %d bytes) -> device:%s", safe_name, category, len(content), device.id)
    return {"ok": True, "filename": safe_name, "category": category, "size": len(content)}


@router.post("/complete")
def sync_complete(
    background_tasks: BackgroundTasks,
    device: Device = Depends(get_current_device),
):
    """로컬앱이 모든 Parquet 벌크 동기화 파일 전송을 마쳤음을 서버에 알림.
    이 시점에 단 한 번 메모리 캐시를 무효화하고 백그라운드 캐시 워밍업을 시작합니다.
    """
    from .. import data_cache
    data_cache.invalidate()
    _log.info("Parquet 벌크 동기화 완료 신호 수신. 메모리 캐시 무효화 완료 -> device:%s", device.id)

    # 백그라운드 스레드에서 캐시 로딩 및 기술적 지표 사전 계산 수행 (Cold Start 극복)
    def warmup_cache():
        try:
            _log.info("🚀 백그라운드 데이터셋 캐시 워밍업(Warm-up) 시작...")
            # raw OHLCV 공유 캐시만 워밍(전 유니버스 45컬럼 9.4GB 빌드 회피). 백테스트·preview는
            # 이 raw에서 참조 컬럼만 그때그때 프로젝션하므로 raw 워밍으로 콜드스타트가 해소된다.
            data_cache.get_raw_dataset()
            _log.info("✅ 백그라운드 데이터셋 캐시 워밍업 완료. 이제 즉시 웹에서 백테스트 가능합니다.")
        except Exception as e:
            _log.error("❌ 백그라운드 데이터셋 캐시 워밍업 실패: %s", e)

    background_tasks.add_task(warmup_cache)
    return {"ok": True}


