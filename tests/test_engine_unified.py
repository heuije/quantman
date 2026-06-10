"""통합 실행 엔진 (engine.py) 회귀 — 명세 §7.6.

Stage 1: 이벤트(on_signal) 경로가 기존 run_strategy_ir(→run_backtest_ir/run_portfolio_ir)와
동치인지 고정한다. 신엔진은 검증된 경로 패리티를 통과한 뒤에만 횡단·청산 overlay로 확장.

    cd platform && pytest tests/test_engine_unified.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, Overlays, PositionSpec, SimSpec, Sizing, StrategyIR, Universe,
    run_backtest_ir,
)
from quant_core.ir_engine.backtest import run_portfolio_ir  # noqa: E402
from quant_core.ir_engine.engine import run_unified, _select, _target_weights  # noqa: E402


def _one(seed: int, periods: int = 200):
    idx = pd.date_range("2020-01-01", periods=periods, freq="B")
    r = np.random.default_rng(seed)
    close = np.maximum(100 + np.cumsum(r.normal(0.05, 1.2, periods)), 5.0)
    return pd.DataFrame({
        "Open": np.r_[close[0], close[:-1]], "High": close * 1.01,
        "Low": close * 0.99, "Close": close, "Volume": r.uniform(1e5, 1e6, periods),
        "ma_dev_20d": r.uniform(-5, 5, periods), "atr_14": np.abs(r.normal(2, 0.5, periods)),
    }, index=idx)


def _cond_self():
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("__SELF__.ma_dev_20d"), "right": const(0.0)})


def _assert_parity(s, d):
    """run_unified == 구 이벤트 엔진(run_backtest_ir/run_portfolio_ir) 직접 대조 — 동치 고정.

    디스패치가 통합 엔진으로 전환된 뒤에도 검증이 자기대조(tautology)가 되지 않도록,
    구 엔진을 직접 호출해 equity 시계열·트레이드 수를 비교한다.
    """
    a = run_unified(s, d)
    ex, sim, u, sz = s.position.exit, s.simulation, s.universe, s.position.sizing
    kw = dict(sell_node=ex.condition, hold_days=ex.hold_days, take_profit=ex.take_profit,
              stop_loss=ex.stop_loss, trail_atr_mult=ex.trail_atr_mult, trail_pct=ex.trail_pct,
              fill=sim.fill, initial_capital=sim.initial_capital, start=sim.start, end=sim.end)
    if u.kind == "single":
        b = run_backtest_ir(d, u.symbols[0], s.signal, **kw)
    else:
        b = run_portfolio_ir(d, u.symbols, s.signal, amount_pct=sz.amount_pct,
                             amount_krw=(sz.amount_krw if sz.mode == "fixed_amount" else None), **kw)
    assert a["success"] and b["success"], (a.get("error"), b.get("error"))
    ea, eb = a["equity"], b["equity"]
    assert list(ea.index) == list(eb.index), "달력 불일치"
    assert np.allclose(ea.to_numpy(), eb.to_numpy(), rtol=1e-9, atol=1e-6), \
        f"equity 불일치: max diff {np.abs(ea.to_numpy() - eb.to_numpy()).max()}"
    assert a["metrics"]["n_trades"] == b["metrics"]["n_trades"]
    assert np.isclose(a["metrics"]["total_return"], b["metrics"]["total_return"])


# ── 단일 종목 (run_backtest_ir 경로) ──────────────────────────────────────────

def test_unified_single_hold_days():
    d = {"005930": _one(5)}
    s = StrategyIR(signal=_cond_self(), universe=Universe(kind="single", symbols=["005930"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"), exit=Exit(hold_days=10)),
                   simulation=SimSpec(initial_capital=1e7))
    _assert_parity(s, d)


def test_unified_single_tp_sl():
    d = {"005930": _one(7)}
    s = StrategyIR(signal=_cond_self(), universe=Universe(kind="single", symbols=["005930"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"),
                                         exit=Exit(take_profit=8.0, stop_loss=-5.0)),
                   simulation=SimSpec(initial_capital=1e7))
    _assert_parity(s, d)


def test_unified_single_sell_condition():
    d = {"005930": _one(11)}
    sell = Node(op="compare", params={"op": "<"},
                inputs={"left": data("__SELF__.ma_dev_20d"), "right": const(0.0)})
    s = StrategyIR(signal=_cond_self(), universe=Universe(kind="single", symbols=["005930"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"), exit=Exit(condition=sell)),
                   simulation=SimSpec(initial_capital=1e7))
    _assert_parity(s, d)


def test_unified_single_close_fill():
    d = {"005930": _one(3)}
    s = StrategyIR(signal=_cond_self(), universe=Universe(kind="single", symbols=["005930"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"), exit=Exit(hold_days=5)),
                   simulation=SimSpec(initial_capital=1e7, fill="close"))
    _assert_parity(s, d)


# ── 다종목 포트폴리오 (run_portfolio_ir 경로) ─────────────────────────────────

def test_unified_portfolio_hold_days():
    d = {"AAA": _one(1), "BBB": _one(2), "CCC": _one(3)}
    s = StrategyIR(signal=_cond_self(), universe=Universe(kind="list", symbols=["AAA", "BBB", "CCC"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"), exit=Exit(hold_days=8),
                                         sizing=Sizing(amount_pct=30.0)),
                   simulation=SimSpec(initial_capital=1e7))
    _assert_parity(s, d)


def test_unified_portfolio_sell_condition():
    d = {"AAA": _one(4), "BBB": _one(6), "CCC": _one(8)}
    sell = Node(op="compare", params={"op": "<"},
                inputs={"left": data("__SELF__.ma_dev_20d"), "right": const(-1.0)})
    s = StrategyIR(signal=_cond_self(), universe=Universe(kind="list", symbols=["AAA", "BBB", "CCC"]),
                   position=PositionSpec(entry=Entry(mode="on_signal"), exit=Exit(condition=sell),
                                         sizing=Sizing(amount_pct=40.0)),
                   simulation=SimSpec(initial_capital=1e7))
    _assert_parity(s, d)


# ── Stage 2: 스케줄 횡단 (정수주 리밸런스) ────────────────────────────────────

def _multi():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(252)
        return pd.DataFrame({
            "Open": close, "High": close * 1.001, "Low": close * 0.999,
            "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom),
            "ma_dev_20d": np.where(np.arange(252) % 2 == 0, 1.0, -1.0),
        }, index=idx)

    return {"AAA": mk(0.003, 10.0), "BBB": mk(-0.001, -5.0), "CCC": mk(0.0, -3.0)}


def test_unified_scheduled_picks_winner():
    """score top1 롱 월간 → 모멘텀 최상위 AAA 보유 → 벤치 초과, 정수주."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
        simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"], res.get("error")
    assert res["metrics"]["total_return"] > res["metrics"]["bench_total"]
    assert res["equity"].iloc[-1] > 0


