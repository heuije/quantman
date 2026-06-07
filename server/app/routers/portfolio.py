"""포트폴리오 위험 분석 — 상관관계 매트릭스 + 섹터(카테고리) 노출.

웹의 최근 snapshot에서 보유 종목을 읽어 dataset의 가격 시계열로 상관계수 계산.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

import quant_core as qc

from ..db import get_session
from ..deps import get_current_user
from ..models import SyncSnapshot, User

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


# ── 포트폴리오 비교 분석 (현재 vs 예상) — on-demand fetch ─────────────────────
# 벤치마크 FDR 심볼(yahoo 형식): ^KS11=코스피, ^KQ11=코스닥, ^GSPC=S&P500, ^IXIC=나스닥.

class PositionIn(BaseModel):
    symbol: str
    weight: float = Field(gt=0)            # 비중(상대값 — 내부에서 합=1로 정규화)


class PortfolioIn(BaseModel):
    label: str = ""
    positions: list[PositionIn] = Field(default_factory=list)


class AnalyzeIn(BaseModel):
    current: PortfolioIn | None = None     # 미연동 시 None — 예상만 분석
    proposed: PortfolioIn | None = None
    benchmark: str = "^KS11"                # FDR 지수 심볼(^KS11/^KQ11/^GSPC/^IXIC)
    years: int = Field(default=2, ge=1, le=5)


def _fetch_closes(symbols: list[str], start: str):
    """심볼별 종가 시계열을 on-demand fetch (FDR). 실패·빈 종목은 skip."""
    import FinanceDataReader as fdr
    out = {}
    for sym in symbols:
        s = sym.strip().upper()
        if not s or s in out:
            continue
        try:
            raw = fdr.DataReader(s, start)
        except Exception:
            continue
        if raw is not None and not raw.empty and "Close" in raw.columns:
            out[s] = raw["Close"]
    return out


def _analyze_one(pf: PortfolioIn, bench_ret, start: str) -> dict | None:
    """포트폴리오 1개 → 가중 일별수익률 → 성과지표 + 베타 + 벤치마크 초과수익."""
    import numpy as np
    import pandas as pd
    from quant_core.ir_engine.metrics import perf_from_returns

    if not pf or not pf.positions:
        return None
    closes = _fetch_closes([p.symbol for p in pf.positions], start)
    if not closes:
        return None
    px = pd.DataFrame(closes).dropna()
    if len(px) < 30:
        return None
    rets = px.pct_change().dropna()
    # 비중 정규화 (fetch 성공 종목만)
    w = {p.symbol.strip().upper(): p.weight for p in pf.positions
         if p.symbol.strip().upper() in closes}
    tot = sum(w.values()) or 1.0
    wn = {k: v / tot for k, v in w.items()}
    port_ret = sum(rets[k] * wn[k] for k in wn)

    perf = perf_from_returns(port_ret)
    # 베타 (vs 벤치마크 일별수익률, 공통 날짜)
    beta = None
    excess = None
    eq = (1.0 + port_ret).cumprod()
    bench_curve = None
    if bench_ret is not None and len(bench_ret) > 2:
        idx = port_ret.index.intersection(bench_ret.index)
        if len(idx) > 2:
            bv = bench_ret.reindex(idx)
            pv = port_ret.reindex(idx)
            if bv.var() and bv.var() > 0:
                beta = float(np.cov(pv, bv)[0, 1] / bv.var())
            beq = (1.0 + bv).cumprod()
            peq = (1.0 + pv).cumprod()
            excess = float((peq.iloc[-1] - beq.iloc[-1]) * 100)  # 누적 초과수익(%p)
            bench_curve = beq

    def _pt(series):
        return [{"date": (i.date().isoformat() if hasattr(i, "date") else str(i)),
                 "value": round(float(v), 4)} for i, v in series.items()]

    return {
        "label": pf.label or "포트폴리오",
        "symbols": list(wn.keys()),
        "weights": {k: round(v * 100, 2) for k, v in wn.items()},
        "metrics": {
            "cagr": _r(perf.get("cagr")),
            "vol": _r(perf.get("std")),                 # 일별 변동성(%)
            "sharpe": _r(perf.get("sharpe")),
            "mdd": _r(perf.get("mdd")),
            "cum_return": _r(perf.get("cum_return")),
            "var_95": _r(perf.get("var_95")),
            "beta": _r(beta),
            "excess_return": _r(excess),
        },
        "equity": _pt(eq),
        "benchmark_equity": _pt(bench_curve) if bench_curve is not None else None,
    }


def _r(v, nd: int = 2):
    import math as _m
    return None if v is None or (isinstance(v, float) and _m.isnan(v)) else round(float(v), nd)


@router.post("/analyze")
def analyze_portfolio(body: AnalyzeIn, user: User = Depends(get_current_user)):
    """현재·예상 포트폴리오를 벤치마크 대비 베타·위험·초과수익으로 비교.

    서버 dataset에 의존하지 않고 종목·벤치마크를 FDR로 on-demand fetch (light mode 동작).
    current가 None이면 예상 포트폴리오만 분석(증권사 미연동 케이스).
    """
    from datetime import date, timedelta

    if not body.current and not body.proposed:
        raise HTTPException(status_code=400, detail="분석할 포트폴리오가 없습니다.")
    start = (date.today() - timedelta(days=365 * body.years + 40)).isoformat()
    bench_closes = _fetch_closes([body.benchmark], start)
    bench_ret = None
    if bench_closes:
        bs = next(iter(bench_closes.values()))
        bench_ret = bs.pct_change().dropna()

    current = _analyze_one(body.current, bench_ret, start) if body.current else None
    proposed = _analyze_one(body.proposed, bench_ret, start) if body.proposed else None
    if current is None and proposed is None:
        raise HTTPException(status_code=400,
                            detail="유효한 가격 데이터를 가진 종목이 없습니다.")
    return {"benchmark": body.benchmark, "years": body.years,
            "current": current, "proposed": proposed}


@router.get("/risk")
def portfolio_risk(window: int = 60,
                   user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    """보유 종목 간 일별 수익률 상관계수 매트릭스 + 섹터(카테고리) 노출.

    window: 상관계수 계산에 사용할 최근 거래일 수 (default 60).
    """
    snap = session.exec(
        select(SyncSnapshot)
        .where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    if snap is None:
        return {"positions": [], "matrix": [], "sectors": []}

    positions = (snap.payload or {}).get("positions") or []
    if not positions:
        return {"positions": [], "matrix": [], "sectors": []}

    # 보유 종목 Close만 필요(상관계수·평가금액) — 보유분만 부분집합 로드(전 유니버스 빌드 회피).
    held = [p.get("symbol") for p in positions if p.get("symbol")]
    data = qc.load_dataset_for(held, with_indicators=False)
    valid: list[tuple[str, list[float], float]] = []
    # (symbol, ret_window, eval_value)
    for p in positions:
        sym = p.get("symbol")
        if not sym or sym not in data:
            continue
        df = data[sym]
        if "Close" not in df.columns or len(df) < window + 2:
            continue
        closes = df["Close"].tail(window + 1).tolist()
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes)) if closes[i - 1]]
        eval_v = float(p.get("eval_price", 0) or 0) * float(p.get("qty", 0) or 0)
        valid.append((sym, rets, eval_v))

    syms = [s for s, _, _ in valid]
    matrix = []
    for i, (_a, ra, _) in enumerate(valid):
        row = []
        for j, (_b, rb, _) in enumerate(valid):
            if i == j:
                row.append(1.0)
            else:
                row.append(_corr(ra, rb))
        matrix.append(row)

    # 섹터 노출 = 카테고리 기준 평가금액 분포
    sector_amt: dict[str, float] = {}
    for sym, _, amt in valid:
        cat = qc.symbol_category(sym) or "기타"
        sector_amt[cat] = sector_amt.get(cat, 0.0) + amt
    total = sum(sector_amt.values()) or 1.0
    sectors = sorted(
        [{"label": k, "amount": round(v, 0), "share_pct": round(v / total * 100, 2)}
         for k, v in sector_amt.items()],
        key=lambda x: -x["share_pct"])

    return {
        "positions": syms,
        "matrix": [[round(c, 3) for c in row] for row in matrix],
        "sectors": sectors,
        "window": window,
    }


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a = a[-n:]; b = b[-n:]
    ma = sum(a) / n; mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)
