"""펼침층 SWEEP — 백테스트를 축 위에서 반복/분할 (비전 §4).

명세 §8. Phase 1은 **조건축**: 한 번 실행한 결과를 사후에 조건 라벨로 분할한다.
예) 전략 일별 수익률을 VIX 국면(저/중/고)별로 나눠 비교("변동성 高/低 비교").

라벨은 label 블록(bucket·국면·달력)으로 만든다. 출력은 resultset(라벨→요약).
파라미터·자산축, 시간축(이벤트 스터디)은 후속(P2).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..blocks import EvalContext, Node, evaluate, select_symbol
from .backtest import run_backtest_ir
from .metrics import perf_from_returns

TRADING_DAYS = 252


def daily_returns(equity: pd.Series) -> pd.Series:
    """자산곡선 → 일별 수익률 (첫날 제외)."""
    return equity.pct_change().dropna()


def summarize_returns(r: pd.Series) -> dict:
    """일별 수익률 요약 — 모든 펼침 버킷의 단일 출처(perf_from_returns 위임).

    MDD·CAGR·Sortino·손익비·VaR/CVaR까지 동질 키로 산출해 버킷 간 비교가 가능하다.
    """
    return perf_from_returns(r)


def partition_by_label(returns: pd.Series, labels: pd.Series) -> dict:
    """수익률을 라벨로 묶는다 (라벨 NaN인 날은 제외). {label: 수익률 Series}."""
    df = pd.DataFrame({"r": returns})
    df["lab"] = labels.reindex(returns.index)
    df = df.dropna(subset=["lab"])
    out: dict = {}
    for lab, grp in df.groupby("lab"):
        out[lab] = grp["r"].dropna()
    return out


def sweep_condition(equity: pd.Series, label_series: pd.Series) -> dict:
    """자산곡선을 라벨별로 분할해 라벨→요약 resultset 반환."""
    r = daily_returns(equity)
    parts = partition_by_label(r, label_series)
    return {lab: summarize_returns(s) for lab, s in parts.items()}


def run_condition_sweep(
    dataset: dict[str, pd.DataFrame],
    trade_symbol: str,
    buy_node: Node,
    label_node: Node,
    **backtest_kw,
) -> dict:
    """전략을 1회 백테스트한 뒤 결과를 label_node 라벨로 사후 분할.

    label_node: type:label Node (예: bucket(VIX.Close, edges=[...]) → 국면).
    반환: {success, overall(요약), buckets({라벨: 요약})}.
    """
    bt = run_backtest_ir(dataset, trade_symbol, buy_node, **backtest_kw)
    if not bt.get("success"):
        return bt
    ctx = EvalContext.from_dataset(dataset, universe=[trade_symbol])
    label_series = select_symbol(evaluate(label_node, ctx), trade_symbol)
    equity = bt["equity"]
    return {
        "success": True,
        "overall": summarize_returns(daily_returns(equity)),
        "buckets": sweep_condition(equity, label_series),
    }
