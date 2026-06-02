"""WTI 원유선물 모듈 회귀 테스트.

신호 발생·백테스트 비용·요약 지표·grid/walk-forward의 핵심 케이스를 고정.
실제 엑셀 데이터 대조는 별도 검증 스크립트(verify)로 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# 기존 core 테스트 컨벤션: 패키지 루트를 sys.path에 추가 (editable install 우회).
_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from quant_core.oil_futures import (
    ContractSpec,
    CostModel,
    Side,
    generate_signals,
    grid_search,
    grid_to_dataframe,
    run_backtest,
    summarize,
    walk_forward,
)


# ───── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def small_df() -> pd.DataFrame:
    """의도적으로 cross 케이스 포함하는 10일치 일봉."""
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=10, freq="B"),
        "open":   [70, 75, 79, 82, 78, 76, 73, 71, 75, 80],
        "high":   [71, 76, 81, 85, 83, 78, 74, 72, 77, 82],
        "low":    [69, 74, 77, 79, 76, 73, 70, 69, 73, 77],
        "close":  [70, 76, 80, 84, 80, 77, 72, 70, 76, 81],
        "volume": [1000] * 10,
    })


# ───── signals ──────────────────────────────────────────────────────────────

def test_short_signal_fires_on_first_cross(small_df: pd.DataFrame) -> None:
    """fixture에 80 첫 cross가 2회 있어야 한다:
    - i=2: high=81 ≥ 80, i=1: high=76 < 80 → 발화
    - i=9: high=82 ≥ 80, i=8: high=77 < 80 → 다시 발화 (중간에 78로 내려갔다 재돌파)
    """
    sigs = generate_signals(small_df, short_thresholds=[80])
    short_sigs = [s for s in sigs if s.side == Side.SHORT]
    assert len(short_sigs) == 2
    assert short_sigs[0].date == pd.Timestamp(small_df["date"].iloc[2])
    assert short_sigs[1].date == pd.Timestamp(small_df["date"].iloc[9])
    assert all(s.threshold == 80 for s in short_sigs)


def test_no_short_signal_when_already_above(small_df: pd.DataFrame) -> None:
    """day3 high=85 ≥ 80 이지만 day2 high=81 도 이미 ≥ 80 → hysteresis로 차단."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    dates = {s.date for s in sigs}
    assert pd.Timestamp(small_df["date"].iloc[3]) not in dates


def test_long_signal_fires_on_first_breach(small_df: pd.DataFrame) -> None:
    """fixture low 시계열에서 70 첫 cross-below:
    - i=6: low=70 ≤ 70 ∧ i=5: low=73 > 70 → 발화 (정확히 70 터치도 ≤ 임계라 포함)
    """
    sigs = generate_signals(small_df, long_thresholds=[70])
    long_sigs = [s for s in sigs if s.side == Side.LONG]
    assert len(long_sigs) == 1
    assert long_sigs[0].date == pd.Timestamp(small_df["date"].iloc[6])
    assert long_sigs[0].threshold == 70


def test_long_signal_with_proper_cross() -> None:
    """명시적인 cross-below 케이스."""
    df = pd.DataFrame({
        "date":  pd.date_range("2024-01-01", periods=5, freq="B"),
        "open":  [50, 48, 45, 43, 46],
        "high":  [51, 49, 46, 44, 47],
        "low":   [49, 47, 44, 42, 45],   # day2 low=44 ≤ 45 < day1 low=47 → fire @45
        "close": [50, 48, 45, 43, 46],
        "volume": [1000] * 5,
    })
    sigs = generate_signals(df, long_thresholds=[45])
    assert len(sigs) == 1
    assert sigs[0].side == Side.LONG
    assert sigs[0].threshold == 45
    assert sigs[0].date == pd.Timestamp(df["date"].iloc[2])


def test_empty_thresholds_returns_empty(small_df: pd.DataFrame) -> None:
    assert generate_signals(small_df) == []


