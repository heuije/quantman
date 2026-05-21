"""웹 ↔ 로컬앱 동기화 라우터.

안전정보만 오간다 — 전략(설정)·잔고·포지션·자산곡선·체결로그.
API키·계좌번호·원시주문은 이 경로를 절대 통과하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_device, get_current_user
from ..models import Device, Strategy, SyncSnapshot, User, UserSettings
from ..schemas import (StrategyOut, SyncPushIn, SyncSnapshotOut,
                       TradableSymbolsSyncIn)

_log = logging.getLogger("app.sync")

router = APIRouter(prefix="/sync", tags=["sync"])


# ── 로컬앱 → 서버 (기기 토큰 인증) ─────────────────────────────────────────────

@router.post("/push")
def push_snapshot(
    body: SyncPushIn,
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """로컬앱이 잔고·포지션·자산곡선·체결로그를 푸시."""
    snap = SyncSnapshot(user_id=device.user_id, device_id=device.id,
                        payload=body.payload)
    session.add(snap)
    session.commit()
    # 임계치 알림 (실패해도 본 응답엔 영향 없음)
    try:
        _check_alerts(session, device.user_id, body.payload)
    except Exception as e:
        _log.warning("알림 검사 실패: %s", e)
    return {"ok": True}


def _post_webhook(url: str, text: str) -> bool:
    """Discord/Slack 호환 형태로 알림 발송 (Discord는 content=, Slack은 text=)."""
    if not url:
        return False
    try:
        body = {"content": text, "text": text}
        r = requests.post(url, json=body, timeout=8)
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
    return [StrategyOut(id=s.id, name=s.name, run_mode=s.run_mode,
                        definition=s.definition, created_at=s.created_at,
                        updated_at=s.updated_at) for s in rows]


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

@router.get("/snapshot", response_model=SyncSnapshotOut | None)
def latest_snapshot(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """웹 대시보드가 로컬앱이 보낸 최신 스냅샷을 조회."""
    snap = session.exec(
        select(SyncSnapshot)
        .where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    if snap is None:
        return None
    return SyncSnapshotOut(payload=snap.payload, received_at=snap.received_at,
                           device_id=snap.device_id)
