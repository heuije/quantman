"""블록 IR 백테스트/분석 엔진.

명세 §7~§9·§12. 1회 백테스트(backtest)·펼침(sweep)·비교검증(compare)을 담는다.
기존 quant_core.backtest(레거시 4-path)와 quant_core.engine(레거시 어댑터)을
단계적으로 대체한다. 전환 완료 후 이 패키지를 engine으로 통합 예정
(현재는 레거시 engine.py와의 이름 충돌을 피해 ir_engine으로 분리).
"""

from .backtest import run_backtest_ir  # noqa: F401
from .run import (  # noqa: F401
    run_describe_report, run_period_split, run_portfolio_diagnosis, run_query,
    run_select, run_strategy_ir, run_sweep,
)
from .compare import (  # noqa: F401
    block_bootstrap_ci, bootstrap_mean_ci, compare_partition, distribution,
    excess_distribution, jackknife_by_year, one_sample_test, two_sample_test,
    walk_forward_consistency,
)
from .compose import (  # noqa: F401
    collect_strat_refs, has_strat_refs, materialize_strategy_assets,
)
from .capabilities import capability_spec  # noqa: F401
from .explain import explain_ir  # noqa: F401
from .service import backtest_from_spec, strategy_from_spec  # noqa: F401
from .spec import (  # noqa: F401
    Entry, Exit, Overlays, ParamAxis, PositionSpec, SelectSpec, Sizing, SimSpec,
    StrategyIR, Study, Universe, field_contract, needed_columns, needed_symbols,
    signal_out_type, validate_strategy,
)
from .sweep import (  # noqa: F401
    daily_returns, partition_by_label, run_condition_sweep, summarize_returns,
    sweep_condition,
)

__all__ = [
    "run_backtest_ir", "run_strategy_ir", "run_sweep", "run_period_split", "run_query", "run_select",
    "run_describe_report", "run_portfolio_diagnosis",
    "backtest_from_spec", "strategy_from_spec", "capability_spec", "explain_ir",
    "collect_strat_refs", "has_strat_refs", "materialize_strategy_assets",
    "StrategyIR", "Universe", "PositionSpec", "Sizing", "Entry", "Exit", "SelectSpec",
    "Overlays", "SimSpec", "Study", "ParamAxis", "validate_strategy", "signal_out_type",
    "needed_symbols", "needed_columns", "field_contract",
    "run_condition_sweep", "sweep_condition", "partition_by_label",
    "summarize_returns", "daily_returns",
    "two_sample_test", "bootstrap_mean_ci", "block_bootstrap_ci", "jackknife_by_year",
    "walk_forward_consistency", "distribution", "compare_partition", "excess_distribution",
]