# ───── backtest ─────────────────────────────────────────────────────────────

def test_short_backtest_entry_is_next_open_not_signal_close(small_df: pd.DataFrame) -> None:
    """진입가가 신호일 종가가 아닌 '다음 영업일 시가'인지 확인 (look-ahead 제거)."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    res = run_backtest(small_df, sigs, horizon_days=3, cost=CostModel(0, 0))
    assert len(res.trades) == 1
    t = res.trades[0]
    # signal at day2 → entry day3, exit day6
    assert t.entry_price == small_df["open"].iloc[3]  # 82
    assert t.exit_price == small_df["close"].iloc[6]  # 72
    assert t.entry_date == pd.Timestamp(small_df["date"].iloc[3])
    assert t.exit_date == pd.Timestamp(small_df["date"].iloc[6])


def test_short_pnl_sign_and_magnitude(small_df: pd.DataFrame) -> None:
    """Short PnL = (entry - exit) * multiplier. 비용 0."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    res = run_backtest(small_df, sigs, horizon_days=3, cost=CostModel(0, 0))
    t = res.trades[0]
    # (82 - 72) * 1000 = +10000
    assert t.gross_pnl_usd == pytest.approx(10_000)
    assert t.net_pnl_usd == pytest.approx(10_000)  # 비용 0
    # 수익률은 short 부호 반전: -(exit/entry - 1) = -(72/82 - 1) > 0
    assert t.return_pct == pytest.approx(-(72 / 82 - 1))
    assert t.return_pct > 0


