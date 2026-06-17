"""전략 연구소 챗봇 도구 테스트 — Task 3/4/5.

모든 테스트는 HERMETIC: 실데이터·엔진 실행 없이 monkeypatch로 격리.
conftest가 이미 sys.path를 in-repo core·server로 설정한다.
"""
# ── Task 3: tool schemas + assemble_ir ──────────────────────────────────────
from quant_core.ir_engine import StrategyIR
from app.chat.tools import assemble_ir, TOOL_SCHEMAS


def test_tool_schemas_present():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"screen", "simulate"}


def test_assemble_screen_makes_valid_select_ir():
    ir = assemble_ir("screen", {"symbols": ["AAA", "BBB"],
                                "score_ref": "__SELF__.pb_ratio",
                                "top_n": 3, "descending": False, "display": ["pb_ratio"]})
    s = StrategyIR.model_validate(ir)         # 유효해야 함(예외 없음)
    assert s.query == "select"
    assert s.select.top_n == 3 and s.select.descending is False
    assert s.universe.kind == "list" and s.universe.symbols == ["AAA", "BBB"]


def test_assemble_screen_no_symbols_uses_all():
    ir = assemble_ir("screen", {"score_ref": "momentum_12_1m", "top_n": 5})
    s = StrategyIR.model_validate(ir)
    assert s.universe.kind == "all"


def test_assemble_simulate_passes_full_ir():
    base = {"universe": {"kind": "single", "symbols": ["AAA"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "position": {"entry": {"mode": "always"}}}
    ir = assemble_ir("simulate", {"strategy": base})
    s = StrategyIR.model_validate(ir)
    assert s.query == "simulate" and s.study.axis == "none"


def test_assemble_unknown_tool_raises():
    import pytest
    with pytest.raises(ValueError):
        assemble_ir("nope", {})


# ── Task 4: run_tool dispatch ────────────────────────────────────────────────
from app.chat import tools as chat_tools


def test_run_tool_dispatches_to_engine(monkeypatch):
    captured = {}

    def fake_load(ir):
        captured["ir"] = ir
        return {"AAA": object()}            # 더미 데이터셋(엔진 호출 안 함)

    def fake_run(ir, dataset):
        captured["ran"] = (ir, dataset)
        return {"success": True, "query": "select", "results": []}

    monkeypatch.setattr(chat_tools, "_load_dataset", fake_load)
    monkeypatch.setattr(chat_tools, "strategy_from_spec", fake_run)

    out = chat_tools.run_tool("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 2})
    assert out["success"] is True
    assert captured["ir"]["query"] == "select"          # 조립된 IR이 전달됨
    assert captured["ran"][0]["query"] == "select"


def test_run_tool_bad_input_returns_error_not_raises(monkeypatch):
    # assemble_ir가 실패하면 예외 대신 error dict(루프가 모델에 피드백)
    out = chat_tools.run_tool("screen", {"top_n": 2})   # score_ref 누락 → KeyError 내부
    assert out["success"] is False and "error" in out


# ── Task 5: compact_summary ──────────────────────────────────────────────────
from app.chat.tools import compact_summary


def test_compact_screen():
    res = {"success": True, "as_of": "2026-06-17", "universe_size": 10, "eligible_size": 4,
           "results": [{"symbol": "AAA", "score": 0.82}, {"symbol": "BBB", "score": 0.79}]}
    out = compact_summary("screen", res)
    assert "AAA" in out and "0.82" in out and "2026-06-17" in out


def test_compact_simulate():
    res = {"success": True, "metrics": {"cagr": 0.123, "sharpe": 0.9, "mdd": -0.22,
                                        "cum_return": 1.4}}
    out = compact_summary("simulate", res)
    assert "cagr" in out and "0.123" in out


def test_compact_failure():
    out = compact_summary("simulate", {"success": False, "error": "전략 파싱 오류: x"})
    assert "실패" in out and "전략 파싱 오류" in out


def test_load_dataset_invalid_ir_returns_empty():
    # 파싱 불가 IR(필수 signal 없음) → {} 반환(엔진 검증경로로 위임), 예외 전파 안 함.
    assert chat_tools._load_dataset({}) == {}
    assert chat_tools._load_dataset({"signal": "not-a-node"}) == {}
