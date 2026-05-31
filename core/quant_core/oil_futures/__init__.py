"""WTI 원유선물 분석 모듈 (Phase 1: 백테스트 엔진 + CLI).

사용 예:
    from quant_core.oil_futures import (
        load_wti, generate_signals, run_backtest, summarize, grid_search,
    )
    df = load_wti()
    sigs = generate_signals(df, short_thresholds=[80, 90], long_thresholds=[40, 50])
    result = run_backtest(df, sigs, horizon_days=60)
    print(summarize(result))

설계 메모:
- 신호: 장중 high/low 가격이 임계값을 첫 터치(전일 비교 = hysteresis).
- 진입: 신호일 다음 영업일 시가 (look-ahead bias 제거).
- 청산: 진입 후 N영업일 후 종가 (N = horizon_days).
- 비용: 계약당 수수료 + 진입/청산 슬리피지 (CostModel 외부 주입).
- 위험지표: win_rate 외 Sharpe, MDD, profit factor, sample size 동시 산출.
"""
from .data import load_wti
from .signals import Signal, Side, generate_signals
from .backtest import (
    Trade,
    BacktestResult,
    CostModel,
    ExitRules,
    RollModel,
    WTI_TICK,
    WTI_MULTIPLIER,
    run_backtest,
    wti_expiry_dates,
)
from .metrics import Summary, LOW_SAMPLE_THRESHOLD, summarize
from .optimizer import (
    GridCell,
    WalkForwardResult,
    grid_search,
    grid_to_dataframe,
    walk_forward,
)

__all__ = [
    "load_wti",
    "Signal", "Side", "generate_signals",
    "Trade", "BacktestResult", "CostModel", "ExitRules", "RollModel",
    "WTI_TICK", "WTI_MULTIPLIER", "run_backtest", "wti_expiry_dates",
    "Summary", "LOW_SAMPLE_THRESHOLD", "summarize",
    "GridCell", "WalkForwardResult", "grid_search", "grid_to_dataframe", "walk_forward",
]
