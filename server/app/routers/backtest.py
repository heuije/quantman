"""백테스트 · 데이터분석 라우터 (서버에서 core 엔진 실행)."""

from __future__ import annotations

import quant_core as qc
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlmodel import Session, select

from .. import kis_master_cache
from ..data_cache import get_dataset
from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, User
from ..schemas import (AnalysisIn, BacktestIn, BacktestRunOut,
                       BacktestRunSummary)
from ..serialize import serialize_analysis, serialize_backtest

router = APIRouter(tags=["backtest"])


@router.get("/symbols")
def list_symbols(user: User = Depends(get_current_user)):
    """전략 빌더용 — 두 집합의 union을 반환:
    1) KIS 마스터의 모든 매수 가능 종목 (trade_symbol 후보, tradable=True)
    2) 서버 dataset의 종목 (조건 평가/지표용, has indicators)

    교집합인 종목은 둘 다 (tradable + indicators), 나머지는 한 쪽만.
    """
    data = get_dataset()
    indic_cols = set(qc.get_all_indicator_columns())

    master_list = kis_master_cache.get_master_list()
    has_master = len(master_list) > 0
    master_by_code = {m["symbol"]: m for m in master_list}

    out = []
    seen: set[str] = set()

    def _category(market: str, kind: str) -> str:
        """카테고리 라벨 — 시장 + 유형 결합."""
        kind_label = {"stock": "주식", "etf_etn": "ETF/ETN",
                       "reits": "REITs"}.get(kind, "주식")
        region = {
            "KOSPI": "국내", "KOSDAQ": "국내",
            "NAS": "미국 NASDAQ", "NYS": "미국 NYSE", "AMS": "미국 AMEX",
            "TSE": "일본", "HKS": "홍콩",
        }.get(market, "")
        if market in ("KOSPI", "KOSDAQ"):
            return f"국내{kind_label} ({market})"
        return f"{region} {kind_label}".strip()

    # 1) dataset 종목 (지표 평가 가능). 마스터에도 있으면 tradable.
    for sym, df in sorted(data.items()):
        cols = [c for c in df.columns if c in indic_cols]
        has_ohlc = {"Open", "Close"}.issubset(df.columns)
        in_master = sym in master_by_code
        meta = master_by_code.get(sym, {})
        kind = meta.get("kind", "stock")
        out.append({
            "symbol": sym,
            "name": meta.get("name", ""),
            "category": (_category(meta.get("market", ""), kind) if in_master
                          else qc.symbol_category(sym)),
            "tradable": in_master and has_ohlc,
            "has_backtest_data": has_ohlc,
            "kind": kind if in_master else None,
            "rows": len(df),
            "indicators": [{
                "key": c,
                "label": qc.get_indicator_label(c),
                "group": qc.get_indicator_group(c),
                "unit": qc.get_indicator_unit(c),
                "compare_group": qc.get_indicator_compare_group(c),
            } for c in cols],
        })
        seen.add(sym)

    # 2) 마스터에는 있지만 dataset에 없는 종목 — 라이브 매매만 가능 (지표 없음)
    for code, meta in master_by_code.items():
        if code in seen:
            continue
        kind = meta.get("kind", "stock")
        out.append({
            "symbol": code,
            "name": meta.get("name", ""),
            "category": _category(meta.get("market", ""), kind),
            "tradable": True,
            "has_backtest_data": False,
            "kind": kind,
            "rows": 0,
            "indicators": [],
        })

    return {"symbols": out, "has_master": has_master,
            "master_status": kis_master_cache.get_status()}


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