def test_unified_scheduled_quarterly():
    """분기 리밸런스 — A-1 핵심 주기. 실행되고 양의 자산곡선."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long", entry=Entry(mode="scheduled",
                                                            rebalance="quarterly", top_n=2)),
        simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"] and res["equity"].iloc[-1] > 0


def test_unified_scheduled_top_pct():
    """top_pct(상위 40%) 선택 — 3종목 중 1~2종목 보유."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long", entry=Entry(mode="scheduled",
                                                            rebalance="monthly", top_pct=40.0)),
        simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"] and res["equity"].iloc[-1] > 0


def test_unified_scheduled_fixed_weight():
    """fixed_weight 정적 배분 — 3자산 지정 비중 보유(B-5)."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long",
                              sizing=Sizing(mode="fixed_weight",
                                            weights={"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}),
                              entry=Entry(mode="scheduled", rebalance="monthly")),
        simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"] and res["equity"].iloc[-1] > 0
    assert res["metrics"]["n_trades"] >= 0


def test_unified_scheduled_integer_shares():
    """정수주 — 자본이 가격에 안 떨어지면 현금드래그 발생(전액 투입 불가)."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long", entry=Entry(mode="scheduled",
                                                            rebalance="monthly", top_n=1)),
        simulation=SimSpec(initial_capital=123_456.0))   # 비정수배 자본
    res = run_unified(s, _multi())
    assert res["success"] and res["equity"].iloc[-1] > 0


