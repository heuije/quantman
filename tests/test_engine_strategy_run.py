"""S2 — 팩터/리밸런스 엔진 + 디스패치 회귀.

명세 §7·§3.3. 횡단·포트폴리오·롱숏 전략이 실제 백테스트로 실행되는지,
포지션 4부품(방향·사이징·top_n)이 가중치에 반영되는지 고정한다.

    cd platform && pytest tests/test_engine_strategy_run.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, Overlays, PositionSpec, Sizing, SimSpec, StrategyIR, Study, Universe,
    run_backtest_ir, run_query, run_strategy_ir, run_sweep,
)
# 선택·사이징 단위 테스트는 통합 엔진(engine.py)으로 이전 — test_engine_unified.py 참조.


# ── 통합: 팩터 백테스트 ───────────────────────────────────────────────────────

def _multi_data():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")

    def mk(daily_drift, mom):
        close = 100 * (1 + daily_drift) ** np.arange(252)
        return pd.DataFrame({
            "Open": close, "High": close * 1.001, "Low": close * 0.999,
            "Close": close, "Volume": 1e6,
            "momentum_12_1m": float(mom),
            "ma_dev_20d": np.where(np.arange(252) % 2 == 0, 1.0, -1.0),
        }, index=idx)

    return {"AAA": mk(0.003, 10.0), "BBB": mk(-0.001, -5.0), "CCC": mk(0.0, -3.0)}


def test_factor_long_top1_picks_winner():
    """모멘텀 상위1 롱 → 상승 종목(AAA) 선택 → 벤치마크 초과."""
    s = StrategyIR(
        signal=data("momentum_12_1m"),                 # score 패널
        universe=Universe(kind="all"),
        position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
        simulation=SimSpec(initial_capital=1e7),
    )
    res = run_strategy_ir(s, _multi_data())
    assert res["success"], res.get("error")
    m = res["metrics"]
    assert m["total_return"] > m["bench_total"]        # 승자 집중 > 동일가중
    assert "turnover" in m


def test_factor_long_short_profits_from_spread():
    """롱숏 상위1/하위1 → 승자 롱 + 패자 숏 둘 다 이익."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long_short",
                              entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
        simulation=SimSpec(initial_capital=1e7),
    )
    res = run_strategy_ir(s, _multi_data())
    assert res["success"]
    assert res["metrics"]["total_return"] > 0          # 스프레드 수익


