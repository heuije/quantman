"""백테스트 · 데이터분석 라우터 (서버에서 core 엔진 실행)."""

from __future__ import annotations

import quant_core as qc
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from ..data_cache import get_dataset
from ..deps import get_current_user
from ..models import User
from ..schemas import AnalysisIn, BacktestIn
from ..serialize import serialize_analysis, serialize_backtest

router = APIRouter(tags=["backtest"])


@router.get("/symbols")
def list_symbols(user: User = Depends(get_current_user)):
    """전략 빌더용 — 심볼별 사용 가능한 지표 컬럼."""
    data = get_dataset()
    indic_cols = set(qc.get_indicator_columns())
    out = []
    for sym, df in sorted(data.items()):
        cols = [c for c in df.columns if c in indic_cols]
        out.append({
            "symbol": sym,
            "tradable": {"Open", "Close"}.issubset(df.columns),
            "rows": len(df),
            "indicators": [{"key": c, "label": qc.get_indicator_label(c)} for c in cols],
        })
    return {"symbols": out}


@router.post("/backtest/run")
def run_backtest(body: BacktestIn, user: User = Depends(get_current_user)):
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
    return serialize_backtest(result)


@router.post("/analysis/run")
def run_analysis(body: AnalysisIn, user: User = Depends(get_current_user)):
    result = qc.run_analysis(
        get_dataset(), body.conditions, body.logic,
        body.target_symbol, body.target_indicator,
        forward_days=body.forward_days, lookback_years=body.lookback_years,
    )
    return serialize_analysis(result)
