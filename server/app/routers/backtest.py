"""백테스트 · 데이터분석 라우터 (서버에서 core 엔진 실행)."""

from __future__ import annotations

import quant_core as qc
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlmodel import Session, select

from ..data_cache import get_dataset
from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, TradableSymbol, User
from ..schemas import (AnalysisIn, BacktestIn, BacktestRunOut,
                       BacktestRunSummary)
from ..serialize import serialize_analysis, serialize_backtest

router = APIRouter(tags=["backtest"])


@router.get("/symbols")
def list_symbols(user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    """전략 빌더용 — 심볼별 사용 가능한 지표 컬럼.

    `tradable=True` 판정:
    1. 로컬앱이 KIS 종목마스터를 push한 경우 → 그 화이트리스트와 교집합
    2. fallback (마스터 sync 전) → 한국 주식 6자리 숫자 코드만 통과
       (미국 ETF·외환·매크로 지표 등 KIS에서 매수 불가능한 것들 차단)
    """
    data = get_dataset()
    indic_cols = set(qc.get_all_indicator_columns())

    master_rows = session.exec(
        select(TradableSymbol.symbol).where(TradableSymbol.user_id == user.id)
    ).all()
    master_set = {s for s in master_rows}
    has_master = len(master_set) > 0

    out = []
    for sym, df in sorted(data.items()):
        cols = [c for c in df.columns if c in indic_cols]
        has_ohlc = {"Open", "Close"}.issubset(df.columns)
        if has_master:
            tradable = sym in master_set and has_ohlc
        else:
            # fallback: 한국 주식 6자리 숫자 코드만 통과
            # (KIS는 한국 주식·ETF가 대부분 6자리. AAPL/^GSPC 등 외국 코드는 자동 제외)
            is_kr_code = len(sym) == 6 and sym.isdigit()
            tradable = has_ohlc and is_kr_code
        out.append({
            "symbol": sym,
            "category": qc.symbol_category(sym),
            "tradable": tradable,
            "rows": len(df),
            "indicators": [{
                "key": c,
                "label": qc.get_indicator_label(c),
                "group": qc.get_indicator_group(c),
                "unit": qc.get_indicator_unit(c),
                "compare_group": qc.get_indicator_compare_group(c),
            } for c in cols],
        })
    return {"symbols": out, "has_master": has_master}


@router.post("/backtest/run")
def run_backtest(body: BacktestIn,
                 user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    try:
        strategy = qc.Strategy(**body.strategy)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"전략 정의 오류: {e.errors()[0]['msg']}")
    result = qc.run_strategy_backtest(
        strategy, get_dataset(),
        initial_capital=body.initial_capital,
        start=body.start, end=body.end,
    )
    payload = serialize_backtest(result)

    # 실행 내역 자동 저장 — 응답에 run_id를 포함해 클라이언트가 단일 결과 페이지로 이동 가능
    run = BacktestRun(
        user_id=user.id,
        name=strategy.name,
        definition=body.strategy,
        result=payload,
        initial_capital=body.initial_capital,
        start=body.start,
        end=body.end,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    payload["run_id"] = run.id
    payload["run_created_at"] = run.created_at.isoformat()
    return payload


@router.get("/backtest/runs", response_model=list[BacktestRunSummary])
def list_backtest_runs(user: User = Depends(get_current_user),
                       session: Session = Depends(get_session)):
    rows = session.exec(
        select(BacktestRun)
        .where(BacktestRun.user_id == user.id)
        .order_by(BacktestRun.created_at.desc())
        .limit(50)
    ).all()
    return [BacktestRunSummary(
        id=r.id, name=r.name, created_at=r.created_at,
        initial_capital=r.initial_capital,
        metrics=r.result.get("metrics", {}) if isinstance(r.result, dict) else {},
        success=bool(r.result.get("success", False)) if isinstance(r.result, dict) else False,
    ) for r in rows]


@router.get("/backtest/runs/{run_id}", response_model=BacktestRunOut)
def get_backtest_run(run_id: int,
                     user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    row = session.get(BacktestRun, run_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "실행 내역을 찾을 수 없습니다.")
    return BacktestRunOut(
        id=row.id, name=row.name, initial_capital=row.initial_capital,
        start=row.start, end=row.end, created_at=row.created_at,
        definition=row.definition, result=row.result,
    )


@router.delete("/backtest/runs/{run_id}")
def delete_backtest_run(run_id: int,
                        user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    row = session.get(BacktestRun, run_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "실행 내역을 찾을 수 없습니다.")
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/analysis/run")
def run_analysis(body: AnalysisIn, user: User = Depends(get_current_user)):
    result = qc.run_analysis(
        get_dataset(), body.conditions, body.logic,
        body.target_symbol, body.target_indicator,
        forward_days=body.forward_days, lookback_years=body.lookback_years,
    )
    return serialize_analysis(result)