def test_engine_rejects_unsupported_signal_type():
    """엔진 불변식 — 미지원 신호 타입을 validate 없이도 명시 거부(조용히 둔갑 안 함)."""
    d = _multi_data()
    # label(bucket) 신호 + 리밸런스 → condition|score 아님 → 거부(라벨코드를 점수로 둔갑 금지)
    label_sig = Node(op="bucket", params={"edges": [0.0]},
                     inputs={"signal": data("momentum_12_1m")})
    res = run_strategy_ir(
        StrategyIR(signal=label_sig, universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled", rebalance="monthly", top_n=1))), d)
    assert not res["success"] and "condition 또는 score" in res.get("error", "")
    # score 신호 + on_signal(이벤트) → condition 아님 → 거부(bool 강제 금지)
    res2 = run_strategy_ir(
        StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="single", symbols=["AAA"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"))), d)
    assert not res2["success"] and "condition" in res2.get("error", "")


def test_engine_rejects_unsupported_root_boundaries():
    """엔진 불변식 — signal 외 루트 경계도 validate 없이 직접 거부(명세 §5.4).

    스크리너 filter·매도조건·그룹라벨·펼침 라벨/이벤트에 잘못된 out_type을 꽂으면
    엔진이 조용히 캐스팅하지 않고 명시 거부(검증 우회·직접 호출에도 안전).
    """
    d = _multi_data()
    score = data("momentum_12_1m")                                   # out_type=score
    label = Node(op="bucket", params={"edges": [0.0]}, inputs={"signal": score})  # out_type=label
    sched = Entry(mode="scheduled", rebalance="monthly", top_n=1)

    # 매도조건에 score → 거부 (condition 요구)
    r = run_strategy_ir(StrategyIR(
        signal=score, universe=Universe(kind="all"),
        position=PositionSpec(entry=sched, exit=Exit(condition=score))), d)
    assert not r["success"] and "매도 조건" in r.get("error", "")

    # 그룹 노출 라벨에 score → 거부 (label 요구)
    r = run_strategy_ir(StrategyIR(
        signal=score, universe=Universe(kind="all"),
        position=PositionSpec(entry=sched,
                              overlays=Overlays(max_group_pct=30.0, group_label=score))), d)
    assert not r["success"] and "그룹 노출 라벨" in r.get("error", "")

    # 스크리너 조건에 score → 거부 (condition 요구)
    r = run_strategy_ir(StrategyIR(
        signal=score, universe=Universe(kind="list", symbols=["AAA", "BBB", "CCC"],
                                        screener={"condition": score.model_dump()}),
        position=PositionSpec(entry=sched)), d)
    assert not r["success"] and "스크리너 조건" in r.get("error", "")

    # 펼침 분할 라벨에 score → 거부 (label 요구), label이면 통과
    r = run_sweep(StrategyIR(
        signal=score, universe=Universe(kind="all"), position=PositionSpec(entry=sched),
        query="simulate", study=Study(axis="label", reduction="contrast", label=score)), d)
    assert not r["success"] and "펼침 분할 라벨" in r.get("error", "")

    # 펼침 이벤트에 score → 거부 (condition 요구)
    r = run_query(StrategyIR(
        signal=score, universe=Universe(kind="all"), position=PositionSpec(entry=sched),
        query="relate", study=Study(event=score, windows=[5])), d)
    assert not r["success"] and "펼침 이벤트" in r.get("error", "")

    # label을 올바로 꽂으면 펼침 통과 (positive control)
    r = run_sweep(StrategyIR(
        signal=score, universe=Universe(kind="all"), position=PositionSpec(entry=sched),
        query="simulate", study=Study(axis="label", reduction="contrast", label=label)), d)
    assert r["success"]


def test_factor_condition_scheduled_equal_weight():
    """condition 신호 + 정기리밸런싱 → 참인 종목 동일가중 보유."""
    s = StrategyIR(
        signal=Node(op="compare", params={"op": ">"},
                    inputs={"left": data("ma_dev_20d"), "right": const(0.0)}),
        universe=Universe(kind="all"),
        position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="scheduled", rebalance="weekly")),
        simulation=SimSpec(initial_capital=1e7),
    )
    res = run_strategy_ir(s, _multi_data())
    assert res["success"]
    assert res["equity"].iloc[-1] > 0


# ── 디스패치: on_signal 단일종목 == run_backtest_ir ───────────────────────────

def test_dispatch_on_signal_single():
    idx = pd.date_range("2020-01-01", periods=200, freq="B")
    r = np.random.default_rng(5)
    close = np.maximum(100 + np.cumsum(r.normal(0.05, 1.2, 200)), 5.0)
    d = {"005930": pd.DataFrame({
        "Open": np.r_[close[0], close[:-1]], "High": close * 1.01,
        "Low": close * 0.99, "Close": close, "Volume": r.uniform(1e5, 1e6, 200),
        "ma_dev_20d": r.uniform(-5, 5, 200),
    }, index=idx)}
    cond = Node(op="compare", params={"op": ">"},
                inputs={"left": data("__SELF__.ma_dev_20d"), "right": const(0.0)})
    s = StrategyIR(signal=cond, universe=Universe(kind="single", symbols=["005930"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"),
                                         exit=Exit(hold_days=10)),
                   simulation=SimSpec(initial_capital=1e7))
    via_ir = run_strategy_ir(s, d)
    direct = run_backtest_ir(d, "005930", cond, hold_days=10, initial_capital=1e7)
    assert via_ir["success"] and direct["success"]
    assert np.isclose(via_ir["metrics"]["total_return"], direct["metrics"]["total_return"])
    assert via_ir["metrics"]["n_trades"] == direct["metrics"]["n_trades"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
