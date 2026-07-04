"""IR 라벨 dtype 부류 잠금 — 범주형(섹터 attribute) 라벨의 러너 전체 계약 통일.

실사건(2026-07-05): 챗 "외국인 순매수가 가장 높은 섹터들을 2020~2026 조사" → describe 신호분포가
study.label=attribute('Sector')를 받아 _run_signal_study의 to_numpy(dtype=float)에서
ValueError: could not convert string to float: '자동차' 크래시(prod 동일 코드).

구조 뿌리: ValueType.LABEL에 dtype 차원이 없어(숫자 bucket/calendar·문자열 attribute 동일 타입)
capabilities가 label=attribute('Sector')를 광고하는데, 라벨 소비가 러너마다 사적 구현이라
simulate(axis=label)만 범주형을 처리하고 신호분포·이벤트스터디는 float 강제(크래시), IC는
첫 컬럼을 전 시장 국면으로 가정(조용한 오답)했다. 이 파일이 계약을 러너 전체로 잠근다:
분포 분할이 의미 있는 곳(신호분포·이벤트스터디)은 범주형 지원, 의미상 불가한 곳(IC 종목축
라벨·target 자체가 범주형)은 fail-loud. 설계=퀀트 워크스페이스 ir_label_dtype_redesign.md.

    cd platform/core && python -m pytest tests/test_ir_label_categorical.py -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis_corpus import _noisy, _ohlc
from quant_core.ir_engine.run import run_query
from quant_core.ir_engine.spec import StrategyIR

_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}
_FNB = {"op": "data", "params": {"ref": "__SELF__.foreign_net_buy"}}
_SECTOR = {"op": "attribute", "params": {"attr": "Sector"}}
_GROUPS = {"AAA": "반도체", "BBB": "반도체", "CCC": "자동차", "DDD": "자동차"}


@pytest.fixture(autouse=True)
def _sector_map(monkeypatch):
    # attribute op는 ops_advanced 모듈 top-level import를 소비한다(호출시점 조회 아님).
    import quant_core.blocks.ops_advanced as adv
    monkeypatch.setattr(adv, "get_symbol_group",
                        lambda s, g="Industry": _GROUPS.get(s, "Other"))


def _ds(n=300):
    """4종목(반도체 2·자동차 2) + 종목별 상이한 foreign_net_buy(결정적 랜덤워크)."""
    out = {}
    for i, sym in enumerate(_GROUPS):
        rng = np.random.default_rng(100 + i)
        fnb = rng.normal((i - 1.5) * 1e9, 5e8, n)      # 그룹별 수준이 갈리는 순매수
        out[sym] = _ohlc(_noisy(n, vol=0.012, seed=i), {"foreign_net_buy": fnb})
    return out


def _ma(w):
    return {"op": "ts_mean", "params": {"window": w}, "inputs": {"signal": _C}}


def _bucket_label():
    return {"op": "bucket", "params": {"edges": [0]},
            "inputs": {"signal": {"op": "ts_delta", "params": {"window": 120},
                                  "inputs": {"signal": _C}}}}


def _run(ir_dict):
    return run_query(StrategyIR.model_validate(ir_dict), _ds())


# ── 신호분포(describe) — 실사건 재현 ─────────────────────────────────────────

def test_signal_dist_sector_label_executes():
    """실사건: 신호값 분포를 섹터(문자열 라벨)별로 — 크래시 없이 섹터 키로 분할돼야."""
    res = _run({"universe": {"kind": "list", "symbols": list(_GROUPS)},
                "signal": _C, "query": "describe",
                "study": {"target_node": _FNB, "label": _SECTOR}})
    assert res["success"], res.get("error")
    by = res["by_regime"]["by_label"]
    assert set(by) == {"반도체", "자동차"}, list(by)
    assert all(v.get("n", 0) > 0 for v in by.values())


def test_signal_dist_numeric_bucket_label_still_works():
    """가드 — 기존 숫자 레짐(bucket) 라벨 동작 무변경."""
    res = _run({"universe": {"kind": "list", "symbols": list(_GROUPS)},
                "signal": _C, "query": "describe",
                "study": {"target_node": _FNB, "label": _bucket_label()}})
    assert res["success"], res.get("error")
    assert res["by_regime"] and res["by_regime"]["by_label"]


# ── 이벤트 스터디(relate+event) — 같은 부류의 크래시 ─────────────────────────

def test_event_study_sector_label_splits_by_sector():
    """'섹터별 이벤트 반응' — attribute 라벨로 이벤트스터디가 크래시 없이 섹터 분할돼야."""
    event = {"op": "compare", "params": {"op": ">"}, "inputs": {"left": _C, "right": _ma(5)}}
    res = _run({"universe": {"kind": "list", "symbols": list(_GROUPS)},
                "signal": _C, "query": "relate",
                "study": {"event": event, "windows": [5], "event_basis": "close",
                          "label": _SECTOR}})
    assert res["success"], res.get("error")
    regimes = res["by_regime"]["5"]["by_regime"]
    populated = [k for k, v in regimes.items() if isinstance(v, dict) and v.get("n", 0) > 0]
    assert len(populated) >= 2, f"섹터 2개가 분리돼야: {list(regimes)}"
    assert set(regimes) <= {"반도체", "자동차"}, list(regimes)


# ── IC — 종목축 라벨은 조용한 오답이었다 → fail-loud ─────────────────────────

def test_ic_sector_label_fails_loud():
    """IC 국면분할은 시간축 라벨 전제(옛 코드는 첫 컬럼=전 시장 국면으로 조용히 오답).
    종목축(섹터) 라벨이 오면 명시 거부 + 행동 가능한 안내여야 한다."""
    res = _run({"universe": {"kind": "list", "symbols": list(_GROUPS)},
                "signal": _C, "query": "relate",
                "study": {"target_node": _FNB, "windows": [5], "label": _SECTOR}})
    assert res["success"] is False
    assert "섹터" in (res.get("error") or "") or "시간축" in (res.get("error") or "")


# ── target 자체가 범주형 — fail-loud ─────────────────────────────────────────

def test_describe_target_categorical_fails_loud():
    """분석 노드(target)가 범주형이면 숫자 분포가 무의미 — 크래시 대신 안내 거부."""
    res = _run({"universe": {"kind": "list", "symbols": list(_GROUPS)},
                "signal": _C, "query": "describe",
                "study": {"target_node": _SECTOR}})
    assert res["success"] is False
    assert "숫자" in (res.get("error") or ""), res.get("error")
