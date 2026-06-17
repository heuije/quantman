"""전략 연구소 챗봇 도구 테스트 — Task 3/4/5.

모든 테스트는 HERMETIC: 실데이터·엔진 실행 없이 monkeypatch로 격리.
conftest가 이미 sys.path를 in-repo core·server로 설정한다.
"""
# ── Task 3: tool schemas + assemble_ir ──────────────────────────────────────
from quant_core.ir_engine import StrategyIR
from app.chat.tools import assemble_ir, TOOL_SCHEMAS


def test_tool_schemas_present():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"screen", "simulate", "save_strategy", "describe", "inspect"}


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


# ── P2: save_strategy 도구 ───────────────────────────────────────────────────
from sqlmodel import Session, SQLModel, create_engine
from app.models import User, Strategy

# create_strategy(draft) 검증을 통과하는 유효 IR(test_strategies_ir._IR_DEF와 동형).
_IR_DEF = {
    "name": "연구소 모멘텀",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": {"op": "compare", "params": {"op": ">"},
               "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                          "right": {"op": "ts_mean", "params": {"window": 20},
                                    "inputs": {"signal": {"op": "data",
                                                          "params": {"ref": "__SELF__.Close"}}}}}},
    "position": {"direction": "long", "entry": {"mode": "on_signal"}},
    "simulation": {"initial_capital": 5_000_000},
}


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


def test_save_strategy_in_tool_schemas():
    assert any(t["name"] == "save_strategy" for t in TOOL_SCHEMAS)


def test_save_strategy_tool_creates_draft():
    with Session(_db()) as s:
        u = User(email="x@x.com"); s.add(u); s.commit(); s.refresh(u)
        res = chat_tools.save_strategy_tool(s, u.id, {"name": "내 전략", "ir": _IR_DEF})
        assert res["success"] is True and res["strategy_id"]
        row = s.get(Strategy, res["strategy_id"])
        assert row.run_mode == "draft" and row.engine == "ir"   # 저장-only 스코프=draft
        assert row.name == "내 전략" and row.user_id == u.id      # name 인자가 IR.name으로 주입


def test_save_strategy_tool_invalid_ir_returns_error():
    with Session(_db()) as s:
        u = User(email="y@y.com"); s.add(u); s.commit(); s.refresh(u)
        res = chat_tools.save_strategy_tool(s, u.id, {"name": "깨진", "ir": {"signal": "not-a-node"}})
        assert res["success"] is False and "error" in res       # 예외 대신 모델 피드백용 error


def test_compact_save_strategy():
    out = compact_summary("save_strategy",
                          {"success": True, "strategy_id": 7, "name": "내 전략", "run_mode": "draft"})
    assert "내 전략" in out and "7" in out


# ── P3: describe(단일 360) + inspect(원시 시계열) 도구 ────────────────────────
import pandas as pd


def test_describe_inspect_in_tool_schemas():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert {"describe", "inspect"} <= names


def test_assemble_describe_single():
    ir = assemble_ir("describe", {"symbol": "005930"})
    s = StrategyIR.model_validate(ir)                       # 유효한 describe IR이어야
    assert s.query == "describe"
    assert s.universe.kind == "single" and s.universe.symbols == ["005930"]


def test_run_inspect_returns_series(monkeypatch):
    # inspect는 엔진 집계가 아니라 데이터셋 직접 retrieval — load_dataset_for를 가짜 DF로 격리.
    idx = pd.to_datetime(["2026-06-15", "2026-06-16", "2026-06-17"])
    df = pd.DataFrame({"consensus_target": [80000.0, 81000.0, 82000.0],
                       "Close": [70000.0, 71000.0, 72000.0]}, index=idx)
    monkeypatch.setattr(chat_tools.qc, "load_dataset_for", lambda syms: {"005930": df})
    out = chat_tools.run_tool("inspect", {"symbol": "005930",
                                          "columns": ["consensus_target"], "window": 2})
    assert out["success"] is True and out["query"] == "inspect"
    assert out["symbol"] == "005930" and out["columns"] == ["consensus_target"]
    assert out["dates"] == ["2026-06-16", "2026-06-17"]                 # tail(window=2)
    assert out["series"]["consensus_target"] == [81000.0, 82000.0]


def test_run_inspect_missing_column_returns_error(monkeypatch):
    df = pd.DataFrame({"Close": [1.0, 2.0]},
                      index=pd.to_datetime(["2026-06-16", "2026-06-17"]))
    monkeypatch.setattr(chat_tools.qc, "load_dataset_for", lambda syms: {"005930": df})
    out = chat_tools.run_tool("inspect", {"symbol": "005930", "columns": ["consensus_target"]})
    assert out["success"] is False and "error" in out


def test_compact_describe():
    out = compact_summary("describe",
                          {"success": True, "symbol": "005930", "sector": "반도체",
                           "price": {"last": 72000}, "fundamentals": {"pb_ratio": 1.2,
                                                                      "trailing_pe": 10.0}})
    assert "005930" in out


def test_compact_inspect():
    out = compact_summary("inspect",
                          {"success": True, "symbol": "005930", "columns": ["consensus_target"],
                           "dates": ["2026-06-16", "2026-06-17"],
                           "series": {"consensus_target": [81000.0, 82000.0]}})
    assert "005930" in out and "82000" in out                  # 최근값 표면화