def test_unified_scheduled_condition_holds_true():
    """condition 신호 스케줄 → 참인 종목 동일가중 보유."""
    cond = Node(op="compare", params={"op": ">"},
                inputs={"left": data("ma_dev_20d"), "right": const(0.0)})
    s = StrategyIR(
        signal=cond, universe=Universe(kind="all"),
        position=PositionSpec(direction="long", entry=Entry(mode="scheduled", rebalance="weekly")),
        simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"] and res["equity"].iloc[-1] > 0


# ── Stage 3: 청산 overlay (스케줄 + 동적 청산) — 재설계의 핵심 ────────────────

def _lt0(ref):
    return Node(op="compare", params={"op": "<"}, inputs={"left": data(ref), "right": const(0.0)})


def test_unified_scheduled_exit_changes_outcome():
    """예시 A-1 패턴 — 스케줄 리밸런스 + 동적 청산조건이 결과를 바꾼다(이전엔 불가).

    동일 전략 ± exit.condition → with-exit가 더 많은 트레이드(중간청산)와 다른 자산곡선.
    """
    d = _multi()
    base = dict(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                simulation=SimSpec(initial_capital=1e7))
    no_exit = StrategyIR(**base, position=PositionSpec(
        direction="long", entry=Entry(mode="scheduled", rebalance="monthly", top_n=2)))
    with_exit = StrategyIR(**base, position=PositionSpec(
        direction="long", entry=Entry(mode="scheduled", rebalance="monthly", top_n=2),
        exit=Exit(condition=_lt0("ma_dev_20d"))))
    a, b = run_unified(no_exit, d), run_unified(with_exit, d)
    assert a["success"] and b["success"], (a.get("error"), b.get("error"))
    assert b["metrics"]["n_trades"] > a["metrics"]["n_trades"], "청산이 트레이드를 늘려야"
    assert not np.isclose(a["equity"].iloc[-1], b["equity"].iloc[-1]), "청산이 결과를 바꿔야"
    assert (b["trades"]["청산사유"] == "매도신호").any(), "매도신호 청산 기록 존재"


def test_unified_scheduled_stop_loss():
    """스케줄 + 손절 — 하락 종목이 손절로 중간 청산."""
    idx = pd.date_range("2020-01-01", periods=120, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(120)
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom),
                             "ma_dev_20d": 1.0}, index=idx)
    d = {"UP": mk(0.004, 12.0), "DN": mk(-0.01, 8.0), "FLAT": mk(0.0, 1.0)}
    s = StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                   position=PositionSpec(direction="long", exit=Exit(stop_loss=-8.0),
                                         entry=Entry(mode="scheduled", rebalance="monthly", top_n=2)),
                   simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, d)
    assert res["success"], res.get("error")
    assert (res["trades"]["청산사유"] == "손절").any(), "DN이 손절로 청산돼야"


def _multi5():
    idx = pd.date_range("2020-01-01", periods=120, freq="B")

    def mk(drift, mom, dev_flip=None):
        close = 100 * (1 + drift) ** np.arange(120)
        dev = (np.full(120, 1.0) if dev_flip is None
               else np.where(np.arange(120) < dev_flip, 1.0, -1.0))
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom),
                             "ma_dev_20d": dev}, index=idx)
    return {"WIN": mk(0.004, 12.0), "EXIT": mk(0.001, 8.0, dev_flip=30),
            "REFILL": mk(0.002, 5.0), "LOW": mk(0.0, 1.0)}


def test_unified_refill_cash_vs_replace():
    """refill: cash=빈 슬롯 현금 유지 · replace=차순위(REFILL) 즉시 충원."""
    d = _multi5()
    exit_cond = _lt0("ma_dev_20d")
    base = dict(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                simulation=SimSpec(initial_capital=1e7))

    def _mk(refill):
        return StrategyIR(**base, position=PositionSpec(
            direction="long", exit=Exit(condition=exit_cond),
            entry=Entry(mode="scheduled", rebalance="monthly", top_n=2, refill=refill)))

    cash = run_unified(_mk("cash"), d)
    repl = run_unified(_mk("replace"), d)
    assert cash["success"] and repl["success"], (cash.get("error"), repl.get("error"))
    cash_syms = set(cash["trades"]["종목"]) if len(cash["trades"]) else set()
    repl_syms = set(repl["trades"]["종목"]) if len(repl["trades"]) else set()
    # replace는 청산 후 차순위 REFILL을 사들임 → cash 모드엔 (중간) 없던 종목
    assert "REFILL" in repl_syms, "replace는 차순위 후보를 충원해야"
    assert "REFILL" not in cash_syms, "cash는 빈 슬롯을 현금 유지(REFILL 미보유)"


