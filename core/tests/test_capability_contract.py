"""Stage-1 규칙엔진 계약 — 형상별 §8 템플릿(결정적 부분)을 한 곳에서 단언.

챗봇이 설계상 목표한 분석 유형(verb×형상) 전체에 대해, LLM을 뺀 결정적 코어(IR→엔진→결과→
요약/엑셀)가 *정확히* 작동하는지 \$0·재현가능하게 진단한다. NL→IR 충실도·실데이터 정확성은
후속(Stage 2). 사전작성(골든 IR·고정 데이터)은 analysis_corpus(엑셀 구조 정본)를 재사용하고,
시각화 전용 형상(correlation·prescribe·breadth)만 여기서 자급 픽스처로 보강한다.

기존 하니스와 *겹치지 않는* 빈칸만 채운다(중복 회피):
  · analysis_corpus/test_analysis_corpus = strategy_from_spec 성공 + 엑셀 *구조*(시트·수식)
  · test_frontier               = correlation/prescribe/breadth 엔진 *구조* + summarize 텍스트
  · test_capability_coverage    = capability_spec ⊇ 엔진 enum (SSOT 표류 가드)
이 모듈 = 위가 안 보는 **유형 횡단 계약**:
  (A) 검증+실행 성공            (B) result_shape 스탬프 == 사전작성 기대(전 형상 일관)
  (C) summarize 무크래시·비폴백·형상 라벨 등장
  (D) 엑셀 게이트: 지원형상=빌드 성공 / 시각화전용=깔끔히 거부(ValueError) — '반쪽 파일' 금지
  + 사이드카 가산성(준실시간 맥락 주입이 분석 텍스트를 변형하지 않음 = 골든 무누출)

    cd core && python -m pytest tests/test_capability_contract.py -q
"""
from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # analysis_corpus 임포트

import analysis_corpus as C  # noqa: E402

from quant_core.ir_engine import (  # noqa: E402
    StrategyIR, build_strategy_excel, result_shape, strategy_from_spec, summarize_result,
)

_SIG = {"op": "data", "params": {"ref": "__SELF__.Close"}}


# ── 시각화 전용 형상(엑셀 비경유) 자급 픽스처 — frontier와 동일 결정적 데이터 ──────────
def _ds_corr() -> dict:
    idx = pd.date_range("2022-01-03", periods=120, freq="B")
    wave = np.cumsum(np.sin(np.linspace(0, 12, 120)) * 0.5)

    def _o(c):
        return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99,
                             "Close": c, "Volume": 1e6}, index=idx)
    return {"AAA": _o(100 + wave), "BBB": _o(100 + wave * 1.1 + 0.3), "CCC": _o(100 - wave * 0.9)}


def _ds_breadth() -> dict:
    idx = pd.date_range("2022-01-03", periods=80, freq="B")
    up, down = np.linspace(100, 130, 80), np.linspace(130, 100, 80)

    def _o(c, sec):
        df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99,
                           "Close": c, "Volume": 1e6}, index=idx)
        df["sector"] = sec
        return df
    return {"AAA": _o(up, "반도체"), "BBB": _o(up * 0.99 + 1, "반도체"),
            "CCC": _o(down, "자동차"), "DDD": _o(down * 1.01, "자동차"), "EEE": _o(down + 0.5, "화학")}


_CORR_IR = {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]},
            "signal": _SIG, "query": "relate", "study": {"relation_kind": "correlation"}}
_PRESCRIBE_IR = {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]},
                 "signal": _SIG, "query": "prescribe"}
_BREADTH_IR = {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
               "signal": _SIG, "query": "breadth"}


# ── 사전작성 = 유형별 (골든 IR, 데이터, 기대형상, 요약라벨, 엑셀지원여부) ────────────
FX = namedtuple("FX", "name ir ds shape token exportable")

# analysis_corpus.BASE_CASES 의 case명 → (기대형상, summarize 라벨). 전부 엑셀 지원.
_BASE = {
    "simulate":           ("simulate", "[백테스트]"),
    "sweep_parameter":    ("sweep", "분할분석"),
    "sweep_entity":       ("sweep", "분할분석"),
    "sweep_label":        ("sweep", "분할분석"),
    "period_split":       ("sweep", "분할분석"),
    "extremize":          ("extremize", "[최적화]"),
    "select":             ("select", "[스크리닝]"),
    "describe_single":    ("describe_single", "[종목분석]"),
    "describe_portfolio": ("describe_portfolio", "[포트진단]"),
    "describe_signal":    ("signal_dist", "[신호분포]"),
    "relate_ic":          ("relate_ic", "[팩터 IC]"),
    "relate_regression":  ("relate_regression", "[다중팩터 회귀"),
    "relate_event":       ("event_study", "[이벤트]"),
}

