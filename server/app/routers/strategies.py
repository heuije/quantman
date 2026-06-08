"""전략 CRUD + 버전 이력·현황 라우터 (Phase 59)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from quant_core import is_futures
from quant_core.ir_engine import StrategyIR, validate_strategy
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, Strategy, StrategyVersion, SyncSnapshot, User
from ..schemas import (StrategyIn, StrategyOut, StrategyRestoreIn,
                       StrategyStatsOut, StrategyVersionOut)
from ..symbols import tradable_symbols

router = APIRouter(prefix="/strategies", tags=["strategies"])

_VALID_MODES = {"draft", "paper", "live"}
_VALID_ENGINES = {"ir"}   # IR 단일 체제 — 레거시 operand 제거됨

# Phase 59 — 자동 스냅샷 회전 정책
_VERSION_MAX_KEEP = 50         # strategy당 최대 보관 버전 수
_VERSION_MAX_AGE_DAYS = 30     # 30일 이전 버전 자동 삭제


def _validate(engine: str, definition: dict) -> tuple[str, dict]:
    """definition을 StrategyIR 스키마로 검증하고 (정규화 이름, 정규화 정의) 반환.

    IR 단일 체제 — engine은 'ir'만. 구조 검증만 (데이터 가용성·무결성은 백테스트 실행이 소유).
    잘못된 정의는 422.
    """
    if engine != "ir":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "전략 엔진은 'ir'만 지원합니다 (레거시 operand 제거됨).")
    try:
        s = StrategyIR.model_validate(definition)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"전략 정의가 올바르지 않습니다: {e.errors()[0]['msg']}")
    # 논리 정합성 — 의미 공허·모순 로직(M-rules)·구조 규칙(S-rules) 등 SEV_ERROR는
    # 저장 차단(모든 모드 — 사용자 결정). 데이터 가용성(R0)은 valid_refs 없이 건너뜀
    # (백테스트 실행이 소유). 무의미한 백테스트가 저장·모의/실전으로 새는 것을 막는다.
    errors = [i for i in validate_strategy(s) if i.is_error]
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"전략 로직이 올바르지 않습니다: {errors[0].message}")
    return s.name, s.model_dump()


def _assert_live_tradable(run_mode: str, definition: dict) -> None:
    """모의/실전 승격 게이트 — 백테스트≠실거래 발산을 막는다.

    ① 레버리지(>1배): 실거래는 사용자 KIS '현금계좌'로 체결한다(로컬앱 order-cash만,
       신용거래 미지원). 엔진 레버리지는 차입/증거금을 가정하므로 현금계좌로는 체결
       불가 → 차단. 실거래로 2x 노출이 필요하면 레버리지 ETF(예: KODEX 레버리지
       122630)를 '현금 매수'하면 된다(레버리지가 ETF 가격에 내장).
    ② 비매매 유니버스: 자동매매 불가 종목(지수·매크로·합성) 또는 빈 선택(전체) →
       로컬앱이 주문할 수 없으므로 차단(반드시 매매가능 종목을 직접 선택해야 함).
    ③ 이벤트 진입 + 세부조건: 라이브 종목선별(universe.screener)은 미구현(Phase 2)
       → 차단. 백테스트는 허용.
    ④ 방향(M1 4계층 게이트): long_short(횡단 랭킹)는 라이브 단방향 체결기가 재현 못 해
       런타임이 skip하므로 차단. short는 선물 전용(현금계좌로 주식 공매도 불가) — 비선물
       숏은 차단. long/단일방향 long·short(선물)만 라이브 승격 허용.
    """
    if run_mode not in ("paper", "live"):
        return

    lev = float((definition.get("simulation") or {}).get("leverage") or 1.0)
    if lev > 1.0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "레버리지(>1배) 전략은 백테스트 전용입니다 — 모의·실전 적용 불가. "
            "실거래에서 레버리지가 필요하면 레버리지 ETF(예: KODEX 레버리지)를 현금 매수하세요.")

    direction = ((definition.get("position") or {}).get("direction")) or "long"
    if direction == "long_short":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "롱숏(횡단 랭킹) 전략은 백테스트 전용입니다 — 모의·실전 미지원. "
            "라이브 자동매매는 단일 방향(long 또는 short)만 체결합니다.")

    u = definition.get("universe") or {}
    syms = u.get("symbols") or []
    if u.get("kind") == "all" or not syms:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "모의·실전 전략은 매매할 종목을 직접 선택해야 합니다(전체 유니버스 불가).")

    # 숏은 선물 전용 — 현금계좌(로컬앱 order-cash)로는 주식 공매도가 불가하므로 비선물
    # 숏 전략은 라이브에서 발주 거부된다. 선물(만기·증거금 모델)만 sell-to-open 지원.
    if direction == "short":
        non_fut = [s for s in syms if not is_futures(s)]
        if non_fut:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "숏 전략은 선물만 모의·실전 가능합니다(현금계좌로 주식 공매도 불가): "
                f"{', '.join(non_fut[:5])}")

    ok = tradable_symbols()
    bad = [s for s in syms if s not in ok]
    if bad:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"자동매매 불가 종목이 포함돼 모의·실전으로 적용할 수 없습니다: {', '.join(bad[:5])}")

    entry_mode = ((definition.get("position") or {}).get("entry") or {}).get("mode")
    if entry_mode == "on_signal" and (u.get("screener") or {}).get("condition"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "이벤트 진입 + 세부조건 전략은 현재 백테스트 전용입니다(라이브 지원 예정).")


def _out(s: Strategy) -> StrategyOut:
    return StrategyOut(id=s.id, name=s.name, run_mode=s.run_mode, engine=s.engine,
                       definition=s.definition, created_at=s.created_at,
                       updated_at=s.updated_at,
                       paper_started_at=s.paper_started_at,
                       live_started_at=s.live_started_at)


def _own_or_404(session: Session, strategy_id: int, user_id: int) -> Strategy:
    row = session.get(Strategy, strategy_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    return row


def _clear_active_period(row: Strategy) -> None:
    """draft 강등(=정지) 시 활성기간 기준점을 초기화. 그러지 않으면 재승격 시
    stale한 started_at·기준자본으로 days_live·손익률 계산이 잘못된 기준점을 쓴다."""
    row.paper_started_at = None
    row.live_started_at = None
    row.live_capital_at_start = None


def _next_version_no(session: Session, strategy_id: int) -> int:
    cur = session.exec(
        select(StrategyVersion.version_no)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version_no.desc())
    ).first()
    return (cur or 0) + 1


def _snapshot_version(session: Session, row: Strategy, reason: str) -> None:
    """Strategy의 현재 정의를 새 버전으로 스냅샷 + 회전 정책 적용.

    호출 시점: PUT 직전 (변경 전 정의 보존) 또는 restore 직전.
    회전: 50건 초과분 또는 30일 이전 버전 삭제. flush는 호출자가.
    """
    ver = StrategyVersion(
        strategy_id=row.id, version_no=_next_version_no(session, row.id),
        name=row.name, definition=row.definition, created_reason=reason)
    session.add(ver)

    # 회전 정책: 1) 30일 이전 삭제 2) 50건 초과 시 가장 오래된 것부터 삭제
    cutoff = datetime.now(timezone.utc) - timedelta(days=_VERSION_MAX_AGE_DAYS)
    old = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == row.id,
            StrategyVersion.created_at < cutoff)).all()
    for v in old:
        session.delete(v)

    all_versions = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == row.id)
        .order_by(StrategyVersion.version_no.desc())
    ).all()
    if len(all_versions) > _VERSION_MAX_KEEP:
        for v in all_versions[_VERSION_MAX_KEEP:]:
            session.delete(v)


@router.get("", response_model=list[StrategyOut])
def list_strategies(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    rows = session.exec(select(Strategy).where(Strategy.user_id == user.id)).all()
    return [_out(s) for s in rows]


@router.post("", response_model=StrategyOut, status_code=status.HTTP_201_CREATED)
def create_strategy(
    body: StrategyIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.run_mode not in _VALID_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "run_mode가 올바르지 않습니다.")
    if body.engine not in _VALID_ENGINES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "engine이 올바르지 않습니다.")
    name, definition = _validate(body.engine, body.definition)
    _assert_live_tradable(body.run_mode, definition)
    now = datetime.now(timezone.utc)
    row = Strategy(user_id=user.id, name=name, run_mode=body.run_mode,
                   engine=body.engine, definition=definition,
                   paper_started_at=now if body.run_mode == "paper" else None,
                   live_started_at=now if body.run_mode == "live" else None)
    session.add(row)
    session.commit()
    session.refresh(row)
    # 최초 버전 스냅샷 (initial)
    initial = StrategyVersion(
        strategy_id=row.id, version_no=1, name=row.name,
        definition=row.definition, created_reason="initial")
    session.add(initial)
    session.commit()
    return _out(row)


@router.get("/{strategy_id}", response_model=StrategyOut)
def get_strategy(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _out(_own_or_404(session, strategy_id, user.id))


@router.put("/{strategy_id}", response_model=StrategyOut)
def update_strategy(
    strategy_id: int,
    body: StrategyIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = _own_or_404(session, strategy_id, user.id)
    if body.run_mode not in _VALID_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "run_mode가 올바르지 않습니다.")
    if body.engine not in _VALID_ENGINES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "engine이 올바르지 않습니다.")
    name, definition = _validate(body.engine, body.definition)
    _assert_live_tradable(body.run_mode, definition)

    # Phase 59 — 변경 전 정의를 버전으로 스냅샷 (사용자 선택: 매 PUT마다)
    _snapshot_version(session, row, reason="manual_edit")

    # run_mode 전환 timestamp 기록
    now = datetime.now(timezone.utc)
    if body.run_mode == "paper" and row.run_mode != "paper":
        row.paper_started_at = now
    if body.run_mode == "live" and row.run_mode != "live":
        row.live_started_at = now
    # draft 강등(=정지) — 활성기간 기준점 초기화(재승격 시 stale 방지).
    if body.run_mode == "draft":
        _clear_active_period(row)

    row.name = name
    row.run_mode = body.run_mode
    row.engine = body.engine
    row.definition = definition
    row.updated_at = now
    # Task 12b — 사용자 수정·전환 시 정적 라이브 바스켓을 초기화해 다음 preview에서 재형성.
    # live_basket은 서버 파생 상태 — definition·run_mode가 바뀌면 고정 집합도 다시 형성해야 한다.
    row.live_basket = None
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


@router.post("/{strategy_id}/stop", response_model=StrategyOut)
def stop_strategy(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """전략 정지 — 모의/실전 자동매매를 멈추고 draft(초안)로 내린다(비파괴 대안).

    삭제와 달리 정의·버전·백테스트를 보존한다. 보유 포지션이 있어도 안전:
    로컬앱은 다음 사이클부터 신규 진입만 멈추고, 기존 보유는 저장된 규칙으로
    계속 청산한다. 정의 변경이 아니므로 버전 스냅샷을 만들지 않는다. 멱등.
    """
    row = _own_or_404(session, strategy_id, user.id)
    if row.run_mode != "draft":
        row.run_mode = "draft"
        _clear_active_period(row)
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
    return _out(row)


@router.delete("/{strategy_id}")
def delete_strategy(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = _own_or_404(session, strategy_id, user.id)
    # 삭제 게이트 — 자동매매 중(모의/실전)인 전략은 삭제 금지. 보유 포지션이 통지
    # 없이 고아가 되는 사고의 원천을 진입점에서 차단한다. 서버는 보안원칙상 로컬
    # 실시간 보유를 모르므로, 권위 있는 단일 신호 run_mode로 게이트한다(먼저 정지).
    if row.run_mode != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "자동매매 중(모의/실전)인 전략은 삭제할 수 없습니다. 먼저 정지(초안으로 전환)한 뒤 삭제하세요.")
    # 연관 버전·백테스트 cascade 삭제
    for v in session.exec(
            select(StrategyVersion).where(
                StrategyVersion.strategy_id == strategy_id)).all():
        session.delete(v)
    for b in session.exec(
            select(BacktestRun).where(
                BacktestRun.strategy_id == strategy_id)).all():
        session.delete(b)
    session.delete(row)
    session.commit()
    return {"ok": True}


# ── Phase 59 — 버전 이력 endpoint ────────────────────────────────────────────

@router.get("/{strategy_id}/versions", response_model=list[StrategyVersionOut])
def list_versions(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """전략 버전 목록 (최신순). definition은 omit — 상세는 단일 조회로."""
    _own_or_404(session, strategy_id, user.id)
    rows = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version_no.desc())
    ).all()
    return [StrategyVersionOut(
        version_no=v.version_no, name=v.name,
        created_at=v.created_at, created_reason=v.created_reason)
        for v in rows]


@router.get("/{strategy_id}/versions/{version_no}",
            response_model=StrategyVersionOut)
def get_version(
    strategy_id: int,
    version_no: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """특정 버전의 전체 정의."""
    _own_or_404(session, strategy_id, user.id)
    v = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.version_no == version_no)).first()
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "버전을 찾을 수 없습니다.")
    return StrategyVersionOut(
        version_no=v.version_no, name=v.name,
        created_at=v.created_at, created_reason=v.created_reason,
        definition=v.definition)


@router.post("/{strategy_id}/restore", response_model=StrategyOut)
def restore_version(
    strategy_id: int,
    body: StrategyRestoreIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """특정 버전으로 현재 정의 복원. 복원 직전 현재 상태도 새 버전으로 보존."""
    row = _own_or_404(session, strategy_id, user.id)
    target = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.version_no == body.version_no)).first()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "복원할 버전을 찾을 수 없습니다.")
    # 복원 전 현재 정의 보존
    _snapshot_version(session, row, reason=f"restore_from_v{body.version_no}")
    row.name = target.name
    row.definition = target.definition
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


# ── Phase 59 — 현황·백테스트 내역 endpoint ───────────────────────────────────

def _days_between(then: datetime | None, now: datetime) -> int | None:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, (now - then).days)


@router.get("/{strategy_id}/stats", response_model=StrategyStatsOut)
def get_stats(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """전략 현황 — 적용 기간 + 최신 snapshot의 strategy_pnl에서 누적 손익 추출."""
    row = _own_or_404(session, strategy_id, user.id)
    now = datetime.now(timezone.utc)

    # 최신 snapshot에서 by_strategy 필드 추출 (전략명 매칭).
    # 로컬앱이 push하는 SyncSnapshot.payload.strategy_pnl.by_strategy[] =
    # [{strategy_name, trades, win_rate, pnl, ...}, ...]
    snap = session.exec(
        select(SyncSnapshot).where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()

    pnl_total = pnl_pct = win_rate = traded_amount = None
    n_trades = 0
    n_positions = 0
    last_snapshot_at = None
    if snap is not None:
        last_snapshot_at = snap.received_at
        payload = snap.payload or {}
        by_strat = (payload.get("strategy_pnl") or {}).get("by_strategy") or []
        for s in by_strat:
            # 로컬 analytics는 전략명을 "strategy" 키로 담는다 — 과거 "strategy_name" 혼용 대비 양쪽 허용.
            if (s.get("strategy_name") or s.get("strategy")) == row.name:
                pnl_total = s.get("pnl")
                win_rate = s.get("win_rate")
                n_trades = int(s.get("trades") or 0)
                traded_amount = s.get("traded_amount")
                break
        positions = payload.get("positions") or []
        n_positions = sum(1 for p in positions
                          if p.get("strategy_name") == row.name)
        # 손익률 — live는 live_capital_at_start, 그 외는 initial_capital
        base_capital = row.live_capital_at_start
        if base_capital and pnl_total is not None:
            pnl_pct = (pnl_total / base_capital) * 100.0

    return StrategyStatsOut(
        paper_started_at=row.paper_started_at,
        live_started_at=row.live_started_at,
        days_paper=_days_between(row.paper_started_at, now),
        days_live=_days_between(row.live_started_at, now),
        pnl_total=pnl_total, pnl_pct=pnl_pct, traded_amount=traded_amount,
        win_rate=win_rate, n_trades=n_trades,
        n_positions=n_positions, last_snapshot_at=last_snapshot_at)


@router.get("/{strategy_id}/backtests")
def list_backtests(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """이 전략으로 실행된 백테스트 내역. 요약 메트릭만, 상세는 /backtest/runs/:id."""
    _own_or_404(session, strategy_id, user.id)
    rows = session.exec(
        select(BacktestRun).where(BacktestRun.strategy_id == strategy_id)
        .order_by(BacktestRun.created_at.desc())
    ).all()
    return [
        {"id": b.id, "name": b.name, "version_no": b.version_no,
         "start": b.start, "end": b.end,
         "initial_capital": b.initial_capital,
         "metrics": (b.result or {}).get("metrics") or {},
         "created_at": b.created_at}
        for b in rows
    ]
