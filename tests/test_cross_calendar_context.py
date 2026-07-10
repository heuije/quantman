"""크로스캘린더 평가 달력 회귀 — D1 '조용한 항상현금' (2026-07-10 진단).

신호 트리에 타 달력 참조 심볼(VIX·미국채 등)이 있으면 EvalContext.from_dataset의
master_idx가 전 심볼 날짜 합집합으로 구성돼, __SELF__ 시리즈(reindex, ffill 없음 —
그 자체는 올바름)에 참조 전용 일자마다 NaN 구멍이 생기고, ts_*(rolling,
min_periods=window)가 창 하나에 NaN 1개만 있어도 전멸해 compare가 영구 False →
전략이 크래시 없이 "항상 현금"을 성공 반환했다.

수정: 마스터 달력·패널 컬럼을 유니버스(평가 주체) 심볼로 스코핑 — 참조 심볼은
resolve_data 브로드캐스트(ffill)로만 합류한다.

    cd platform && pytest tests/test_cross_calendar_context.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import EvalContext, Node, const, data, evaluate  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, PositionSpec, SimSpec, StrategyIR, Universe, run_strategy_ir,
)


def _bdays(periods: int, drop_mod: int, drop_rem: int) -> pd.DatetimeIndex:
    """평일 달력에서 (i % drop_mod == drop_rem)번째를 휴장일로 뺀 시장 달력."""
    idx = pd.date_range("2020-01-06", periods=periods, freq="B")
    return idx[[i % drop_mod != drop_rem for i in range(len(idx))]]


def _kr_df(seed: int = 3) -> pd.DataFrame:
    # KR 휴장(=US-only 일자) 간격 13 < ts_mean 창 20 — 합집합 달력에선 모든 20일
    # 창에 NaN 구멍이 들어가 MA가 전멸한다(실데이터 VIX×KR의 연 ~93일 어긋남 재현).
    # US(remainder 6)와 같은 modulus·다른 remainder라 휴장일이 절대 겹치지 않는다
    # (겹치면 그 일자는 합집합에서도 빠져 구멍이 사라지고 일부 창이 살아남는다).
    idx = _bdays(260, 13, 5)
    r = np.random.default_rng(seed)
    close = np.maximum(100 + np.cumsum(r.normal(0.05, 1.2, len(idx))), 5.0)
    return pd.DataFrame({
        "Open": np.r_[close[0], close[:-1]], "High": close * 1.01,
        "Low": close * 0.99, "Close": close,
        "Volume": r.uniform(1e5, 1e6, len(idx)),
    }, index=idx)


def _vix_df() -> pd.DataFrame:
    # US 달력: KR과 휴장일이 어긋난다 — KR 휴장일에 거래(합집합 달력에 KR-부재 일자
    # 주입)하고, US 휴장일은 KR 거래일이다. KR 첫 바 이전 관측을 둬 ffill을 보장.
    idx = _bdays(260, 13, 6)
    return pd.DataFrame({"Close": np.full(len(idx), 15.0)}, index=idx)


def _price_leg(ref: str) -> Node:
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data(ref),
                        "right": Node(op="ts_mean", params={"window": 20},
                                      inputs={"signal": data(ref)})})


def _vix_leg() -> Node:
    return Node(op="compare", params={"op": "<"},
                inputs={"left": data("VIX.Close"), "right": const(1e9)})


def _and(*conds: Node) -> Node:
    return Node(op="logic", params={"logic": "AND"},
                inputs={str(i): c for i, c in enumerate(conds)})


def _strategy(signal: Node) -> StrategyIR:
    return StrategyIR(signal=signal,
                      universe=Universe(kind="single", symbols=["069500"]),
                      position=PositionSpec(entry=Entry(mode="on_signal"),
                                            exit=Exit(hold_days=5)),
                      simulation=SimSpec(initial_capital=1e8))


# ── 엔진 E2E — 결함 시나리오 그대로 ──────────────────────────────────────────

def test_self_ts_mean_alive_with_cross_calendar_ref():
    """AND(가격>20MA[__SELF__], VIX<항상참) — 참조 매크로가 있어도 거래가 살아있어야 한다."""
    d = {"069500": _kr_df(), "VIX": _vix_df()}
    res = run_strategy_ir(_strategy(_and(_price_leg("__SELF__.Close"), _vix_leg())), d)
    assert res["success"], res.get("error")
    assert res["metrics"]["n_trades"] > 0, \
        "크로스캘린더 참조가 __SELF__ ts_* 신호를 전멸시켰다(항상 현금)"


def test_self_leg_parity_with_explicit_ref():
    """같은 데이터셋에서 __SELF__판 == 명시ref(069500.Close)판 — equity·거래수 동일."""
    d = {"069500": _kr_df(), "VIX": _vix_df()}
    a = run_strategy_ir(_strategy(_and(_price_leg("__SELF__.Close"), _vix_leg())), d)
    b = run_strategy_ir(_strategy(_and(_price_leg("069500.Close"), _vix_leg())), d)
    assert a["success"] and b["success"], (a.get("error"), b.get("error"))
    assert a["metrics"]["n_trades"] == b["metrics"]["n_trades"]
    ea, eb = a["equity"], b["equity"]
    assert list(ea.index) == list(eb.index)
    assert np.allclose(ea.to_numpy(), eb.to_numpy(), rtol=1e-9, atol=1e-6)


def test_pure_kr_unchanged_by_unreferenced_macro():
    """매크로 없는 순수 KR 트리는 데이터셋에 매크로가 실려 있어도 결과 불변(기존 의미 보존)."""
    sig = _price_leg("__SELF__.Close")
    a = run_strategy_ir(_strategy(sig), {"069500": _kr_df()})
    b = run_strategy_ir(_strategy(sig), {"069500": _kr_df(), "VIX": _vix_df()})
    assert a["success"] and b["success"], (a.get("error"), b.get("error"))
    assert a["metrics"]["n_trades"] == b["metrics"]["n_trades"] > 0
    assert np.allclose(a["equity"].to_numpy(), b["equity"].to_numpy(),
                       rtol=1e-9, atol=1e-6)


# ── 블록 단위 — 컨텍스트 스코핑 계약 ─────────────────────────────────────────

def test_from_dataset_universe_scopes_calendar_and_columns():
    kr, vix = _kr_df(), _vix_df()
    ctx = EvalContext.from_dataset({"069500": kr, "VIX": vix}, universe=["069500"])
    assert list(ctx.master_idx) == list(kr.index), "마스터 달력은 유니버스 달력이어야 한다"
    assert ctx.symbols == ["069500"], "참조 심볼은 패널 컬럼이 아니다"
    # __SELF__ ts_mean: 워밍업(20일) 이후 NaN 구멍이 없어야 한다.
    ma = evaluate(Node(op="ts_mean", params={"window": 20},
                       inputs={"signal": data("__SELF__.Close")}), ctx)
    assert not ma["069500"].iloc[20:].isna().any(), "유니버스 달력에서 ts_mean 전멸"
    # 참조 브로드캐스트는 ffill로 전 유니버스 일자를 채운다(KR 거래일 = US 휴장 포함).
    panel = evaluate(data("VIX.Close"), ctx)
    assert list(panel.columns) == ["069500"]
    assert not panel["069500"].isna().any()


def test_from_dataset_without_universe_keeps_legacy_union():
    kr, vix = _kr_df(), _vix_df()
    ctx = EvalContext.from_dataset({"069500": kr, "VIX": vix})
    assert set(ctx.symbols) == {"069500", "VIX"}
    assert set(kr.index) | set(vix.index) == set(ctx.master_idx)
