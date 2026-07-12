"""이벤트 스터디 윈도우 값 도메인 — 음수/0 창 조용한 오답 부류 봉쇄 (prod conv#50).

부류: 검증기(S-event)가 windows의 *비어있음*만 보고 값 도메인을 안 봐서 음수 창이 통과 →
_event_paths가 대부분 이벤트를 빈 슬라이스로 조용히 탈락시키고, 시리즈 초반(p<|w|) 이벤트는
파이썬 음수 인덱스 wrap으로 **미래 구간 수익률을 '전조'로 집계** → 자기서술은 "발생 후 forward"
고정이라 유저에겐 정상처럼 보였다. 3겹(검증기·엔진 가드·자기서술)을 함께 잠근다.
"""
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.run import _event_paths
from quant_core.ir_engine.spec import StrategyIR, validate_strategy
from quant_core.ir_engine.summarize import summarize_result

_EVENT = {"op": "compare", "params": {"op": ">="},
          "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.pct_change_20d"}},
                     "right": {"op": "const", "params": {"value": 0.1}}}}
_VALID_REFS = {"pct_change_20d", "Close"}


def _ir(windows, basis="close"):
    return StrategyIR.model_validate({
        "universe": {"kind": "all"},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "query": "relate",
        "study": {"event": _EVENT, "windows": windows, "event_basis": basis},
    })


def _event_errors(ir):
    return [i for i in validate_strategy(ir, valid_refs=_VALID_REFS)
            if i.rule == "S-event" and i.is_error]


# ── 검증기 — 값 도메인 ────────────────────────────────────────────────────────

def test_validator_rejects_negative_windows():
    errs = _event_errors(_ir([-240, -120, 0]))
    assert errs, "음수/0 윈도우가 검증을 통과하면 conv#50 조용한 오답이 재발한다"
    assert any("전조" in e.message and "미지원" in e.message for e in errs)   # 정직 한계 안내


def test_validator_rejects_zero_window_close_basis():
    assert _event_errors(_ir([0, 5]))         # close 기준 0은 빈 슬라이스(조용 탈락)라 거부


def test_validator_accepts_positive_windows():
    assert not _event_errors(_ir([5, 20, 120]))


def test_validator_allows_zero_for_intraday_basis():
    # intraday k=0 = 당일 시가→종가(정의된 유효 경로) — 과잉 강성 방지.
    assert not _event_errors(_ir([0, 5], basis="intraday"))
    assert _event_errors(_ir([-1, 5], basis="intraday"))


# ── 엔진 경계 가드 — 검증 우회(저장 IR·직접 호출) 방어 ─────────────────────────

def test_event_paths_negative_window_never_wraps():
    ca = np.linspace(100, 200, 1000)
    oa = ca.copy()
    # 시리즈 초반 이벤트: 가드 없으면 ca[11:-229] wrap으로 +760일 미래 수익률이 나왔다(실측 재현값).
    assert _event_paths(ca, oa, 10, -240, "close", None) is None
    assert _event_paths(ca, oa, 500, -240, "close", None) is None
    assert _event_paths(ca, oa, 500, 0, "close", None) is None
    # 유효 도메인은 보존
    assert _event_paths(ca, oa, 500, 5, "close", None) is not None
    assert _event_paths(ca, oa, 500, 0, "intraday", None) is not None


# ── 자기서술 — 창별 표본 커버리지 표면화 ──────────────────────────────────────

def test_event_summary_surfaces_window_coverage():
    result = {"shape": "event_study", "n_events": 100, "basis": "close",
              "windows": ["120"],
              "overall": {"120": {"n": 3, "mean": 1.0, "p_value": 0.5,
                                  "prob_positive": 60.0, "mean_mae": -2.0, "mean_mfe": 3.0}}}
    text = summarize_result(result)
    assert "표본 3/100건" in text            # 조용한 탈락이 문장으로 드러난다
    assert "전조 아님" in text               # 계산 방향의 명시


# ── 광고 키 소비자 계약 — explain의 _does 조회 키는 반드시 실존해야 한다 ─────────
# (실측: sweep_axis·sweep_target·period_split 키가 개명·부재로 무성 빈 문자열 폴백이었다.)

def test_explain_does_keys_exist_in_capability_spec():
    from quant_core.ir_engine.capabilities import capability_spec
    src = (Path(__file__).resolve().parent.parent
           / "quant_core" / "ir_engine" / "explain.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'_does\(\s*"([a-z_]+)"', src))
    caps = capability_spec()
    missing = sorted(k for k in keys if k not in caps)
    assert not missing, f"explain이 존재하지 않는 capabilities 키를 조회(무성 빈 문자열): {missing}"


def test_explain_analysis_axis_labels_not_raw_enums():
    # entity 축 sweep의 분석 버킷 — raw enum("asset")이 아니라 한글 라벨이 노출돼야 한다.
    from quant_core.ir_engine.explain import explain_ir
    ir = StrategyIR.model_validate({
        "universe": {"kind": "list", "symbols": ["AAA", "BBB"]},
        "signal": {"op": "compare", "params": {"op": ">"},
                   "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                              "right": {"op": "const", "params": {"value": 0}}}},
        "query": "simulate",
        "study": {"axis": "entity", "assets": ["AAA", "BBB"]},
    })
    out = explain_ir(ir, [])
    bucket = next(b for b in out["buckets"] if b["key"] == "analysis")
    axis_item = next(i for i in bucket["items"] if i["label"] == "분석 축")
    assert axis_item["value"] != "asset" and "종목별" in axis_item["value"]