# ── 선택·사이징 단위 (Selector/Sizer — _weights_row 커버리지 이전) ────────────

def test_engine_select_long_topn_equal():
    pos = PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                       entry=Entry(mode="scheduled", top_n=2))
    alpha = pd.Series({"A": 3.0, "B": 1.0, "C": 2.0, "D": 0.5})
    longs, shorts = _select(alpha, pos, False)
    assert set(longs) == {"A", "C"} and not shorts
    w = _target_weights(longs, shorts, alpha, None, pos.sizing)
    assert np.isclose(w.abs().sum(), 1.0) and (w > 0).all()


def test_engine_select_long_short_dollar_neutral():
    pos = PositionSpec(direction="long_short", entry=Entry(mode="scheduled", top_n=1))
    alpha = pd.Series({"A": 3.0, "B": 1.0, "C": 2.0, "D": 0.5})
    longs, shorts = _select(alpha, pos, False)
    w = _target_weights(longs, shorts, alpha, None, pos.sizing)
    assert np.isclose(w.sum(), 0.0, atol=1e-9)         # 달러 중립
    assert np.isclose(w.abs().sum(), 1.0)              # 풀투자(절대비중)
    assert w["A"] > 0 and w["D"] < 0                    # 최고 롱·최저 숏


def test_engine_size_signal_proportional():
    pos = PositionSpec(direction="long", sizing=Sizing(mode="signal_proportional"),
                       entry=Entry(mode="scheduled", top_n=2))
    alpha = pd.Series({"A": 3.0, "B": 1.0})
    longs, shorts = _select(alpha, pos, False)
    w = _target_weights(longs, shorts, alpha, None, pos.sizing)
    assert np.isclose(w["A"], 0.75) and np.isclose(w["B"], 0.25)   # 3:1 → 0.75:0.25


def test_engine_select_threshold_sign_split():
    """임계 선택(threshold) — 부호 기준 롱숏(TSMOM): score>0 롱·score<0 숏(순위 무관)."""
    pos = PositionSpec(direction="long_short", entry=Entry(mode="scheduled", threshold=0.0))
    alpha = pd.Series({"A": 3.0, "B": -1.0, "C": 2.0, "D": -0.5, "E": 0.0})
    longs, shorts = _select(alpha, pos, False)
    assert set(longs) == {"A", "C"}            # 양수 전부 롱
    assert set(shorts) == {"B", "D"}           # 음수 전부 숏 (정확히 임계값 0은 어느 쪽도 아님)


def test_engine_select_threshold_long_keeps_score():
    """롱 전용 임계 — score>thr 선택하되 score 보존(신호비례 사이징과 결합 가능)."""
    pos = PositionSpec(direction="long", sizing=Sizing(mode="signal_proportional"),
                       entry=Entry(mode="scheduled", threshold=5.0))
    alpha = pd.Series({"A": 10.0, "B": 6.0, "C": 3.0})
    longs, shorts = _select(alpha, pos, False)
    assert set(longs) == {"A", "B"} and not shorts          # >5 만 선택
    w = _target_weights(longs, shorts, alpha, None, pos.sizing)
    assert np.isclose(w["A"], 10 / 16) and np.isclose(w["B"], 6 / 16)   # score 비례 유지


# ── Stage 4: 롱숏 정수주 + 전역 overlay + 비용 ────────────────────────────────

def test_unified_long_short_winner_loser():
    """롱숏 — 승자(AAA) 롱 + 패자(BBB) 숏 둘 다 이익 → 양의 수익."""
    s = StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                   position=PositionSpec(direction="long_short",
                                         entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
                   simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"], res.get("error")
    assert res["metrics"]["total_return"] > 0


def test_unified_tsmom_sign_split_runs():
    """TSMOM — 자기 추세 부호로 독립 롱/숏(임계 선택). AAA(+) 롱·BBB·CCC(−) 숏 → 추세대로 양수익."""
    s = StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                   position=PositionSpec(direction="long_short",
                                         entry=Entry(mode="scheduled", rebalance="monthly", threshold=0.0)),
                   simulation=SimSpec(initial_capital=1e7))
    res = run_unified(s, _multi())
    assert res["success"], res.get("error")
    assert res["metrics"]["total_return"] > 0


