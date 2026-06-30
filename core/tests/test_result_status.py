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
    # 단일종목 IC는 엔진이 _empty(success=False)로 → infeasible(기존 동작 회귀 잠금)
    r = classify_status({"success": False, "error": "IC 분석은 종목이 2개 이상이어야 합니다."})
    assert r["status"] == "infeasible" and "2개 이상" in r["verdict"]