def test_cost_model_reduces_net_pnl(small_df: pd.DataFrame) -> None:
    """수수료 + 슬리피지가 net_pnl 을 적절히 줄이는지."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    free = run_backtest(small_df, sigs, 3, CostModel(0, 0)).trades[0]
    paid = run_backtest(small_df, sigs, 3, CostModel(2.5, 1)).trades[0]
    # 슬리피지 1틱 * $0.01 * 1000 * 2(in+out) = $20, 수수료 $2.5*2 = $5 → 총 $25
    assert paid.net_pnl_usd == pytest.approx(free.net_pnl_usd - 25)
    assert paid.gross_pnl_usd == free.gross_pnl_usd  # gross 는 비용 무관


def test_horizon_overflow_excludes_trade() -> None:
    """horizon이 데이터 끝을 넘어가는 신호는 trade 미성립."""
    df = pd.DataFrame({
        "date":  pd.date_range("2024-01-01", periods=4, freq="B"),
        "open":  [70, 79, 82, 80],
        "high":  [71, 79, 82, 80],   # day2 cross 80
        "low":   [69, 78, 79, 78],
        "close": [70, 79, 82, 80],
        "volume": [1000] * 4,
    })
    sigs = generate_signals(df, short_thresholds=[80])
    assert len(sigs) == 1
    # horizon=5 → exit_i=2+1+5=8, 데이터 길이 4 → trade 미성립
    res = run_backtest(df, sigs, horizon_days=5)
    assert res.trades == []


def test_equity_curve_is_cumulative_net_pnl(small_df: pd.DataFrame) -> None:
    sigs = generate_signals(small_df, short_thresholds=[80])
    res = run_backtest(small_df, sigs, 3, CostModel(0, 0))
    assert len(res.equity_curve) == 1
    assert float(res.equity_curve.iloc[-1]) == pytest.approx(res.trades[0].net_pnl_usd)


# ───── metrics ──────────────────────────────────────────────────────────────

def test_summary_marks_low_sample(small_df: pd.DataFrame) -> None:
    sigs = generate_signals(small_df, short_thresholds=[80])
    res = run_backtest(small_df, sigs, 3, CostModel(0, 0))
    s = summarize(res)
    assert s.n_trades == 1
    assert s.low_sample is True
    assert 0 <= s.win_rate <= 1


def test_summary_empty_trades_safe() -> None:
    from quant_core.oil_futures.backtest import BacktestResult
    s = summarize(BacktestResult(
        trades=[],
        equity_curve=pd.Series(dtype=float),
        portfolio_equity_curve=pd.Series(dtype=float),
        portfolio_mdd_usd=0.0,
    ))
    assert s.n_trades == 0
    assert s.win_rate == 0.0
    assert s.profit_factor == 0.0
    assert s.low_sample is True


# ───── optimizer ────────────────────────────────────────────────────────────

def test_grid_search_covers_all_combos(small_df: pd.DataFrame) -> None:
    cells = grid_search(
        small_df,
        short_thresholds=[80],
        long_thresholds=[45],
        horizons=[2, 3],
        cost=CostModel(0, 0),
    )
    # 1 short * 2 horizons + 1 long * 2 horizons = 4
    assert len(cells) == 4
    df_out = grid_to_dataframe(cells)
    assert set(df_out["side"].unique()) == {"short", "long"}
    assert set(df_out["horizon"].unique()) == {2, 3}


def test_light_mode_matches_summary_and_skips_heavy(small_df: pd.DataFrame) -> None:
    """light=True 가 summarize() 핵심 지표를 light=False 와 동일하게 내야 하고,
    per-trade MAE/MFE 와 portfolio MTM 만 건너뛴다.

    fixture: 80 임계에서 short cross 가 2회 발화하는 8일치 (entry+horizon 모두 인바운드).
    """
    df = pd.DataFrame({
        "date":  pd.date_range("2024-01-01", periods=8, freq="B"),
        # i=1 high=81 ≥80>71 → fire; i=2~3 <80 으로 후퇴; i=4 high=82 ≥80>74 → fire
        "open":  [70, 80, 76, 73, 81, 78, 75, 74],
        "high":  [71, 81, 77, 74, 82, 79, 76, 75],
        "low":   [69, 78, 73, 70, 77, 74, 71, 72],
        "close": [70, 79, 75, 72, 80, 76, 73, 73],
        "volume": [1000] * 8,
    })
    sigs = generate_signals(df, short_thresholds=[80])
    short_sigs = [s for s in sigs if s.side == Side.SHORT]
    assert len(short_sigs) == 2  # 두 번 발화 확인

    heavy = run_backtest(df, sigs, horizon_days=2, cost=CostModel(0, 0), light=False)
    fast = run_backtest(df, sigs, horizon_days=2, cost=CostModel(0, 0), light=True)
    assert len(heavy.trades) >= 2
    assert len(fast.trades) == len(heavy.trades)

    sh = summarize(heavy)
    sf = summarize(fast)
    # 정수 지표 — 정확히 같아야
    assert sf.n_trades == sh.n_trades
    # float 지표 — approx 동일
    assert sf.win_rate == pytest.approx(sh.win_rate)
    assert sf.avg_return == pytest.approx(sh.avg_return)
    assert sf.sharpe_annualized == pytest.approx(sh.sharpe_annualized)
    assert sf.max_drawdown_usd == pytest.approx(sh.max_drawdown_usd)
    assert sf.total_net_pnl_usd == pytest.approx(sh.total_net_pnl_usd)
    assert sf.profit_factor == pytest.approx(sh.profit_factor)
    assert sf.gross_profit_usd == pytest.approx(sh.gross_profit_usd)
    assert sf.gross_loss_usd == pytest.approx(sh.gross_loss_usd)

    # light 가 건너뛰는 것: per-trade MAE/MFE 와 portfolio MTM
    assert all(t.mae_usd == 0.0 and t.mfe_usd == 0.0 for t in fast.trades)
    assert fast.portfolio_equity_curve.empty
    assert fast.portfolio_mdd_usd == 0.0


def test_walk_forward_train_test_split(small_df: pd.DataFrame) -> None:
    """train(0~7) 안에 signal@i=2 + horizon=2 → entry i=3, exit i=5 (모두 train).
    test(8~9)는 데이터 부족해서 OOS는 거래 없을 수 있음 — 그것도 정상.
    """
    res = walk_forward(
        small_df,
        short_thresholds=[80],
        long_thresholds=[],
        horizons=[2],
        split_date=pd.Timestamp(small_df["date"].iloc[8]),
        cost=CostModel(0, 0),
        require_min_trades=1,
    )
    assert res.train_period[0] <= res.train_period[1]
    assert res.test_period[0] >= res.train_period[1]
    assert res.best_in_sample.side == Side.SHORT
    assert res.best_in_sample.summary.n_trades >= 1


def test_contract_spec_scales_usd_pnl_not_returns(small_df: pd.DataFrame) -> None:
    """ContractSpec multiplier scales $ PnL linearly; return%/win/sharpe spec-invariant."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    wti = run_backtest(small_df, sigs, 3, CostModel(0, 0),
                       spec=ContractSpec(tick=0.01, multiplier=1000))
    gold = run_backtest(small_df, sigs, 3, CostModel(0, 0),
                        spec=ContractSpec(tick=0.10, multiplier=100))
    assert len(wti.trades) == 1 and len(gold.trades) == 1
    tw, tg = wti.trades[0], gold.trades[0]
    assert tg.gross_pnl_usd == pytest.approx(tw.gross_pnl_usd * (100 / 1000))
    assert tg.net_pnl_usd == pytest.approx(tw.net_pnl_usd * (100 / 1000))
    assert tg.return_pct == pytest.approx(tw.return_pct)
    sw, sg = summarize(wti), summarize(gold)
    assert sg.win_rate == pytest.approx(sw.win_rate)
    assert sg.sharpe_annualized == pytest.approx(sw.sharpe_annualized)
    assert sg.total_net_pnl_usd == pytest.approx(sw.total_net_pnl_usd * (100 / 1000))


