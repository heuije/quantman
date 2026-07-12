"""이벤트 스터디 윈도우 값 도메인 — 조용한 오답 부류 봉쇄 (prod conv#50) + WS1 전조 승격.

부류(원형): 검증기가 값 도메인을 안 봐서 음수 창이 통과 → 시리즈 초반 이벤트가 음수 인덱스
wrap으로 **미래 구간을 '전조'로 집계**하던 조용한 오답. P0가 '정직 거부'로 막았고,
WS1(IR 대수 캠페인)이 음수 창을 **1급 지원**(창 시작점 anchor 누적수익·wrap 금지·조기 이벤트
탈락 회계)으로 승격했다. 잔여 거부는 intraday 기준 음수(분봉 필요)와 close/excess의 0뿐.
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

def test_validator_accepts_negative_windows_close_basis():
    """WS1 — 음수 창(전조)은 close/excess 기준에서 1급 지원(더 이상 거부 아님)."""
    assert not _event_errors(_ir([-240, -120, 20]))


def test_validator_rejects_negative_windows_intraday():
    errs = _event_errors(_ir([-240, -120], basis="intraday"))
    assert errs, "intraday 기준 전조는 분봉이 필요해 거부돼야 한다"
    assert any("전조" in e.message or "이전" in e.message for e in errs)   # 정직 한계+대안 안내


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
    # 시리즈 초반 이벤트(p+w<0): wrap 금지 — 가드 없으면 ca[11:-229] wrap으로 +760일
    # 미래 수익률이 '전조'가 됐다(실측 재현값). WS1 이후에도 조기 이벤트는 반드시 탈락.
    assert _event_paths(ca, oa, 10, -240, "close", None) is None
    # 유효한 전조 창(p+w>=0)은 창 시작점 anchor 누적수익을 반환(WS1 승격).
    got = _event_paths(ca, oa, 500, -240, "close", None)
    assert got is not None and got[0] > 0                      # 상승 시리즈의 전조 구간 수익
    assert abs(got[0] - (ca[500] / ca[260] - 1.0)) < 1e-12     # anchor=p+w 정확 산술
    assert _event_paths(ca, oa, 500, 0, "close", None) is None  # close의 0은 여전히 무의미
    assert _event_paths(ca, oa, 10, -240, "intraday", None) is None   # intraday 전조 미지원
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
