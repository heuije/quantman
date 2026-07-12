"""캠페인 소형 3건 — G3 증거금 오버라이드 · 달력 신호 게이트 광고 · exit.fill 광고.

prod 근거: G3=conv#47/#25('증거금 10%로 10배 감안' 정직 거부) · 달력=Part1 L2-a(엔진은 되는데
광고가 '분할용'뿐이라 Sell in May류 미컴파일) · exit.fill=conv#49 부류의 NL 노출 완결(D5).
"""
import numpy as np
import pandas as pd
import pytest

from quant_core.ir_engine.service import strategy_from_spec
from quant_core.ir_engine.spec import StrategyIR, validate_strategy

_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}
_ALWAYS = {"op": "compare", "params": {"op": ">"},
           "inputs": {"left": _C, "right": {"op": "const", "params": {"value": 0}}}}


def _fut_spec(override=None):
    spec = {"name": "선물", "universe": {"kind": "single", "symbols": ["코스피200선물"]},
            "signal": _ALWAYS, "query": "simulate",
            "position": {"direction": "long",
                         "sizing": {"mode": "equal_weight", "futures_margin_pct": 100.0},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 10}},
            "simulation": {"initial_capital": 5e8}}
    if override is not None:
        spec["simulation"]["margin_rate_override"] = override
    return spec


def _fut_df(periods=60):
    idx = pd.bdate_range("2024-01-01", periods=periods)
    close = np.linspace(300.0, 330.0, periods)      # 지수포인트(+10%)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close, "Volume": np.full(periods, 1e5)}, index=idx)


def _errors(spec):
    return [i for i in validate_strategy(StrategyIR.model_validate(spec))
            if getattr(i, "is_error", False)]


# ── G3 — 증거금률 오버라이드 ─────────────────────────────────────────────────

def test_margin_override_validator_domain():
    assert not _errors(_fut_spec(0.10))
    assert _errors(_fut_spec(0.0))
    assert _errors(_fut_spec(1.5))


def test_margin_override_scales_futures_exposure():
    """override 0.1(10배)은 카탈로그(~7.8%→기본과 유사할 수 있으니) 0.5(2배) 대비 노출이 커야 —
    같은 데이터·자본에서 |누적수익|이 증거금률에 반비례해 커진다."""
    ds = {"코스피200선물": _fut_df()}
    low = strategy_from_spec(_fut_spec(0.10), ds)    # 증거금 10% ≈ 10배
    high = strategy_from_spec(_fut_spec(0.50), ds)   # 증거금 50% ≈ 2배
    assert low.get("success") and high.get("success"), (low.get("error"), high.get("error"))
    tr_low = low["metrics"]["total_return"]
    tr_high = high["metrics"]["total_return"]
    assert tr_low > tr_high > 0                       # 상승장 롱 — 낮은 증거금률=더 큰 노출


def test_margin_override_ignored_for_stocks():
    """주식 심볼엔 무영향(증거금 1.0 불변) — override가 주식 회계를 오염하면 안 된다."""
    idx = pd.bdate_range("2024-01-01", periods=40)
    close = np.linspace(100, 110, 40)
    df = pd.DataFrame({"Open": close, "High": close, "Low": close,
                       "Close": close, "Volume": np.full(40, 1e6)}, index=idx)
    spec = {"name": "주식", "universe": {"kind": "single", "symbols": ["005930"]},
            "signal": _ALWAYS, "query": "simulate",
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "always"}, "exit": {}},
            "simulation": {"initial_capital": 1e8}}
    base = strategy_from_spec(spec, {"005930": df})
    spec2 = {**spec, "simulation": {**spec["simulation"], "margin_rate_override": 0.10}}
    over = strategy_from_spec(spec2, {"005930": df})
    assert base.get("success") and over.get("success")
    assert over["metrics"]["total_return"] == pytest.approx(base["metrics"]["total_return"])


def test_margin_override_reflected_in_self_description():
    """자기서술(execution_summary·methodology_brief)이 override 값을 서술해야 한다 —
    엔진은 10%로 계산하는데 방법론이 카탈로그 20%를 말하면 봇이 '미반영' 오판(G3 스모크 실측)."""
    from quant_core.ir_engine.execution_summary import execution_summary, methodology_brief
    over = execution_summary(_fut_spec(0.10))
    lev = next(d for d in over["assumed"] if d["label"] == "레버리지(선물)")
    assert "10%" in lev["value"] and "오버라이드" in lev["value"]
    assert "최대 10.0x" in lev["value"]                # 1/0.10 — 카탈로그가 아닌 override 기준
    assert "개시증거금률 10%" in methodology_brief(_fut_spec(0.10))
    base = next(d for d in execution_summary(_fut_spec())["assumed"]
                if d["label"] == "레버리지(선물)")
    assert "오버라이드" not in base["value"]            # 미지정 시 카탈로그 서술 불변


# ── 광고(달력 게이트·exit.fill·오버라이드) — 컴파일러가 배우는 표면 ────────────

def test_calendar_block_advertises_signal_gate():
    from quant_core.blocks.catalog import get
    doc = get("calendar").doc
    assert "신호 게이트" in doc and "is_in" in doc      # 분할용으로만 광고되던 L2-a 마감


def test_capabilities_advertise_exit_fill_and_margin_override():
    from quant_core.ir_engine.capabilities import capability_spec
    caps = capability_spec()
    assert "fill" in caps["exit"]["knobs"]              # exit.fill NL 노출(D5 완결)
    assert "오버나이트" in caps["exit"]["knobs"]["fill"]
    mo = caps["margin_override"]
    assert "백테스트 전용" in mo["use_for"]