def test_default_spec_is_wti_backward_compatible(small_df: pd.DataFrame) -> None:
    """spec unspecified == WTI_SPEC(0.01/1000). byte-identical to before."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    default = run_backtest(small_df, sigs, 3, CostModel(0, 0))
    explicit = run_backtest(small_df, sigs, 3, CostModel(0, 0),
                            spec=ContractSpec(tick=0.01, multiplier=1000))
    assert default.trades[0].net_pnl_usd == pytest.approx(explicit.trades[0].net_pnl_usd)


def test_contract_spec_tick_scales_slippage(small_df: pd.DataFrame) -> None:
    """tick 은 슬리피지 드래그를 스케일한다. 동일 multiplier·동일 거래에서
    tick 10배면 슬리피지 비용(gross-net)도 10배. (gross 는 tick 불변)"""
    sigs = generate_signals(small_df, short_thresholds=[80])
    lo = run_backtest(small_df, sigs, 3, CostModel(0, 1),
                      spec=ContractSpec(tick=0.01, multiplier=1000)).trades[0]
    hi = run_backtest(small_df, sigs, 3, CostModel(0, 1),
                      spec=ContractSpec(tick=0.10, multiplier=1000)).trades[0]
    # gross 는 슬리피지·tick 무관 → 동일
    assert lo.gross_pnl_usd == pytest.approx(hi.gross_pnl_usd)
    # 슬리피지 드래그 = 2 * slippage_ticks(1) * tick * multiplier(1000), commission 0
    drag_lo = lo.gross_pnl_usd - lo.net_pnl_usd
    drag_hi = hi.gross_pnl_usd - hi.net_pnl_usd
    assert drag_lo == pytest.approx(20.0)    # 2 * 1 * 0.01 * 1000
    assert drag_hi == pytest.approx(200.0)   # 2 * 1 * 0.10 * 1000
    assert drag_hi == pytest.approx(drag_lo * 10)
