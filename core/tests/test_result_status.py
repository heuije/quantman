"""결과 품질 계약(chat-reliability §3) — classify_status 회귀.

진단 뿌리 R1: 엔진이 빈/퇴화 결과를 success=True로 흘려보내 모델·UI가 '분석 완료'로 표시.
classify_status가 그 결과들을 status로 구분하는지 단언(순수 함수·픽스처).
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine.result_status import classify_status  # noqa: E402


def _sim(shape="simulate", **metrics):
    return {"success": True, "shape": shape, "equity": [1, 1.1],
            "metrics": {"n_trades": 50, "total_return": 20.0, "mdd": -15.0, **metrics}}


def test_simulate_ok():
    r = classify_status(_sim())
    assert r["status"] == "ok" and r["diagnostics"]["n_trades"] == 50


def test_simulate_empty_zero_trades():
    r = classify_status(_sim(n_trades=0, total_return=0.0, mdd=0.0))
    assert r["status"] == "empty" and "거래 0건" in r["verdict"]


def test_simulate_degenerate_impossible_loss():
    # 진단 conv#25: 레버리지 충돌로 n_trades=1·total_return=-154%·MDD-154% garbage.
    r = classify_status(_sim(n_trades=1, total_return=-154.18, mdd=-154.18, win_rate=100.0))
    assert r["status"] == "degenerate" and "한계" in r["verdict"]


def test_simulate_data_insufficient_low_coverage():
    r = classify_status({**_sim(), "warnings": [
        {"code": "data_gap", "message": "코스피200선물: 2024~2026 데이터 밀도 부족(300/498일 — 결손)"}]})
    assert r["status"] == "data_insufficient" and r["diagnostics"]["coverage"] < 0.8


def test_simulate_mild_gap_stays_ok():
    # 80~95% 밴드는 ok(경고는 summarize가 warnings로 별도 표면화). 404/498≈0.81.
    r = classify_status({**_sim(), "warnings": [
        {"code": "data_gap", "message": "선물: 데이터 밀도(404/498일 — 결손)"}]})
    assert r["status"] == "ok" and 0.8 <= r["diagnostics"]["coverage"] < 0.95


def test_simulate_single_trade_low_confidence():
    r = classify_status(_sim(n_trades=1, total_return=3.0, mdd=-2.0))
    assert r["status"] == "ok" and "표본" in r["verdict"]   # 저신뢰 주의(status는 ok)


def test_select_empty_zero_eligible():
    # 진단 conv#21: universe 16,000인데 eligible=0(모멘텀 단일팩터 섹터스크린).
    r = classify_status({"success": True, "shape": "select", "query": "select",
                         "universe_size": 16019, "eligible_size": 0, "results": []})
    assert r["status"] == "empty" and r["diagnostics"]["eligible_size"] == 0


def test_select_ok():
    r = classify_status({"success": True, "shape": "select", "query": "select",
                         "universe_size": 7583, "eligible_size": 34,
                         "results": [{"symbol": f"{i}"} for i in range(10)]})
    assert r["status"] == "ok"


def test_event_study_empty():
    # 진단 conv#25: ±50% 조건이 너무 가혹 → n_events=0.
    r = classify_status({"success": True, "shape": "event_study", "axis": "time", "n_events": 0})
    assert r["status"] == "empty" and "이벤트 0건" in r["verdict"]


def test_event_study_low_power_stays_ok():
    r = classify_status({"success": True, "shape": "event_study", "axis": "time", "n_events": 3})
    assert r["status"] == "ok" and "표본" in r["verdict"]


def test_event_study_ok():
    r = classify_status({"success": True, "shape": "event_study", "axis": "time", "n_events": 100})
    assert r["status"] == "ok" and not r["verdict"]


# ── P3-a (E 뿌리): 백테스트 함정 — 이벤트 연도 편중 자동 verdict ───────────────
# 프로덕션(floo.korea)서 봇이 '특정 해 집중'을 수동으로 눈치채던 상승장-전용 과대추정 함정을
# composition.by_year(엔진이 이미 산출)를 소비해 구조가 자동 경고한다. status는 ok(caveat).
def _event(n_events, by_year):
    return {"success": True, "shape": "event_study", "axis": "time",
            "n_events": n_events, "composition": {"by_year": by_year}}


def test_event_study_year_concentration_warns():
    # 이벤트 20건 중 15건(75%)이 2026년에 집중 → 레짐 편중 경고(그 국면이 통계 지배).
    r = classify_status(_event(20, {"2023": 2, "2024": 3, "2026": 15}))
    assert r["status"] == "ok"                                   # 실패 아님 — 정직한 caveat
    assert "편중" in r["verdict"] and "75%" in r["verdict"] and "2026" in r["verdict"]
    assert r["diagnostics"]["top_year_share"] == 0.75            # 정량 근거(사용자 요청 ①)


def test_event_study_single_year_all_events_warns():
    # 전 이벤트가 단일 연도 → share 100% → 편중(교차연도 증거 0).
    r = classify_status(_event(30, {"2026": 30}))
    assert r["status"] == "ok" and "100%" in r["verdict"]


def test_event_study_balanced_years_no_warning():
    # 여러 해에 고르게 분산(최다 40% < 60%) → 편중 경고 없음(오탐 방지).
    r = classify_status(_event(50, {"2022": 20, "2023": 15, "2024": 15}))
    assert r["status"] == "ok" and not r["verdict"]
    assert r["diagnostics"]["top_year_share"] == 0.4


def test_event_study_low_sample_suppresses_concentration():
    # 소표본(<5)에선 연도 편중이 노이즈 → 저신뢰 caveat만, 편중 경고로 겹치지 않는다.
    r = classify_status(_event(3, {"2026": 3}))
    assert r["status"] == "ok" and "표본" in r["verdict"] and "편중" not in r["verdict"]


def test_event_study_no_composition_stays_clean():
    # composition 없으면(구형 결과) 편중 판정 불가 → 기존 동작 보존(verdict 없음).
    r = classify_status({"success": True, "shape": "event_study", "axis": "time", "n_events": 100})
    assert r["status"] == "ok" and not r["verdict"] and "top_year_share" not in r["diagnostics"]


def test_sweep_all_inactive_empty():
    r = classify_status({"success": True, "shape": "sweep", "axis": "period_split",
                         "buckets": {"2010": {"n": 0}, "2011": {"error": "무거래"}}})
    assert r["status"] == "empty"


def test_sweep_ok():
    r = classify_status({"success": True, "shape": "sweep", "axis": "period_split",
                         "buckets": {"2020": {"n": 50, "cum_return": 10.0}}})
    assert r["status"] == "ok"


def test_describe_ok():
    r = classify_status({"success": True, "shape": "describe_single", "report": "single"})
    assert r["status"] == "ok"


def test_failure_is_infeasible():
    r = classify_status({"success": False, "error": "이 전략은 현재 IR 구조로 표현할 수 없습니다."})
    assert r["status"] == "infeasible" and "표현할 수 없" in r["verdict"]


def test_non_dict_infeasible():
    assert classify_status(None)["status"] == "infeasible"


# ── T4 (Wave 2): 관계분석(IC·회귀·상관) 0표본 → 정직 status (빈 차트 차단) ───────
def test_ic_zero_sample_is_data_insufficient():
    """공통구간 부족 IC는 횡단 표본이 0 → ok로 흘려보내지 않고 data_insufficient로 정직 고지
    (증상 #9b — 좋은 가설이 빈 차트로 죽던 C측 뿌리). 단일종목은 엔진이 success=False로 별도 차단."""
    r = classify_status({"success": True, "shape": "relate_ic", "axis": "relation",
                         "relation": "ic", "windows": ["21"],
                         "by_window": {"21": {"overall": {"n": 0, "mean": None}}}})
    assert r["status"] == "data_insufficient" and "이벤트스터디" in r["verdict"]


def test_ic_with_samples_is_ok():
    r = classify_status({"success": True, "shape": "relate_ic", "axis": "relation",
                         "relation": "ic", "windows": ["21"],
                         "by_window": {"21": {"overall": {"n": 240, "mean": 0.03}}}})
    assert r["status"] == "ok"


def test_ic_low_sample_ok_low_confidence():
    r = classify_status({"success": True, "shape": "relate_ic", "axis": "relation",
                         "relation": "ic", "windows": ["21"],
                         "by_window": {"21": {"overall": {"n": 3, "mean": 0.1}}}})
    assert r["status"] == "ok" and "신뢰도" in r["verdict"]


def test_correlation_zero_obs_data_insufficient():
    r = classify_status({"success": True, "shape": "correlation_matrix", "axis": "relation",
                         "relation": "correlation", "symbols": ["A", "B"], "n_obs": 0})
    assert r["status"] == "data_insufficient"


def test_regression_zero_periods_data_insufficient():
    r = classify_status({"success": True, "shape": "relate_regression", "axis": "relation",
                         "relation": "regression", "windows": ["21"],
                         "by_window": {"21": {"n_periods": 0, "factors": None}}})
    assert r["status"] == "data_insufficient"


def test_single_symbol_ic_infeasible_preserved():
    # 단일종목 IC는 엔진이 _empty(success=False)로 → infeasible(기존 동작 회귀 잠금).
    # M1(#9): 메시지가 막다른 제약이 아니라 올바른 대안(이벤트 스터디)을 안내해야 한다.
    r = classify_status({"success": False, "error": "IC 분석은 종목이 2개 이상이어야 합니다. "
                                                    "단일종목의 예측력은 이벤트 스터디(신호 발생 후 수익 분포)로 분석하세요."})
    assert r["status"] == "infeasible" and "2개 이상" in r["verdict"] and "이벤트" in r["verdict"]