# ── M5d: on_signal 부호방향 당일매매 (라이브 양방향 경로의 backtest 거울) ──────────

def _daytrade_bars():
    """방향전환 당일매매 검증용 결정적 일봉 + 부호 신호(sig).

    defer(next_open): 신호 바 i → 시가 진입 바 i+1. sig[i]가 바 i+1 방향을 정한다.
      bar0 sig=+1 → bar1 롱(open100→close110, +10%)
      bar1 sig=-1 → bar2 숏(open100→close 90, +10% 환매이익)
      bar2 sig= 0 → bar3 무진입
    """
    idx = pd.date_range("2020-01-01", periods=4, freq="B")
    df = pd.DataFrame({
        "Open":   [100.0, 100.0, 100.0, 100.0],
        "High":   [100.0, 110.0, 100.0, 100.0],
        "Low":    [100.0, 100.0,  90.0, 100.0],
        "Close":  [100.0, 110.0,  90.0, 100.0],
        "Volume": [1e6, 1e6, 1e6, 1e6],
        "sig":    [1.0, -1.0, 0.0, 0.0],
    }, index=idx)
    return {"005930": df}


def test_unified_directional_long_short_daytrade():
    """M5d — on_signal + long_short(부호방향) + hold_days=0 당일매매.

    score 부호로 바별 방향 결정: 롱일 종가↑·숏일 종가↓ 둘 다 +10%, 보유일 0.
    엔진 _direction_for seam이 단일종목 directional을 횡단 랭킹 없이 처리하는지 고정."""
    d = _daytrade_bars()
    s = StrategyIR(
        signal=data("__SELF__.sig"),
        universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(direction="long_short",
                              entry=Entry(mode="on_signal", threshold=0.0),
                              exit=Exit(hold_days=0)),
        simulation=SimSpec(initial_capital=1e7, fill="next_open",
                           commission=0.0, slippage=0.0, sell_tax=0.0))
    res = run_unified(s, d)
    assert res["success"], res.get("error")
    tr = res["trades"]
    assert len(tr) == 2, f"롱1·숏1 두 당일 트레이드 기대, 실제 {len(tr)}: {tr}"
    assert (tr["보유일"] == 0).all(), tr[["보유일"]]             # 당일청산
    assert (tr["수익률(%)"] > 0).all(), tr[["수익률(%)"]]        # 롱↑·숏↓ 둘 다 이익
    # 1e7 → +10%(롱) → 1.1e7 → +10%(숏) → 1.21e7 복리
    assert np.isclose(res["equity"].iloc[-1], 1.21e7, rtol=1e-6), res["equity"].iloc[-1]


def test_unified_directional_requires_score():
    """on_signal+long_short인데 condition 신호면 엔진이 명시 거부(부호방향 불가)."""
    d = _daytrade_bars()
    s = StrategyIR(
        signal=_cond_self(),                                   # condition 신호
        universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(direction="long_short",
                              entry=Entry(mode="on_signal"),
                              exit=Exit(hold_days=0)),
        simulation=SimSpec(initial_capital=1e7, fill="next_open"))
    # _cond_self는 __SELF__.ma_dev_20d 참조 — _daytrade_bars엔 그 컬럼이 없으나
    # 신호 타입(condition) 거부가 먼저 걸린다.
    res = run_unified(s, d)
    assert not res["success"]
    assert "score" in (res.get("error") or "")


def test_threshold_requires_score_signal():
    """임계 선택은 score 신호 전용 — condition 신호에 threshold면 S-select 에러."""
    from quant_core.ir_engine import validate_strategy
    cond = Node(op="compare", params={"op": ">"},
                inputs={"left": data("momentum_12_1m"), "right": const(0.0)})
    s = StrategyIR(signal=cond, universe=Universe(kind="all"),
                   position=PositionSpec(direction="long",
                                         entry=Entry(mode="scheduled", rebalance="monthly", threshold=0.0)))
    assert any(i.rule == "S-select" and i.is_error for i in validate_strategy(s))


