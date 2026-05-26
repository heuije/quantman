"""백테스트 · 데이터분석 라우터 (서버에서 core 엔진 실행)."""

from __future__ import annotations

import quant_core as qc
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlmodel import Session, select

from .. import data_cache, kis_master_cache
from ..data_cache import get_dataset
from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, User
from ..schemas import (AnalysisIn, BacktestIn, BacktestRunOut,
                       BacktestRunSummary)
from ..serialize import serialize_analysis, serialize_backtest

router = APIRouter(tags=["backtest"])


# /symbols 응답 캐시 — (dataset 버전, 마스터 갱신시각) 키로 1회 빌드·직렬화 후 재사용.
# 데이터가 실제로 바뀔 때(=키 변동)만 재빌드되므로 하루 몇 번 갱신돼도 항상 최신.
# 큰 페이로드라 dict가 아닌 인코딩된 bytes를 캐시해 재직렬화 비용까지 없앤다.
_symbols_cache: tuple[tuple[int, int], bytes] | None = None


def _symbols_version_key() -> tuple[int, int]:
    return (data_cache.get_version(), kis_master_cache.get_fetched_epoch())


def _build_symbols_payload() -> dict:
    """빌더용 종목 union을 만든다. 비용이 커서 _symbols_cache로 결과를 재사용한다.

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
        # 클래스주 심볼로지: dataset은 대시(BRK-B), 마스터는 슬래시(BRK/B) →
        # 정규화 조회해야 매칭(안 하면 Berkshire 등이 tradable=False가 됨).
        meta = master_by_code.get(sym) or master_by_code.get(sym.replace("-", "/")) or {}
        in_master = bool(meta)
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
        # §4.8: 미국은 데이터 보유분(S&P500, dataset)만 selectable로 노출한다.
        # 마스터에만 있고 데이터 없는 미국 종목(~1만+)을 selectable로 두면
        # 사용자가 골라도 자동매매가 skip_no_data로 조용히 건너뛰어 혼란 → 제외.
        if meta.get("market") in ("NAS", "NYS", "AMS"):
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


@router.get("/symbols")
def list_symbols(request: Request, user: User = Depends(get_current_user)):
    """전략 빌더용 종목 목록. 데이터 변경 시점에만 재빌드되는 캐시 + ETag.

    같은 데이터에 대해선 서버가 재계산·재직렬화를 건너뛰고, 브라우저는
    If-None-Match가 일치하면 304(본문 없음)로 받아 전송 비용도 사라진다.
    """
    global _symbols_cache
    key = _symbols_version_key()
    if _symbols_cache is None or _symbols_cache[0] != key:
        body = JSONResponse(_build_symbols_payload()).body
        _symbols_cache = (key, body)
    body = _symbols_cache[1]

    etag = f'W/"symbols-{key[0]}-{key[1]}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("/backtest/run")
def run_backtest(body: BacktestIn,
                 user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    try:
        strategy = qc.Strategy(**body.strategy)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"전략 정의 오류: {e.errors()[0]['msg']}")
    # Phase 57-B — screener:preset_key 자동선택은 PRESETS의 spec으로 resolve.
    # "screener:custom"이면 strategy.screener_spec(사용자 입력) 그대로 사용.
    ts = strategy.trade_symbol or ""
    if ts.startswith("screener:") and not strategy.screener_spec:
        key = ts[len("screener:"):]
        from ..screener import PRESETS
        preset = PRESETS.get(key)
        if preset and isinstance(preset.get("spec"), dict):
            strategy = strategy.model_copy(update={"screener_spec": preset["spec"]})
        elif key != "custom":
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"알 수 없는 screener preset: '{key}'")
    result = qc.run_strategy_backtest(
        strategy, get_dataset(),
        initial_capital=body.initial_capital,
        start=body.start, end=body.end,
    )
    payload = serialize_backtest(result)

    # Phase 59 — strategy_id가 있을 때만 저장 (orphan 백테스트 즉시 삭제 정책).
    # 빌더에서 임시 실행은 응답만 반환, DB row 안 만듦.
    if body.strategy_id is not None:
        # 사용자 본인 소유 검증
        from ..models import Strategy as _Strategy
        s = session.get(_Strategy, body.strategy_id)
        if s is None or s.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "지정한 전략을 찾을 수 없습니다.")
        run = BacktestRun(
            user_id=user.id,
            strategy_id=body.strategy_id,
            version_no=body.version_no,
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
def list_backtest_runs(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    # S-08 — 하드코딩 limit=50 제거. 기본은 호환 유지(50), 클라이언트가
    # limit/offset으로 페이지네이션 가능. 최대 200으로 캡(서버 보호).
    limit: int = Query(50, ge=1, le=200, description="페이지당 최대 항목 수"),
    offset: int = Query(0, ge=0, description="건너뛸 항목 수(페이지네이션)"),
):
    rows = session.exec(
        select(BacktestRun)
        .where(BacktestRun.user_id == user.id)
        .order_by(BacktestRun.created_at.desc())
        .offset(offset)
        .limit(limit)
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
