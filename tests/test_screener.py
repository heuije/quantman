"""백테스트 스크리너 유니버스(PIT) — 단일 선별 조건(condition) 검증.

스크리너 = 필터·횡단순위(rank 블록)를 AND/OR로 조합한 단일 condition의 시점별 자격 마스크.
각 리밸런스 시점에 그 날의 값으로 PIT 판정. rank 블록의 unit(개수/분위)·descending으로
상위 N개·상위 X% 모두 한 프리미티브로 표현(별도 rank struct 없음).

    cd platform && pytest tests/test_screener.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from quant_core.blocks import EvalContext            # noqa: E402
from quant_core.ir_engine import strategy_from_spec  # noqa: E402
from quant_core.ir_engine.engine import _screener_mask, _apply_refresh  # noqa: E402


def _ds():
    """A·B는 초기 대형(시총↓ 추세), C·D는 후기 대형(시총↑) — 시점별 자격이 뒤집히게."""
    idx = pd.date_range("2021-01-01", periods=300, freq="B")
    t = np.arange(300)

    def mk(mc, mom):
        return pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 1e6,
                             "momentum_12_1m": float(mom), "market_cap": mc}, index=idx)
    return {"A": mk(100 - 0.1 * t, 5), "B": mk(95 - 0.1 * t, 4),
            "C": mk(10 + 0.3 * t, 3), "D": mk(8 + 0.3 * t, 2),
            # F-01: A~D는 비숫자 심볼이라 통화 SSOT(instrument_spec)상 USD로 분류된다 —
            # fixed_amount(₩정액) 사이징 테스트가 환산용 원달러환율 시계열을 요구한다.
            "원달러환율": pd.DataFrame({"Close": 1300.0}, index=idx)}


# ── 조건 빌더 (프리미티브 조합) ────────────────────────────────────────────────

def _data(ref):
    return {"op": "data", "params": {"ref": ref}}

def _const(v):
    return {"op": "const", "params": {"value": v}}

def _rank_cond(ref, cut, descending=True, unit="count", op="<="):
    """횡단순위 선별: rank(ref, descending, unit) op cut. 상위 N개=count·desc, 상위 X%=pct·desc."""
    return {"op": "compare", "params": {"op": op},
            "inputs": {"left": {"op": "rank",
                                "params": {"descending": descending, "unit": unit},
                                "inputs": {"signal": _data(ref)}},
                       "right": _const(cut)}}

def _cmp(ref, op, val):
    return {"op": "compare", "params": {"op": op},
            "inputs": {"left": _data(ref), "right": _const(val)}}

def _and(*conds):
    return {"op": "logic", "params": {"logic": "AND"},
            "inputs": {str(i): c for i, c in enumerate(conds)}}


def _spec(condition) -> dict:
    return {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": condition}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 1e7}}


def _spec_list(condition, refresh="each_rebalance"):
    """list 유니버스(A..D) + 세부조건 — 선택 종목 ∩ 조건."""
    return {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": condition, "refresh": refresh}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 4}},
            "simulation": {"initial_capital": 1e7}}


# ── 실행 ───────────────────────────────────────────────────────────────────────

def test_screener_rank_topn_runs():
    res = strategy_from_spec(_spec(_rank_cond("market_cap", 2)), _ds())
    assert res["success"], res
    assert len(res["equity"]) > 0


def test_screener_filter_threshold_runs():
    res = strategy_from_spec(_spec(_cmp("market_cap", ">", 50.0)), _ds())
    assert res["success"], res


def test_screener_filter_and_rank_combined_runs():
    """필터 ∧ 횡단순위를 한 조건으로 — 거래대금류 필터 + 시총 상위 N."""
    cond = _and(_cmp("market_cap", ">", 5.0), _rank_cond("market_cap", 2))
    res = strategy_from_spec(_spec(cond), _ds())
    assert res["success"], res


def test_list_screener_dynamic_runs():
    res = strategy_from_spec(_spec_list(_rank_cond("market_cap", 2)), _ds())
    assert res["success"], res
    assert len(res["equity"]) > 0


def test_list_screener_static_runs():
    res = strategy_from_spec(_spec_list(_rank_cond("market_cap", 2), "once_at_start"), _ds())
    assert res["success"], res


# ── PIT 자격 (시점별) ──────────────────────────────────────────────────────────

def test_screener_pit_eligibility_flips_count():
    """상위 N개(count·desc) — 시점값으로 자격 판정(PIT), 정적 스냅샷 아님."""
    ctx = EvalContext.from_dataset(_ds())
    elig = _screener_mask({"condition": _rank_cond("market_cap", 2)}, ctx, ["A", "B", "C", "D"])
    assert [c for c in elig.columns if elig.iloc[0][c]] == ["A", "B"]    # 초기 대형
    assert [c for c in elig.columns if elig.iloc[-1][c]] == ["C", "D"]   # 후기 대형


def test_screener_pit_eligibility_percentile():
    """상위 X%(pct·desc) — 4종목 상위 50% = 2종목. count와 동일 결과여야."""
    ctx = EvalContext.from_dataset(_ds())
    elig = _screener_mask({"condition": _rank_cond("market_cap", 0.5, unit="pct")},
                          ctx, ["A", "B", "C", "D"])
    assert [c for c in elig.columns if elig.iloc[0][c]] == ["A", "B"]
    assert [c for c in elig.columns if elig.iloc[-1][c]] == ["C", "D"]


def test_screener_rank_ascending_selects_smallest():
    """descending=False → 작은 값이 상위. 시총 하위 2개(초기 C·D)."""
    ctx = EvalContext.from_dataset(_ds())
    elig = _screener_mask({"condition": _rank_cond("market_cap", 2, descending=False)},
                          ctx, ["A", "B", "C", "D"])
    assert [c for c in elig.columns if elig.iloc[0][c]] == ["C", "D"]    # 초기 소형


# ── 재선별 시점 (동적/정적) ────────────────────────────────────────────────────

def test_apply_refresh_dynamic_passthrough():
    """each_rebalance — 마스크 그대로."""
    ctx = EvalContext.from_dataset(_ds())
    m = _screener_mask({"condition": _rank_cond("market_cap", 2)}, ctx, ["A", "B", "C", "D"])
    out = _apply_refresh(m, "each_rebalance", None)
    assert out.equals(m)


def test_apply_refresh_static_freezes_first_row():
    """once_at_start — 시작시점(첫 행) 자격을 전 기간 고정. 시점 자격이 뒤집혀도 불변."""
    ctx = EvalContext.from_dataset(_ds())
    m = _screener_mask({"condition": _rank_cond("market_cap", 2)}, ctx, ["A", "B", "C", "D"])
    out = _apply_refresh(m, "once_at_start", None)
    first = [c for c in m.columns if m.iloc[0][c]]              # 초기 대형 A,B
    assert first == ["A", "B"]
    # 모든 행이 첫 행과 동일(바스켓 고정)
    assert all([c for c in out.columns if out.iloc[k][c]] == first
               for k in range(len(out.index)))
    # 동적과 달리 마지막 행이 C,D로 뒤집히지 않음
    assert [c for c in out.columns if out.iloc[-1][c]] == first


def test_apply_refresh_static_empty_when_start_past_window():
    """once_at_start + start가 전 구간 이후 → 빈 바스켓(전부 False, 거래 없음)."""
    ctx = EvalContext.from_dataset(_ds())
    m = _screener_mask({"condition": _rank_cond("market_cap", 2)}, ctx, ["A", "B", "C", "D"])
    out = _apply_refresh(m, "once_at_start", "2099-01-01")
    assert out.shape == m.shape
    assert not out.to_numpy().any()   # 전부 False


# ── 검증 게이트 ────────────────────────────────────────────────────────────────

def test_screener_invalid_refresh_rejected():
    """refresh 값이 enum 밖이면 에러."""
    spec = _spec(_rank_cond("market_cap", 2))
    spec["universe"]["screener"]["refresh"] = "bogus"
    res = strategy_from_spec(spec, _ds())
    assert not res["success"]
    assert any(i["rule"] == "S-univ" for i in res["issues"])


def test_screener_on_all_is_allowed():
    """kind=all + 세부조건 = '전 종목을 조건으로 필터' — 엔진 _screener_mask가 전 종목에 적용(honor).

    옛 'screener requires symbols' 과대제약 폐기: 스크리너는 universe *필터*지 list *정제*가
    아니다. kind=all이면 전 종목이 곧 후보집합이고 스크리너가 그걸 거른다(run_select·simulate
    엔진 모두 지원). list/single 빈 symbols는 각 유니버스 구조 규칙이 따로 거부.
    """
    spec = {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "all",
                         "screener": {"condition": _rank_cond("market_cap", 2)}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 1e7}}
    res = strategy_from_spec(spec, _ds())
    assert not any(i["rule"] == "S-univ" for i in res.get("issues", [])), \
        f"kind=all+스크리너는 S-univ로 거부되면 안 됨: {res.get('issues')}"


def test_event_with_screener_allowed_in_backtest():
    """이벤트 진입 + 세부조건은 백테스트 허용(라이브 차단은 서버 가드 담당)."""
    spec = {"signal": {"op": "compare", "params": {"op": ">"},
                       "inputs": {"left": _data("momentum_12_1m"), "right": _const(0)}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": _rank_cond("market_cap", 2)}},
            "position": {"direction": "long", "sizing": {"mode": "fixed_amount", "amount_krw": 1e6},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 5}},
            "simulation": {"initial_capital": 1e7}}
    res = strategy_from_spec(spec, _ds())
    assert res["success"], res


def test_event_screener_gates_entry():
    """이벤트 진입 + 세부조건: 자격 False인 종목/날은 신호가 참이어도 진입 차단.

    신호=항상참(momentum>0). 세부조건=시총 상위 2(count). 초기엔 A,B만 자격 →
    C,D는 신호 참이어도 미보유. trades에 자격 종목만 등장.
    """
    spec = {"signal": {"op": "compare", "params": {"op": ">"},
                       "inputs": {"left": _data("momentum_12_1m"), "right": _const(0)}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": _rank_cond("market_cap", 2),
                                      "refresh": "each_rebalance"}},
            "position": {"direction": "long", "sizing": {"mode": "fixed_amount", "amount_krw": 1e6},
                         "entry": {"mode": "on_signal"},
                         "exit": {"hold_days": 5}},
            "simulation": {"initial_capital": 1e7, "fill": "close"}}
    res = strategy_from_spec(spec, _ds())
    assert res["success"], res
    # 첫 보름(초기 대형 A·B 자격 구간)의 진입 종목엔 C·D 없음
    # (res["trades"]는 DataFrame — 진입일 컬럼으로 초기 구간 필터)
    tr = res["trades"]
    early = tr[tr["진입일"] <= pd.Timestamp("2021-02-01")]
    assert len(early) and early["종목"].isin(("A", "B")).all(), early


def test_list_screener_window_not_full():
    """list+세부조건은 전체 로드(None) 아님 — 선택 종목 + 조건 참조만."""
    from quant_core.ir_engine.spec import StrategyIR, needed_symbols
    s = StrategyIR.model_validate(_spec_list(_rank_cond("market_cap", 2)))
    win = needed_symbols(s)
    assert win is not None
    assert {"A", "B", "C", "D"}.issubset(win)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