def test_unified_borrow_cost_reduces_return():
    """숏 차입비용(5%)이 롱숏 수익을 깎는다."""
    base = dict(signal=data("momentum_12_1m"), universe=Universe(kind="all"))

    def _run(bp):
        return run_unified(StrategyIR(
            **base, position=PositionSpec(direction="long_short",
                                          entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
            simulation=SimSpec(initial_capital=1e7, short_borrow_pct=bp)), _multi())
    r0, r5 = _run(0.0), _run(5.0)
    assert r0["success"] and r5["success"]
    assert r5["metrics"]["total_return"] < r0["metrics"]["total_return"]


def test_unified_target_vol_runs():
    """target_vol 사이저 + 레버리지 — 실행·양의 자산곡선(B-1 형태)."""
    s = StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                   position=PositionSpec(direction="long",
                                         sizing=Sizing(mode="target_vol", target_vol_pct=20.0),
                                         entry=Entry(mode="scheduled", rebalance="monthly", top_n=2)),
                   simulation=SimSpec(initial_capital=1e7, leverage=2.0))
    res = run_unified(s, _multi())
    assert res["success"] and res["equity"].iloc[-1] > 0


def test_unified_rfr_cash_accrues():
    """현금 무위험수익 — 미보유(현금) 전략이 rfr로 적립."""
    never = Node(op="compare", params={"op": ">"},
                 inputs={"left": data("ma_dev_20d"), "right": const(100.0)})
    base = dict(signal=never, universe=Universe(kind="all"),
                position=PositionSpec(direction="long", entry=Entry(mode="scheduled", rebalance="monthly")))
    r0 = run_unified(StrategyIR(**base, simulation=SimSpec(initial_capital=1e7, rfr_pct=0.0)), _multi())
    r5 = run_unified(StrategyIR(**base, simulation=SimSpec(initial_capital=1e7, rfr_pct=5.0)), _multi())
    assert r0["success"] and r5["success"]
    assert r5["equity"].iloc[-1] > r0["equity"].iloc[-1]


def test_unified_drawdown_overlay_caps_mdd():
    """낙폭 정지 overlay — 하락 전략의 MDD 완화."""
    idx = pd.date_range("2020-01-01", periods=120, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(120)
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom),
                             "ma_dev_20d": 1.0}, index=idx)
    d = {"DN": mk(-0.005, 5.0), "DN2": mk(-0.004, 4.0)}
    base = dict(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                simulation=SimSpec(initial_capital=1e7))
    plain = run_unified(StrategyIR(**base, position=PositionSpec(
        direction="long", entry=Entry(mode="scheduled", rebalance="monthly", top_n=1))), d)
    dd = run_unified(StrategyIR(**base, position=PositionSpec(
        direction="long", entry=Entry(mode="scheduled", rebalance="monthly", top_n=1),
        overlays=Overlays(max_drawdown_stop=10.0))), d)
    assert plain["success"] and dd["success"]
    assert dd["metrics"]["mdd"] >= plain["metrics"]["mdd"]   # mdd<0 — 완화 시 0에 가까움


# ── 예시 매핑: A-1 형태 (스크리너 + 분기 + 동적 청산) — 재설계 동기 ───────────

def test_unified_a1_shape_screener_quarterly_exit():
    """A-1 형태 — 스크리너 유니버스 + score 신호 + 분기 리밸런스 + 적자전환류 청산.

    이전 엔진에선 '분기 횡단 리밸런스 + 동적 청산'이 표현 불가했다. 통합 엔진에서
    한 전략으로 조립·실행되고, 청산이 결과에 효과를 내는지 고정한다.
    """
    d = _multi()
    filt = Node(op="compare", params={"op": ">"},
                inputs={"left": data("momentum_12_1m"), "right": const(-100.0)})  # 전부 통과(경로 가동)
    # 단일 선별 조건: 필터 ∧ 횡단순위(모멘텀 상위 3, count·desc)
    rank_cond = Node(op="compare", params={"op": "<="},
                     inputs={"left": Node(op="rank", params={"descending": True, "unit": "count"},
                                          inputs={"signal": data("momentum_12_1m")}),
                             "right": const(3.0)})
    screener = {"condition": Node(op="logic", params={"logic": "AND"},
                                  inputs={"0": filt, "1": rank_cond}).model_dump()}
    base = dict(signal=data("momentum_12_1m"),
                universe=Universe(kind="list", symbols=["AAA", "BBB", "CCC"], screener=screener),
                simulation=SimSpec(initial_capital=1e7))
    no_exit = StrategyIR(**base, position=PositionSpec(
        direction="long", entry=Entry(mode="scheduled", rebalance="quarterly", top_n=2)))
    with_exit = StrategyIR(**base, position=PositionSpec(
        direction="long", entry=Entry(mode="scheduled", rebalance="quarterly", top_n=2),
        exit=Exit(condition=_lt0("ma_dev_20d"))))
    a, b = run_unified(no_exit, d), run_unified(with_exit, d)
    assert a["success"] and b["success"], (a.get("error"), b.get("error"))
    assert not np.isclose(a["equity"].iloc[-1], b["equity"].iloc[-1]), "동적 청산이 결과를 바꿔야"


