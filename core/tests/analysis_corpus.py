"""분석·엑셀 진단 코퍼스 (LLM-free) — 챗봇 결정적 코어의 단일 진실원천.

챗봇 파이프라인 중 LLM(자연어→IR)을 뺀 결정적 2·3단계(IR→엔진→결과→요약/엑셀)를 고정 IR
픽스처로 $0·재현가능하게 검증한다. 한 곳(이 파일)에 정의하고 ① pytest(회귀 어설션)와
② 진단 CLI(scripts/analysis_diag.py — 실 xlsx 생성+리포트)가 함께 소비한다.

- BASE_CASES: 13 result_shape(엑셀 구조 회귀; test_ir_excel_export_shapes가 run_query로 소비).
- EDGE_CASES: 이번 세션 핵심(크로스에셋 신호패널·입력 역할태깅·데이터품질 경고) — 챗봇 정본
  경로(strategy_from_spec)로만 의미 있는 것.
- CASES = BASE + EDGE. run_case()는 챗봇과 동일하게 strategy_from_spec을 탄다.

데이터: 합성(결정적, 항상 동작) 기본 + frozen 실데이터 스냅샷(fixtures/frozen/*.csv, 있으면 --real).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine import StrategyIR, build_strategy_excel, strategy_from_spec
from quant_core.ir_engine.summarize import result_shape

_FROZEN = Path(__file__).resolve().parent / "fixtures" / "frozen"


# ── 합성 데이터 빌더 (결정적) ─────────────────────────────────────────────────
def _ohlc(close, extra=None) -> pd.DataFrame:
    n = len(close)
    idx = pd.date_range("2020-01-02", periods=n, freq="B")
    close = np.asarray(close, dtype=float)
    op = np.concatenate([[close[0]], close[:-1]])
    d = {"Open": op, "High": np.maximum(op, close) * 1.001,
         "Low": np.minimum(op, close) * 0.999, "Close": close, "Volume": np.full(n, 1e6)}
    for k, v in (extra or {}).items():
        d[k] = np.full(n, v) if np.isscalar(v) else np.asarray(v, dtype=float)
    return pd.DataFrame(d, index=idx)


def _geom(drift, n):
    return 100.0 * (1 + drift) ** np.arange(n)


def _noisy(n, vol=0.01, seed=0, drift=0.0):
    """결정적 랜덤워크 — 일별 변동이 ±0.1%를 충분히 넘나들어 임계 신호가 실제 발화."""
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, vol, n)
    return 100.0 * np.cumprod(1 + rets)


def ds_factor(n=300):
    return {"AAA": _ohlc(_geom(0.0010, n), {"momentum_12_1m": 3.0}),
            "BBB": _ohlc(_geom(-0.0005, n), {"momentum_12_1m": 1.0}),
            "CCC": _ohlc(_geom(0.0004, n), {"momentum_12_1m": 2.0})}


def ds_factor_kr(n=300):
    return {"005930": _ohlc(_geom(0.0010, n), {"momentum_12_1m": 3.0}),
            "000660": _ohlc(_geom(-0.0005, n), {"momentum_12_1m": 1.0}),
            "105560": _ohlc(_geom(0.0004, n), {"momentum_12_1m": 2.0})}


def ds_fund():
    pbs = {"AAA": 0.8, "BBB": 3.0, "CCC": 1.2, "DDD": 0.5, "EEE": 2.0}
    return {s: _ohlc(_geom(0.0, 120), {"pb_ratio": pb, "market_cap": 9e11}) for s, pb in pbs.items()}


def ds_single():
    return {"AAA": _ohlc(_geom(0.001, 260), {"pb_ratio": 1.5, "trailing_pe": 12.0})}


def ds_pf():
    return {"AAA": _ohlc(_geom(0.001, 120), {"pb_ratio": 1.0}),
            "BBB": _ohlc(_geom(-0.0005, 120), {"pb_ratio": 2.0}),
            "CCC": _ohlc(_geom(0.0008, 120), {"pb_ratio": 3.0})}


def ds_relate():
    out = {}
    for s, fv, gv in [("AAA", -0.02, 0.5), ("BBB", -0.01, -0.3), ("CCC", 0.0, 0.1),
                      ("DDD", 0.01, -0.2), ("EEE", 0.02, 0.4)]:
        r = (1 + 2 * fv) ** (1 / 5.0) - 1
        out[s] = _ohlc(_geom(r, 120), {"fac1": fv, "fac2": gv})
    return out


def ds_cross_asset(n=520):
    """크로스에셋: 코스피200선물(대상) + S&P500(신호) — 둘 다 노이즈로 임계 발화.

    신호가 참조하는 S&P500.pct_change_1d 지표를 미리 계산해 넣는다(직접 전달 데이터셋은 엔진이
    지표를 자동 산출하지 않으므로 — base 케이스가 momentum_12_1m을 넣는 것과 동일 패턴).
    """
    sp_close = _noisy(n, 0.013, seed=2)
    sp_pc = pd.Series(sp_close).pct_change(1).to_numpy() * 100   # indicators.pct_change_1d 정의와 동일
    return {"코스피200선물": _ohlc(_noisy(n, 0.011, seed=1, drift=0.0003)),
            "S&P500": _ohlc(sp_close, {"pct_change_1d": sp_pc})}


def ds_cross_asset_stale(n=520, stale_cut=70):
    """신호종목(S&P500)이 대상보다 일찍 끝남 → assess_data_quality stale_data 발화."""
    ds = ds_cross_asset(n)
    ds["S&P500"] = ds["S&P500"].iloc[:-stale_cut]
    return ds


def ds_kr_futures(n=520):
    """단일 종목 코스피200선물(합성). real 스냅샷(번들 CSV)으로 교체 가능한 케이스용."""
    return {"코스피200선물": _ohlc(_noisy(n, 0.011, seed=3, drift=0.0002))}


# ── 신호·포지션 IR 조각 ───────────────────────────────────────────────────────
SIG_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}
SIG_M = {"op": "data", "params": {"ref": "__SELF__.momentum_12_1m"}}
MA5 = {"op": "ts_mean", "params": {"window": 5}, "inputs": {"signal": SIG_C}}
MA20 = {"op": "ts_mean", "params": {"window": 20}, "inputs": {"signal": SIG_C}}
TREND = {"op": "compare", "params": {"op": ">"}, "inputs": {"left": SIG_C, "right": MA20}}
EVENT = {"op": "compare", "params": {"op": ">"}, "inputs": {"left": SIG_C, "right": MA5}}
POS = {"direction": "long", "sizing": {"mode": "equal_weight"},
       "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}}
POS3 = {**POS, "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 3}}
SIM = {"initial_capital": 1e7}
LST3 = {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]}
ALL = {"kind": "all"}

# 크로스에셋 역추종: S&P500 전일대비 ≤-0.1%→롱(+1), ≥+0.1%→숏(-1), 사이→관망(0).
_SP = {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}}
CROSS_SIG = {"op": "select", "inputs": {
    "cond": {"op": "compare", "params": {"op": "<="}, "inputs": {"left": _SP, "right": {"op": "const", "params": {"value": -0.1}}}},
    "a": {"op": "const", "params": {"value": 1}},
    "b": {"op": "select", "inputs": {
        "cond": {"op": "compare", "params": {"op": ">="}, "inputs": {"left": _SP, "right": {"op": "const", "params": {"value": 0.1}}}},
        "a": {"op": "const", "params": {"value": -1}}, "b": {"op": "const", "params": {"value": 0}}}}}}
CROSS_POS = {"direction": "long_short", "sizing": {"mode": "equal_weight"},
             "entry": {"mode": "on_signal", "threshold": 0}, "exit": {"hold_days": 0}}
CROSS_UNI = {"kind": "single", "symbols": ["코스피200선물"]}


# ── 코퍼스 ────────────────────────────────────────────────────────────────────
# 각 case: {name, desc, ds(builder), ir(dict), checks{sheet:[needle...]}, real(심볼리스트|None)}
BASE_CASES = [
    dict(name="simulate", desc="3종목 모멘텀 매수후보유(단일 백테스트)", ds=ds_factor,
         ir={"universe": LST3, "signal": SIG_M, "position": POS, "simulation": SIM, "query": "simulate", "study": {"axis": "none"}},
         checks={"백테스트": ["일일 추가비용", "=(E", "STDEV.S"], "거래내역": ["종목"], "지표·설명": ["엔진 지표"]}),
    dict(name="sweep_parameter", desc="수수료 파라미터 스윕", ds=ds_factor,
         ir={"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM, "query": "simulate",
             "study": {"axis": "parameter", "param_grid": [{"path": "simulation.commission", "values": [0.0, 0.001]}]}},
         checks={"스윕결과": ["commission=0.0", "commission=0.001", "CAGR(%)"], "설명": ["방법론"]}),
    dict(name="sweep_entity", desc="종목별 독립 백테스트 스윕", ds=ds_factor,
         ir={"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM, "query": "simulate",
             "study": {"axis": "entity", "assets": ["AAA", "BBB", "CCC"]}},
         checks={"스윕결과": ["AAA", "BBB", "CCC", "샤프"], "설명": ["방법론"]}),
    dict(name="sweep_label", desc="조건(섹터)별 성과 대조", ds=ds_factor_kr,
         ir={"universe": {"kind": "list", "symbols": ["005930", "000660", "105560"]}, "signal": SIG_M, "position": POS3,
             "simulation": SIM, "query": "simulate",
             "study": {"axis": "label", "reduction": "contrast", "label": {"op": "attribute", "params": {"attr": "Industry"}}}},
         checks={"조건별성과": ["전체(포트폴리오)", "조건"], "설명": ["조건 대조"]}),
    dict(name="period_split", desc="연도별 성과 분할(라이브 수식)", ds=ds_factor,
         ir={"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM, "query": "simulate",
             "study": {"axis": "time_fold", "reduction": "consistency", "split_period": "year"}},
         checks={"연도별성과": ["연도", "엑셀 연수익", "2020"], "백테스트": ["라이브", "STDEV.S"], "원자료": ["원자료"]}),
    dict(name="extremize", desc="목적함수 최적화(OOS 가드)", ds=ds_factor,
         ir={"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM, "query": "simulate",
             "study": {"axis": "entity", "reduction": "extremize", "assets": ["AAA", "BBB", "CCC"],
                       "objective": {"metric": "cum_return", "direction": "max", "oos_guard": True}}},
         checks={"최적화결과": ["최적", "순위", "AAA"], "OOS검증": ["구간"], "설명": ["최적화"]}),
    dict(name="select", desc="저PBR 스크리닝(as-of 랭킹)", ds=ds_fund,
         ir={"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
             "signal": {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}, "query": "select",
             "select": {"top_n": 3, "descending": False, "display": ["pb_ratio"]}},
         checks={"스크리닝결과": ["순위", "DDD", "pb_ratio"], "설명": ["스크리닝"]}),
    dict(name="describe_single", desc="단일종목 분석(52주·변동성 라이브)", ds=ds_single,
         ir={"universe": {"kind": "single", "symbols": ["AAA"]}, "signal": SIG_C, "query": "describe"},
         checks={"종목요약": ["현재가", "52주 고가", "=MAX(원자료", "STDEV.S(원자료"], "원자료": ["=B5/B4-1"], "설명": ["종목 분석"]}),
    dict(name="describe_portfolio", desc="포트폴리오 진단(HHI·가중 라이브)", ds=ds_pf,
         ir={"universe": {"kind": "portfolio", "symbols": ["AAA", "BBB", "CCC"], "weights": {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}},
             "signal": SIG_C, "query": "describe"},
         checks={"보유진단": ["=SUMPRODUCT(B4:B6,B4:B6)", "가중PBR"], "섹터노출": ["섹터"], "설명": ["포트폴리오"]}),
    dict(name="describe_signal", desc="신호값 분포(분위·히스토그램)", ds=ds_factor,
         ir={"universe": ALL, "signal": SIG_C, "query": "describe", "study": {"target_node": SIG_M}},
         checks={"신호분포": ["분위수", "q05"], "히스토그램": ["빈도"], "설명": ["신호 분포"]}),
    dict(name="relate_ic", desc="팩터 IC(신호↔미래수익 횡단상관)", ds=ds_relate,
         ir={"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]}, "signal": SIG_C, "query": "relate",
             "study": {"relation_kind": "ic", "target_node": {"op": "data", "params": {"ref": "__SELF__.fac1"}}, "windows": [5, 10]}},
         checks={"IC분석": ["윈도(일)", "평균IC(%)", "IR"], "설명": ["IC"]}),
    dict(name="relate_regression", desc="다중팩터 Fama-MacBeth 회귀", ds=ds_relate,
         ir={"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]}, "signal": SIG_C, "query": "relate",
             "study": {"relation_kind": "regression",
                       "factors": [{"op": "data", "params": {"ref": "__SELF__.fac1"}}, {"op": "data", "params": {"ref": "__SELF__.fac2"}}],
                       "windows": [5, 10]}},
         checks={"회귀분석": ["fac1", "fac2", "계수(coef)"], "설명": ["Fama-MacBeth"]}),
    dict(name="relate_event", desc="이벤트 스터디(전후 수익·MAE/MFE)", ds=ds_factor,
         ir={"universe": LST3, "signal": SIG_C, "query": "relate", "study": {"event": EVENT, "windows": [5, 10], "event_basis": "close"}},
         checks={"이벤트분석": ["윈도(일)", "평균MAE(%)", "페이오프"], "설명": ["이벤트 스터디"]}),
]

EDGE_CASES = [
    dict(name="cross_asset_intraday", desc="★ S&P500 신호→코스피200선물 당일 롱숏(신호·결정 패널·입력 역할태깅)",
         ds=ds_cross_asset, real=["코스피200선물", "S&P500"],
         ir={"universe": CROSS_UNI, "signal": CROSS_SIG, "position": CROSS_POS,
             "simulation": {"initial_capital": 1e8, "fill": "next_open"}, "query": "simulate"},
         checks={"원자료": ["S&P500 종가(신호)", "코스피200선물 시가(대상)"],
                 "신호·결정": ["S&P500.pct_change_1d (신호값)", "포지션 점수(룰)"]}),
    dict(name="cross_asset_period_split", desc="★ 위 전략 연도별 분할(신호패널+연도별 라이브수식)",
         ds=ds_cross_asset, real=["코스피200선물", "S&P500"],
         ir={"universe": CROSS_UNI, "signal": CROSS_SIG, "position": CROSS_POS,
             "simulation": {"initial_capital": 1e8, "fill": "next_open"}, "query": "simulate",
             "study": {"axis": "time_fold", "reduction": "consistency", "split_period": "year"}},
         checks={"연도별성과": ["연도", "엑셀 연수익"], "신호·결정": ["포지션 점수(룰)"], "원자료": ["S&P500 종가(신호)"]}),
    dict(name="data_quality_stale", desc="★ 신호종목 stale → 데이터품질 경고 표면화(Phase 0.5/3)",
         ds=ds_cross_asset_stale,
         ir={"universe": CROSS_UNI, "signal": CROSS_SIG, "position": CROSS_POS,
             "simulation": {"initial_capital": 1e8, "fill": "next_open"}, "query": "simulate"},
         checks={"지표·설명": ["⚠", "S&P500"]}),
    dict(name="kr_futures_trend", desc="코스피200선물 추세추종(Close>MA20) — frozen 실데이터 스냅샷 지원",
         ds=ds_kr_futures, real=["코스피200선물"],
         ir={"universe": {"kind": "single", "symbols": ["코스피200선물"]},
             "signal": TREND, "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
             "entry": {"mode": "on_signal"}, "exit": {"hold_days": 10}},
             "simulation": {"initial_capital": 1e8}, "query": "simulate"},
         checks={"백테스트": ["일일 추가비용", "=(E"], "거래내역": ["종목"]}),
]

CASES = BASE_CASES + EDGE_CASES


# ── 실행 헬퍼 (챗봇 정본 경로 = strategy_from_spec) ────────────────────────────
def frozen_dataset(symbols: list[str]) -> dict | None:
    """fixtures/frozen/<symbol>.csv 들을 로드. 하나라도 없으면 None(해당 케이스 실데이터 스킵)."""
    out = {}
    for s in symbols:
        p = _FROZEN / f"{s}.csv"
        if not p.exists():
            return None
        df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
        out[s] = df
    return out


def run_case(case: dict, real: bool = False):
    """케이스를 챗봇과 동일 경로(strategy_from_spec)로 실행. (ir, dataset, result, xlsx|None, mode)."""
    dataset, mode = None, "synthetic"
    if real and case.get("real"):
        dataset = frozen_dataset(case["real"])
        mode = "real" if dataset is not None else "synthetic(real 스냅샷 없음)"
    if dataset is None:
        dataset = case["ds"]()
    ir = StrategyIR.model_validate(case["ir"])
    res = strategy_from_spec(case["ir"], dataset)
    xlsx = build_strategy_excel(ir, dataset, res) if res.get("success") else None
    return ir, dataset, res, xlsx, mode


def case_shape(result: dict) -> str:
    return result_shape(result)
