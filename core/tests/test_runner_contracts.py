"""러너 능력 계약(contracts.py) — 결정 SSOT·경계 가드·광고 파생.

핵심 불변식: resolve_runner(결정의 순수함수 추출)가 실제 디스패치(_dispatch_query + 하위 분기)와
**분기 순서까지** 일치한다. 어긋나면 검증기가 엉뚱한 러너의 계약을 검사하는 신형 드리프트가
생기므로, 전 러너를 모의 계측해 예측=실행을 전수 대조한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine import run as run_mod
from quant_core.ir_engine.contracts import REGISTRY, contract_issues, resolve_runner
from quant_core.ir_engine.run import run_query
from quant_core.ir_engine.spec import StrategyIR

_COND = {"op": "compare", "params": {"op": ">"},
         "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "const", "params": {"value": 0}}}}
_SCORE = {"op": "data", "params": {"ref": "__SELF__.Close"}}
_GRID = [{"path": "position.exit.hold_days", "values": [1, 2]}]

# (IR 조각, 기대 러너 키, 실제로 호출돼야 하는 run.py 함수명)
_MATRIX = [
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "select",
      "select": {"top_n": 3}}, "select.rank", "run_select"),
    ({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _SCORE, "query": "select",
      "select": {"mode": "compare"}}, "select.compare", "run_select"),
    ({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _SCORE,
      "query": "prescribe"}, "prescribe", "run_prescribe"),
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "breadth"}, "breadth", "run_breadth"),
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "rotation"}, "rotation", "run_rotation"),
    ({"universe": {"kind": "single", "symbols": ["A"]}, "signal": _SCORE,
      "query": "describe"}, "describe.single", "run_describe_report"),
    ({"universe": {"kind": "portfolio", "symbols": ["A", "B"]}, "signal": _SCORE,
      "query": "describe"}, "describe.portfolio", "run_portfolio_diagnosis"),
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "describe",
      "study": {"target_node": _SCORE}}, "describe.signal_dist", "_run_signal_study"),
    ({"universe": {"kind": "all"}, "signal": _COND, "query": "relate",
      "study": {"event": _COND, "windows": [5, 20]}}, "relate.event_study", "_run_event_study"),
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "relate",
      "study": {"target_node": _SCORE}}, "relate.ic", "_run_ic_study"),
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "relate",
      "study": {"relation_kind": "regression", "factors": [_SCORE]}},
     "relate.regression", "_run_regression_study"),
    ({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _SCORE, "query": "relate",
      "study": {"relation_kind": "correlation"}}, "relate.correlation", "_run_correlation_study"),
    ({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _COND, "query": "relate",
      "study": {"axis": "entity", "assets": ["A", "B"]}},
     "relate.entity_cohort", "_run_entity_cohort"),
    ({"universe": {"kind": "single", "symbols": ["A"]}, "signal": _COND, "query": "simulate",
      "study": {"axis": "time_fold"}}, "simulate.period_split", "run_period_split"),
    ({"universe": {"kind": "single", "symbols": ["A"]}, "signal": _COND,
      "query": "simulate"}, "simulate.event", "run_strategy_ir"),
    ({"universe": {"kind": "all"}, "signal": _SCORE, "query": "simulate",
      "position": {"entry": {"mode": "scheduled", "top_n": 3}}},
     "simulate.scheduled", "run_strategy_ir"),
    ({"universe": {"kind": "single", "symbols": ["A"]}, "signal": _COND, "query": "simulate",
      "study": {"axis": "parameter", "param_grid": _GRID, "reduction": "extremize"}},
     "simulate.extremize", "run_extremize"),
    ({"universe": {"kind": "single", "symbols": ["A"]}, "signal": _COND, "query": "simulate",
      "study": {"axis": "parameter", "param_grid": _GRID}},
     "simulate.sweep.parameter", "run_sweep"),
    ({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _COND, "query": "simulate",
      "study": {"axis": "entity", "assets": ["A", "B"]}},
     "simulate.sweep.entity", "run_sweep"),
    ({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _COND, "query": "simulate",
      "study": {"axis": "label", "label": {"op": "calendar", "params": {"unit": "weekday"}}}},
     "simulate.sweep.label", "run_sweep"),
]

_RUNNER_FNS = ["run_select", "run_prescribe", "run_breadth", "run_rotation",
               "run_describe_report", "run_portfolio_diagnosis", "_run_signal_study",
               "_run_entity_cohort", "_run_event_study", "_run_regression_study",
               "_run_correlation_study", "_run_ic_study", "run_period_split",
               "run_strategy_ir", "run_extremize", "run_sweep"]


def test_resolve_runner_matches_actual_dispatch(monkeypatch):
    """예측(resolve_runner) = 실행(디스패치가 실제 부른 함수) — 전 러너 전수 대조."""
    called: list[str] = []
    for name in _RUNNER_FNS:
        def _rec(strategy, dataset, _n=name):
            called.append(_n)
            return {"success": False, "error": "stub"}
        monkeypatch.setattr(run_mod, name, _rec)
    for frag, key, expected_fn in _MATRIX:
        ir = StrategyIR.model_validate(frag)
        assert resolve_runner(ir) == key, f"resolve 불일치: {frag.get('query')} → {key}"
        called.clear()
        run_query(ir, {})
        assert called == [expected_fn], \
            f"디스패치 드리프트: {key} 예측인데 실제 호출={called}(기대 {expected_fn})"


# ── 경계 가드 — 검증 우회(저장 IR·직접 호출)도 조용한 오답 대신 정직 거부 ────────

def test_run_query_boundary_guard_refuses_contract_violation():
    # WS1로 음수 창(전조)은 1급 지원이 됐다 — 여전히 계약 위반인 케이스로 가드를 검증:
    # intraday 기준의 음수 창(장중 전조 — 분봉 필요·미지원).
    ir = StrategyIR.model_validate({
        "universe": {"kind": "all"}, "signal": _COND, "query": "relate",
        "study": {"event": _COND, "windows": [-240, -120], "event_basis": "intraday"},
    })
    res = run_query(ir, {})            # validate_strategy를 거치지 않는 직접 실행
    assert res["success"] is False
    assert res["status"] == "unsupported"
    assert "전조" in res["error"]       # 계약의 정직 한계 안내가 그대로 사유가 된다


def test_run_query_stamps_runner_key(monkeypatch):
    monkeypatch.setattr(run_mod, "_run_event_study",
                        lambda s, d: {"success": True, "n_events": 0, "windows": [],
                                      "overall": {}, "shape": "event_study"})
    ir = StrategyIR.model_validate({
        "universe": {"kind": "all"}, "signal": _COND, "query": "relate",
        "study": {"event": _COND, "windows": [5]},
    })
    res = run_query(ir, {})
    assert res["runner"] == "relate.event_study"


# ── 광고 파생 — not_supported가 capabilities에 자동 노출 ─────────────────────

def test_capability_spec_exposes_runner_limits():
    from quant_core.ir_engine.capabilities import capability_spec
    limits = capability_spec()["지원_한계"]
    assert "이벤트 스터디" in limits and "전조" in limits


def test_registry_contracts_advertise_shape_and_limits():
    # 계약 리뷰 체크리스트의 기계화 — 등록된 계약은 광고 3요소(does/use_for/label)와
    # not_supported에 대안 문구를 갖춰야 한다(한계만 말하고 대안이 없으면 수리 루프가 헤맨다).
    for c in REGISTRY.values():
        assert c.does and c.use_for and c.label and c.shape
        for item in c.not_supported:
            assert "expressible" in item or "대안" in item or "가능" in item, \
                f"{c.key}: not_supported에 대안 안내가 없다"


# ── 검증기 흡수 — 기존 규칙 ID 호환(S-event 등) 유지 확인 ─────────────────────

def _errs(frag, refs=("Close",)):
    from quant_core.ir_engine.spec import validate_strategy
    ir = StrategyIR.model_validate(frag)
    return [i for i in validate_strategy(ir, valid_refs=set(refs)) if i.is_error]


def test_validator_contract_absorption_keeps_rule_ids():
    errs = _errs({"universe": {"kind": "all"}, "signal": _COND, "query": "relate",
                  "study": {"event": _COND, "windows": []}})
    assert any(i.rule == "S-event" and "윈도우가 필요" in i.message for i in errs)


def test_absorbed_select_rank_domains():
    # xor(top_n·top_pct 동시/전무)·값 범위 — 옛 S-SEL 손코딩의 계약 흡수(ID 호환).
    both = _errs({"universe": {"kind": "all"}, "signal": _SCORE, "query": "select",
                  "select": {"top_n": 3, "top_pct": 10}})
    assert any(i.rule == "S-SEL" and "정확히 하나" in i.message for i in both)
    zero = _errs({"universe": {"kind": "all"}, "signal": _SCORE, "query": "select",
                  "select": {"top_n": 0}})
    assert any(i.rule == "S-SEL" and "top_n" in i.message for i in zero)
    pct = _errs({"universe": {"kind": "all"}, "signal": _SCORE, "query": "select",
                 "select": {"top_pct": 150}})
    assert any(i.rule == "S-SEL" and "top_pct" in i.message for i in pct)


def test_absorbed_min_symbols_rules():
    # 옛 kind=single 거부보다 강한 명시 리스트 2+ 검사 — 규칙 ID는 호환 유지.
    for query, rule in (("prescribe", "S-PRESCRIBE"), ("breadth", "S-BREADTH"),
                        ("rotation", "S-ROTATION")):
        errs = _errs({"universe": {"kind": "single", "symbols": ["A"]},
                      "signal": _SCORE, "query": query})
        assert any(i.rule == rule for i in errs), f"{query} 종목수 규칙 소실"
    corr = _errs({"universe": {"kind": "list", "symbols": ["A"]}, "signal": _SCORE,
                  "query": "relate", "study": {"relation_kind": "correlation"}})
    assert any(i.rule == "S-CORR" for i in corr)


def test_absorbed_regression_and_ic_domains():
    reg = _errs({"universe": {"kind": "single", "symbols": ["A"]}, "signal": _SCORE,
                 "query": "relate", "study": {"relation_kind": "regression", "factors": []}})
    rules = {i.rule for i in reg}
    assert "S-REG" in rules                          # factors 1+ · 종목 2+
    ic_neg = _errs({"universe": {"kind": "list", "symbols": ["A", "B"]}, "signal": _SCORE,
                    "query": "relate", "study": {"target_node": _SCORE, "windows": [-5]}},
                   refs=("Close",))
    assert any("음수" in i.message or "1 이상" in i.message for i in ic_neg)  # 신규 승격 도메인


def test_promoted_silent_failure_domains():
    # 옛 조용한 실패의 fail-loud 승격 — vol_window(pandas 예외)·every_n_days(무거래).
    vw = _errs({"universe": {"kind": "all"}, "signal": _SCORE, "query": "simulate",
                "position": {"entry": {"mode": "scheduled", "top_n": 3},
                             "sizing": {"mode": "vol_inverse", "vol_window": 0}}})
    assert any("vol_window" in i.message for i in vw)
    nd = _errs({"universe": {"kind": "all"}, "signal": _SCORE, "query": "simulate",
                "position": {"entry": {"mode": "scheduled", "top_n": 3,
                                       "rebalance": "every_n_days"}}})
    assert any("every_n_days" in i.message and "무거래" in i.message for i in nd)


def test_registry_covers_all_dispatchable_runners():
    # 전수 등록 계약 — 매트릭스의 모든 러너 키가 레지스트리에 존재(광고·shape 단일 원천).
    for _, key, _fn in _MATRIX:
        assert key in REGISTRY, f"레지스트리 누락: {key}"


def test_registry_shapes_are_known_summarize_vocabulary():
    # 계약 shape는 summarize/웹이 아는 형상 어휘여야 한다 — 오타·미지 형상은 '[분석 완료]'
    # 폴백(조용한 렌더 저하)이 되므로 어휘 밖 선언을 차단.
    known = {"select", "prescribe", "breadth", "describe_single", "describe_portfolio",
             "relate_ic", "relate_regression", "correlation_matrix", "cohort",
             "event_study", "signal_dist", "simulate", "sweep", "extremize", "heatmap"}
    for c in REGISTRY.values():
        assert c.shape in known, f"{c.key}: 미지 shape {c.shape!r}"


# ── 탈락 회계(자기서술 v2) — 이벤트 스터디 파일럿 ─────────────────────────────

def _tiny_event_ds(n=60):
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(100.0, index=idx)
    close.iloc[10] = 120.0                       # 하루 급등 → 이벤트 1건
    return {"AAA": pd.DataFrame({"Open": close, "High": close * 1.01,
                                 "Low": close * 0.99, "Close": close,
                                 "Volume": 1e6}, index=idx)}


_JUMP = {"op": "compare", "params": {"op": ">"},
         "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "const", "params": {"value": 110}}}}


def test_event_study_accounting_and_low_coverage_status():
    ds = _tiny_event_ds()
    ir = StrategyIR.model_validate({
        "universe": {"kind": "list", "symbols": ["AAA"]}, "signal": _JUMP,
        "query": "relate", "study": {"event": _JUMP, "windows": [5, 500]},
    })
    res = run_query(ir, ds)
    assert res["success"], res.get("error")
    acct = res["accounting"]
    assert acct["events_total"] == 1
    assert acct["evaluated_by_window"] == {"5": 1, "500": 0}   # 500일 창=데이터 밖 → 조용한 탈락이 회계로
    assert acct["universe_events"] == {"KR": 0, "US": 1}
    # status가 회계를 소비 — 전멸 창은 empty로 판정(성공 가장 금지)
    assert res["status"] == "empty" and "+500" in res["verdict"]
