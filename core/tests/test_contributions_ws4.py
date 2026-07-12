"""WS4 — 적립식 납입(contributions·DCA) 1급 지원 (IR 대수 캠페인).

prod 실수요(conv#23 '매달 100만원씩 매수'): 신규 자본 유입 모델 부재로 scheduled×fixed_amount가
0거래→equal_weight 대체되던 부류. 핵심 계약: ①주기 첫 거래일 현금 유입 ②지표=시간가중(TWR·
납입 왜곡 제거) ③원금대비 별도 동봉 ④contributions=None이면 기존 경로 byte-identical(골든).
"""
import numpy as np
import pandas as pd
import pytest

from quant_core.ir_engine.service import strategy_from_spec
from quant_core.ir_engine.spec import StrategyIR, validate_strategy

_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}
_ALWAYS = {"op": "compare", "params": {"op": ">"},
           "inputs": {"left": _C, "right": {"op": "const", "params": {"value": 0}}}}


def _df(mult=1.01, periods=70):
    idx = pd.bdate_range("2024-01-01", periods=periods)
    close = 100.0 * np.cumprod(np.full(periods, mult))
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close, "Volume": np.full(periods, 1e6)}, index=idx)


def _hold_spec(contrib=None, cap=1e8):
    spec = {"name": "적립", "universe": {"kind": "single", "symbols": ["TEST"]},
            "signal": _ALWAYS, "query": "simulate",
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "always"}, "exit": {}},
            "simulation": {"initial_capital": cap, "fill": "close"}}
    if contrib:
        spec["simulation"]["contributions"] = contrib
    return spec


def _errors(spec):
    return [i for i in validate_strategy(StrategyIR.model_validate(spec))
            if getattr(i, "is_error", False)]


# ── 검증기 ────────────────────────────────────────────────────────────────────

def test_validator_accepts_monthly_contribution():
    assert not _errors(_hold_spec({"amount": 1_000_000, "schedule": "monthly"}))


def test_validator_rejects_nonpositive_amount():
    assert _errors(_hold_spec({"amount": 0, "schedule": "monthly"}))


def test_validator_rejects_overlay_combo():
    spec = _hold_spec({"amount": 1_000_000, "schedule": "monthly"})
    spec["position"]["overlays"] = {"vol_target": 15.0}
    errs = _errors(spec)
    assert any("오버레이" in e.message for e in errs)


# ── e2e — TWR·원금대비 계약 ───────────────────────────────────────────────────

def test_contribution_metrics_twr_matches_no_contrib_run():
    """핵심 불변식: 상시보유 단일종목에선 TWR 지표가 무납입 동일 전략과 (거의) 같아야 한다 —
    납입이 '수익'으로 왜곡되면 이 등식이 깨진다(부류의 정의)."""
    ds = {"TEST": _df(1.005)}
    base = strategy_from_spec(_hold_spec(), ds)
    dca = strategy_from_spec(_hold_spec({"amount": 5_000_000, "schedule": "monthly"}), ds)
    assert base.get("success") and dca.get("success"), (base.get("error"), dca.get("error"))
    # TWR CAGR ≈ 무납입 CAGR (정수주 반올림·현금 잔고 차이로 소폭 오차 허용)
    assert dca["metrics"]["cagr"] == pytest.approx(base["metrics"]["cagr"], rel=0.15)
    # 납입 요약 계약
    ct = dca["contributions"]
    assert ct["n"] >= 3 and ct["total"] == pytest.approx(ct["n"] * 5_000_000)
    assert ct["total_invested"] == pytest.approx(1e8 + ct["total"])
    assert ct["final_value"] == pytest.approx(float(dca["equity"].iloc[-1]))
    # 실제 평가액(자본곡선)은 납입만큼 무납입보다 커야 한다
    assert float(dca["equity"].iloc[-1]) > float(base["equity"].iloc[-1])
    # 정직 경고 동봉
    assert any(w.get("code") == "contributions_twr" for w in dca.get("warnings") or [])


def test_contribution_flat_price_profit_pct_near_zero():
    """가격 불변(수익 0) + 납입 — 원금대비 ≈ 0%·TWR 누적 ≈ 0%(입금이 수익으로 잡히면 크게 양수)."""
    ds = {"TEST": _df(1.0)}
    res = strategy_from_spec(_hold_spec({"amount": 10_000_000, "schedule": "monthly"}), ds)
    assert res.get("success"), res.get("error")
    assert abs(res["contributions"]["profit_pct"]) < 1.0
    assert abs(res["metrics"]["cum_return"] if "cum_return" in res["metrics"]
               else res["metrics"]["total_return"]) < 1.0


def test_no_contribution_result_has_no_contrib_keys():
    """contributions=None → 결과에 납입 키·경고 없음(기존 경로 무영향·골든 보존의 표면 확인)."""
    res = strategy_from_spec(_hold_spec(), {"TEST": _df(1.005)})
    assert res.get("success")
    assert "contributions" not in res
    assert not any((w or {}).get("code") == "contributions_twr"
                   for w in res.get("warnings") or [])


def test_contribution_on_signal_path_also_injects():
    """on_signal(이벤트) 경로에도 동일 주입 — 골든크로스류 이벤트 전략의 적립식."""
    ds = {"TEST": _df(1.005)}
    spec = _hold_spec({"amount": 5_000_000, "schedule": "monthly"})
    spec["position"]["entry"] = {"mode": "on_signal"}
    spec["position"]["exit"] = {"hold_days": 5}
    res = strategy_from_spec(spec, ds)
    assert res.get("success"), res.get("error")
    assert res["contributions"]["n"] >= 3
    assert res["contributions"]["final_value"] == pytest.approx(float(res["equity"].iloc[-1]))
