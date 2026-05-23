"""포트폴리오 위험 분석 — 상관관계 매트릭스 + 섹터(카테고리) 노출.

웹의 최근 snapshot에서 보유 종목을 읽어 dataset의 가격 시계열로 상관계수 계산.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

import quant_core as qc

from ..data_cache import get_dataset
from ..db import get_session
from ..deps import get_current_user
from ..models import SyncSnapshot, User

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


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

    data = get_dataset()
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