# ── Phase 1: 레버리지(노출>순자산) — 매수여력=NAV×L ───────────────────────────

def _rising(rate: float, periods: int = 120):
    idx = pd.date_range("2020-01-01", periods=periods, freq="B")
    close = 100 * (1 + rate) ** np.arange(periods)
    return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                         "Close": close, "Volume": 1e6, "ma_dev_20d": 1.0}, index=idx)


def _always_true():
    """ma_dev_20d(=1.0) > 0 — 항상 참 condition(상시 보유)."""
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("ma_dev_20d"), "right": const(0.0)})


def test_leverage_amplifies_single_asset():
    """단일 상승자산 상시보유 — lev=2가 lev=1 수익을 크게 증폭(레버리지 작동, no-op 아님)."""
    d = {"AAA": _rising(0.003)}

    def _run(lev):
        return run_unified(StrategyIR(
            signal=_always_true(), universe=Universe(kind="single", symbols=["AAA"]),
            position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                                  entry=Entry(mode="always")),
            simulation=SimSpec(initial_capital=1e7, leverage=lev, fill="close")), d)
    r1, r2 = _run(1.0), _run(2.0)
    assert r1["success"] and r2["success"], (r1.get("error"), r2.get("error"))
    # 일일 리밸런싱 2배 → 대략 2배 복리. 비용 감안 1.7배 이상이면 레버리지가 실제로 채워진 것.
    assert r2["metrics"]["total_return"] > r1["metrics"]["total_return"] * 1.7


def test_leverage_no_greedy_collapse():
    """균등가중 2종목 lev=2 — 대칭 쌍(±0.3%/일)이면 net≈0. 첫 종목 현금 독식 손상버그 회귀가드.

    손상버그면 현금이 첫 종목(AAA)에 쏠려 단일종목 lev=2처럼 큰 양수익이 됨.
    올바르면 두 종목 모두 1×nav씩 채워져 롱끼리 상쇄 → 거의 평탄.
    """
    idx = pd.date_range("2020-01-01", periods=120, freq="B")

    def mk(rate):
        close = 100 * (1 + rate) ** np.arange(120)
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "ma_dev_20d": 1.0}, index=idx)
    d = {"AAA": mk(0.003), "BBB": mk(-0.003)}

    def _run(syms):
        return run_unified(StrategyIR(
            signal=_always_true(), universe=Universe(kind="list", symbols=syms),
            position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                                  entry=Entry(mode="always")),
            simulation=SimSpec(initial_capital=1e7, leverage=2.0, fill="close")), d)
    pair, aaa_only = _run(["AAA", "BBB"]), _run(["AAA"])
    assert pair["success"] and aaa_only["success"], (pair.get("error"), aaa_only.get("error"))
    # 독식 아님: 대칭 쌍은 단일 AAA lev=2 수익의 절반 미만(사실상 평탄)이어야.
    assert pair["metrics"]["total_return"] < aaa_only["metrics"]["total_return"] * 0.5
    assert abs(pair["metrics"]["total_return"]) < 20.0   # 거의 평탄(상쇄)


def test_leverage_funding_cost_reduces_return():
    """lev=2 + 펀딩비용(10%) → 차입분(=nav)에 이자 → 무펀딩보다 수익↓."""
    d = {"AAA": _rising(0.003)}

    def _run(funding):
        return run_unified(StrategyIR(
            signal=_always_true(), universe=Universe(kind="single", symbols=["AAA"]),
            position=PositionSpec(direction="long", entry=Entry(mode="always")),
            simulation=SimSpec(initial_capital=1e7, leverage=2.0, funding_cost_pct=funding,
                               fill="close")), d)
    r0, r10 = _run(0.0), _run(10.0)
    assert r0["success"] and r10["success"], (r0.get("error"), r10.get("error"))
    assert r10["metrics"]["total_return"] < r0["metrics"]["total_return"]


