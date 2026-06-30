"""M2(#5·#9) — 단일종목 방향성 이벤트 스터디(+국면 라벨) 실행 잠금.

이벤트스터디 쿡북 레시피·few-shot(server/app/ir_compiler.py)이 모델에게 가르치는 IR 패턴이
엔진에서 실제로 실행되고 국면(by_regime)으로 분리되는지 결정적으로 잠근다 — 엔진 substrate가
조용히 깨지면 프롬프트가 깨진 패턴을 가르치게 되므로(원칙4: 검증된 해결책만).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from analysis_corpus import _noisy, _ohlc
from quant_core.ir_engine.run import run_query
from quant_core.ir_engine.spec import StrategyIR

_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}


def _ma(w):
    return {"op": "ts_mean", "params": {"window": w}, "inputs": {"signal": _C}}


def _two_regime_single():
    # 상승 추세 200일 + 하락 추세 200일 → ts_delta(120) 부호로 상승/하락 2국면 보장.
    close = np.concatenate([_noisy(200, vol=0.012, seed=1, drift=0.004),
                            _noisy(200, vol=0.012, seed=2, drift=-0.003)])
    return {"005930": _ohlc(close)}


def _run(study):
    ir = {"name": "t", "universe": {"kind": "single", "symbols": ["005930"]},
          "signal": _C, "query": "relate", "study": study}
    return run_query(StrategyIR.model_validate(ir), _two_regime_single())


def test_single_symbol_event_study_executes():
    # 가장 단순한 #9 케이스 — 라벨 없이도 단일종목 이벤트스터디가 실행돼야(IC 오매핑의 올바른 대안).
    event = {"op": "compare", "params": {"op": ">"}, "inputs": {"left": _C, "right": _ma(5)}}
    res = _run({"event": event, "windows": [5, 20], "event_basis": "close"})
    assert res["success"] and res["shape"] == "event_study"
    assert res["n_events"] > 0, f"이벤트가 발화해야: n_events={res['n_events']}"


def test_single_symbol_regime_event_study_splits_by_regime():
    # #5 아키타입 — 국면 라벨(bucket)을 붙이면 by_regime로 상승/하락 국면이 분리돼야.
    event = {"op": "compare", "params": {"op": ">"}, "inputs": {"left": _C, "right": _ma(5)}}
    label = {"op": "bucket", "params": {"edges": [0]},
             "inputs": {"signal": {"op": "ts_delta", "params": {"window": 120}, "inputs": {"signal": _C}}}}
    res = _run({"event": event, "windows": [5, 20], "event_basis": "close", "label": label})
    assert res["success"] and res["shape"] == "event_study" and res["n_events"] > 0
    regimes = res["by_regime"]["5"]["by_regime"]
    populated = [k for k, v in regimes.items() if isinstance(v, dict) and v.get("n", 0) > 0]
    assert len(populated) >= 2, f"상승·하락 두 국면이 분리돼야: {list(regimes.keys())}"
