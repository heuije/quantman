"""웹 ↔ 로컬앱 동기화 라우터.

안전정보만 오간다 — 전략(설정)·잔고·포지션·자산곡선·체결로그.
API키·계좌번호·원시주문은 이 경로를 절대 통과하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends
from sqlalchemy import delete
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_device, get_current_user
from ..models import (Device, Strategy, SyncSnapshot, TradableSymbol, User,
                      UserSettings)
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

    # 1. Kill switch 활성 알림
    ks = (payload or {}).get("kill_switch") or {}
    if s.alert_on_killswitch and ks.get("active"):
        last = s.last_alerted_killswitch
        if last is None or now - last > cooldown:
            ok = _post_webhook(
                s.alert_webhook_url,
                f"[Quant 알림] Kill Switch 활성 — 사유: {ks.get('reason', '(없음)')}")
            if ok:
                s.last_alerted_killswitch = now
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


@router.post("/tradable_symbols")
def push_tradable_symbols(
    body: TradableSymbolsSyncIn,
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """로컬앱이 KIS 종목마스터를 push. 사용자 단위로 전체 교체(snapshot 방식).

    /symbols API가 이 화이트리스트와 데이터셋 교집합으로 tradable=True를 판정.
    """
    session.exec(delete(TradableSymbol)
                 .where(TradableSymbol.user_id == device.user_id))
    now = datetime.now(timezone.utc)
    for s in body.symbols:
        if not s.symbol:
            continue
        session.add(TradableSymbol(user_id=device.user_id, symbol=s.symbol,
                                    name=s.name, market=s.market, updated_at=now))
    session.commit()
    return {"ok": True, "n": len(body.symbols)}


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