_by_name = {c["name"]: c for c in C.BASE_CASES}
REGISTRY: list[FX] = [
    FX(nm, _by_name[nm]["ir"], _by_name[nm]["ds"], shape, token, True)
    for nm, (shape, token) in _BASE.items()
]
REGISTRY += [   # 시각화 전용 = 엑셀 비지원(깔끔히 거부돼야 함)
    FX("correlation_matrix", _CORR_IR, _ds_corr, "correlation_matrix", "[상관행렬]", False),
    FX("prescribe", _PRESCRIBE_IR, _ds_corr, "prescribe", "[포트추천]", False),
    FX("breadth", _BREADTH_IR, _ds_breadth, "breadth", "[시장 breadth]", False),
]


def _excel_outcome(ir: dict, ds: dict, res: dict) -> tuple[str, str]:
    """엑셀 게이트 결과 분류: built(생성) / refused(ValueError로 깔끔히 거부) / crashed(딴 예외=결함)."""
    try:
        b = build_strategy_excel(StrategyIR.model_validate(ir), ds, res)
        return ("built", str(len(b) if b else 0))
    except ValueError as e:
        return ("refused", str(e))
    except Exception as e:   # noqa: BLE001 — 게이트가 ValueError 아닌 예외로 깨지는 '반쪽 파일' 탐지용
        return ("crashed", f"{type(e).__name__}: {e}")


@pytest.mark.parametrize("fx", REGISTRY, ids=[f.name for f in REGISTRY])
def test_rule_engine_contract(fx: FX) -> None:
    ds = fx.ds()
    res = strategy_from_spec(fx.ir, ds)
    # (A) 검증+실행
    assert res.get("success"), f"[{fx.name}] 실행 실패: {res.get('error')}"
    # (B) 형상 스탬프 == 사전작성 기대 (스탬프·폴백 양쪽 일관)
    assert result_shape(res) == fx.shape, f"[{fx.name}] 형상 {result_shape(res)} != 기대 {fx.shape}"
    stamped = res.get("shape")
    if isinstance(stamped, str) and stamped:
        assert stamped == fx.shape, f"[{fx.name}] 스탬프 {stamped} != 기대 {fx.shape}"
    # (C) summarize 무크래시·비폴백·형상 라벨
    s = summarize_result(res)
    assert s and not s.startswith("[실패]") and s != "[분석 완료]", f"[{fx.name}] summarize 폴백: {s[:60]}"
    assert fx.token in s, f"[{fx.name}] 요약에 '{fx.token}' 없음: {s[:90]}"
    # (D) 엑셀 게이트 — 지원=생성 / 시각화전용=깔끔히 거부
    kind, detail = _excel_outcome(fx.ir, ds, res)
    if fx.exportable:
        assert kind == "built" and int(detail) > 3000, f"[{fx.name}] 엑셀 미생성: {kind}/{detail}"
    else:
        assert kind == "refused", f"[{fx.name}] 엑셀이 깔끔히 거부되지 않음(반쪽 파일 위험): {kind} — {detail}"


def test_registry_covers_all_analytical_shapes() -> None:
    """사전작성 집합이 설계상 분석 형상 13종을 빠짐없이 덮는지(MECE 완전성)."""
    covered = {f.shape for f in REGISTRY}
    expected = {"simulate", "sweep", "extremize", "select", "describe_single", "describe_portfolio",
                "signal_dist", "relate_ic", "relate_regression", "event_study",
                "correlation_matrix", "prescribe", "breadth"}
    assert expected <= covered, f"미덮 형상: {expected - covered}"


def test_sidecar_context_is_additive_only() -> None:
    """P4 사이드카: result['context'] 주입이 분석 텍스트를 변형하지 않고 '준실시간' 블록만 가산(골든 무누출)."""
    case = _by_name["describe_single"]
    res = strategy_from_spec(case["ir"], case["ds"]())
    base = summarize_result(res)
    res_ctx = {**res, "context": {"quotes": {"AAA": {"close": 12345, "chg": -1.2}},
                                  "news": [{"title": "테스트 헤드라인"}]}}
    enriched = summarize_result(res_ctx)
    assert enriched.startswith(base), "사이드카가 분석 텍스트를 변형 — 골든 무누출 위반"
    assert "준실시간" in enriched and "테스트 헤드라인" in enriched
