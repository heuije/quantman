"""quant_core — 백테스트·분석·전략 공유 패키지."""

from .strategy import (Condition, ConditionGroup, ExecutionPolicy, ExitRules,
                       Modifier, Operand, SellRules, Strategy,
                       SELF_SYMBOL, is_self_ref,
                       parse_trade_symbols, sell_pct_for_reason)
from .dataset import load_dataset
from .data_fetcher import symbol_category
from .engine import run_strategy_backtest, evaluate_buy_signal
from .analysis import (run_analysis, run_temporal_stability, build_signal_mask,
                        explain_buy_signal, explain_buy_signal_per_symbol,
                        describe_condition)
from .backtest import run_backtest
from .indicators import (compute_all, get_indicator_columns, get_indicator_group,
                         get_indicator_label, get_indicator_unit,
                         get_indicator_compare_group, get_all_indicator_columns)
from .exec_defaults import (DEFAULT_EXECUTION, KRW_DAILY_LIMIT_PCT,
                            apply_daily_price_limit, merged_execution,
                            round_to_tick, tick_size)

__all__ = [
    "Condition", "ConditionGroup", "ExecutionPolicy", "ExitRules", "Modifier",
    "Operand", "SellRules", "Strategy", "SELF_SYMBOL", "is_self_ref",
    "parse_trade_symbols", "sell_pct_for_reason",
    "load_dataset", "symbol_category", "run_strategy_backtest", "evaluate_buy_signal",
    "run_analysis", "run_temporal_stability", "build_signal_mask",
    "explain_buy_signal", "explain_buy_signal_per_symbol", "describe_condition",
    "run_backtest", "compute_all", "get_indicator_columns", "get_indicator_group",
    "get_indicator_label", "get_indicator_unit", "get_indicator_compare_group",
    "get_all_indicator_columns",
    "DEFAULT_EXECUTION", "merged_execution", "round_to_tick", "tick_size",
    "apply_daily_price_limit", "KRW_DAILY_LIMIT_PCT",
]