# ── Phase 3: 종목별 증거금/노티오널(선물 펀딩 면제) ───────────────────────────

def test_futures_margin_exempts_funding():
    """선물은 carry가 가격에 내장 → 레버리지 펀딩 면제. 동일 경로·동일 lev=2·동일 펀딩률에서
    선물(원유선물)이 현금주식(코드)보다 펀딩비용이 작아 최종 수익이 높아야."""
    base = _rising(0.002)
    d_fut, d_eq = {"원유선물": base.copy()}, {"005930": base.copy()}

    def _run(d, sym):
        return run_unified(StrategyIR(
            signal=_always_true(), universe=Universe(kind="single", symbols=[sym]),
            position=PositionSpec(direction="long", entry=Entry(mode="always")),
            simulation=SimSpec(initial_capital=1e7, leverage=2.0, funding_cost_pct=10.0,
                               fill="close")), d)
    fut, eq = _run(d_fut, "원유선물"), _run(d_eq, "005930")
    assert fut["success"] and eq["success"], (fut.get("error"), eq.get("error"))
    # 유일한 차이는 증거금률(선물 0.15 vs 주식 1.0) → 선물 펀딩 면제 → 수익↑.
    assert fut["metrics"]["total_return"] > eq["metrics"]["total_return"]


# ── Phase 2: 유지증거금 강제청산(마진콜) ──────────────────────────────────────

def test_maintenance_margin_prevents_wipeout():
    """월간 레버리지(3x) 포지션 급락 — 유지증거금이 마진콜로 디레버리지해 NAV 음수 폭주를 막는다.

    무청산: 고정 보유라 급락 시 nav가 음수로 폭주.
    유지증거금: nav/gross<유지율이면 목표 3x로 강제 복원 → nav>0 유지.
    """
    idx = pd.date_range("2020-01-01", periods=50, freq="B")
    close = np.concatenate([np.full(20, 100.0), 100 * 0.93 ** np.arange(1, 31)])  # 20일 평탄→ -7%/일
    df = pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                       "Close": close, "Volume": 1e6, "ma_dev_20d": 1.0}, index=idx)

    def _run(maint):
        return run_unified(StrategyIR(
            signal=_always_true(), universe=Universe(kind="single", symbols=["AAA"]),
            position=PositionSpec(direction="long",
                                  entry=Entry(mode="scheduled", rebalance="monthly")),
            simulation=SimSpec(initial_capital=1e7, leverage=3.0, fill="close",
                               maintenance_margin_pct=maint)), {"AAA": df})
    mc, no_mc = _run(25.0), _run(None)
    assert mc["success"] and no_mc["success"], (mc.get("error"), no_mc.get("error"))
    # 두 실행의 유일한 차이가 maintenance_margin_pct이므로 결과 차이는 마진콜 발동을 의미.
    # (부분 디레버리지는 라운드트립이 아니라 trades에 기록되지 않음 — 자산곡선으로 검증.)
    assert not np.isclose(mc["equity"].iloc[-1], no_mc["equity"].iloc[-1]), "마진콜이 결과를 바꿔야"
    assert no_mc["equity"].min() < 0, "무청산은 NAV가 음수로 폭주(고정 보유 + 3x 급락)"
    assert mc["equity"].iloc[-1] > 0, "유지증거금이 NAV 음수 폭주를 막아야"
    assert mc["equity"].min() > no_mc["equity"].min(), "유지증거금이 손실을 제한"


def test_maintenance_margin_inactive_when_unleveraged():
    """무레버리지(1x)면 자기자본=gross라 유지율 25%에 절대 안 걸림(마진콜 0)."""
    d = {"AAA": _rising(0.002)}
    res = run_unified(StrategyIR(
        signal=_always_true(), universe=Universe(kind="single", symbols=["AAA"]),
        position=PositionSpec(direction="long", entry=Entry(mode="scheduled", rebalance="monthly")),
        simulation=SimSpec(initial_capital=1e7, leverage=1.0, fill="close",
                           maintenance_margin_pct=25.0)), d)
    assert res["success"], res.get("error")
    if len(res["trades"]):
        assert not (res["trades"]["청산사유"] == "마진콜").any()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
