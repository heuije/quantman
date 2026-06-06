"""quant_core — 백테스트·분석·전략 공유 패키지.

전략 표현·실행은 통합 IR 엔진(``quant_core.ir_engine``)과 조건 평가기
(``quant_core.blocks``)로 일원화됐다. 이들은 서브모듈에서 직접 임포트한다
(예: ``from quant_core.ir_engine import StrategyIR``).
"""

from .dataset import load_dataset, load_dataset_for
from .data_fetcher import symbol_category, ALL_SYMBOLS
from .indicators import (compute_all, get_indicator_columns, get_indicator_group,
                         get_indicator_label, get_indicator_unit,
                         get_indicator_compare_group, get_all_indicator_columns)
from .exec_defaults import (DEFAULT_EXECUTION, KRW_DAILY_LIMIT_PCT,
                            apply_daily_price_limit, is_futures, merged_execution,
                            round_to_tick, tick_size)

__all__ = [
    "load_dataset", "load_dataset_for", "symbol_category", "ALL_SYMBOLS",
    "compute_all", "get_indicator_columns", "get_indicator_group",
    "get_indicator_label", "get_indicator_unit", "get_indicator_compare_group",
    "get_all_indicator_columns",
    "DEFAULT_EXECUTION", "merged_execution", "round_to_tick", "tick_size", "is_futures",
    "apply_daily_price_limit", "KRW_DAILY_LIMIT_PCT",
]
