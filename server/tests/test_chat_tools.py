"""전략 연구소 챗봇 도구 테스트 — Task 3/4/5.

모든 테스트는 HERMETIC: 실데이터·엔진 실행 없이 monkeypatch로 격리.
conftest가 이미 sys.path를 in-repo core·server로 설정한다.
"""
# ── Task 3: tool schemas + assemble_ir ──────────────────────────────────────
from quant_core.ir_engine import StrategyIR
from app.chat.tools import assemble_ir, TOOL_SCHEMAS


def test_tool_schemas_present():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"screen", "simulate", "save_strategy", "describe", "inspect", "adjust_analysis"}


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


def test_assemble_simulate_raises_not_assembled():
    # simulate는 assemble_ir 경로를 거치지 않는다 — run_simulate가 compile_strategy로 IR을 만든다.
    import pytest
    with pytest.raises(ValueError):
        assemble_ir("simulate", {"nl": "삼성전자 종가 전략"})


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
    res = {"success": True, "query": "select", "as_of": "2026-06-17", "universe_size": 10,
           "eligible_size": 4,
           "results": [{"symbol": "AAA", "score": 0.82}, {"symbol": "BBB", "score": 0.79}]}
    out = compact_summary("screen", res)
    assert "AAA" in out and "0.82" in out and "2026-06-17" in out


def test_compact_simulate():
    res = {"success": True, "equity": [100, 101, 102],
           "metrics": {"cagr": 3.46, "sharpe": 0.26, "mdd": -65.7, "total_return": 64.4}}
    out = compact_summary("simulate", res)
    assert "백테스트" in out and "3.46" in out and "샤프" in out


def test_compact_simulate_period_surfaces_buckets():
    """②관측 근본수정: simulate가 연도분할이면 요약이 연도별 buckets를 담는다(이전엔 4스칼라뿐 →
    모델이 연도 수치를 못 봐 재실행하던 헛돌이의 근본 차단)."""
    res = {"success": True, "axis": "period_split",
           "metrics": {"cagr": -9.4, "sharpe": -0.39},
           "buckets": {"2015": {"cum_return": 3.1, "cagr": 3.1, "sharpe": 0.26, "mdd": -10, "n": 250},
                       "2024": {"cum_return": -23.3, "cagr": -23.3, "sharpe": -1.1, "mdd": -25, "n": 240}}}
    out = compact_summary("simulate", res)
    assert "2015" in out and "2024" in out and "3.1" in out and "-23.3" in out


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
    # 4-arg 시그니처: session, user_id, conversation_id, tool_input.
    # 직전 simulate IR을 대화에서 재사용하는 경로 — 유효 IR을 simulate tool_result로 영속해 검증.
    from app.models import Conversation, Message
    eng = _db()
    with Session(eng) as s:
        u = User(email="x@x.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        s.add(Message(conversation_id=c.id, role="assistant", parts=[
            {"type": "tool_result", "name": "simulate",
             "result": {"success": True, "ir": _IR_DEF}}]))
        s.commit()
        res = chat_tools.save_strategy_tool(s, u.id, c.id, {"name": "내 전략"})
        assert res["success"] is True and res["strategy_id"]
        row = s.get(Strategy, res["strategy_id"])
        assert row.run_mode == "draft" and row.engine == "ir"   # 저장-only 스코프=draft
        assert row.name == "내 전략" and row.user_id == u.id      # name 인자가 IR.name으로 주입


def test_save_strategy_tool_no_prior_simulate_no_nl_returns_error():
    # 이전 simulate 없고 nl도 없으면 오류 반환.
    from app.models import Conversation
    eng = _db()
    with Session(eng) as s:
        u = User(email="y@y.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        res = chat_tools.save_strategy_tool(s, u.id, c.id, {"name": "깨진"})
        assert res["success"] is False and "error" in res       # 예외 대신 모델 피드백용 error


def test_compact_save_strategy():
    out = compact_summary("save_strategy",
                          {"success": True, "strategy_id": 7, "name": "내 전략", "run_mode": "draft"})
    assert "내 전략" in out and "7" in out


# ── PR-B: adjust_analysis (①명세 — IR 핸들 값조정 재실행, nl 재컴파일 차단) ────

def _adjust_base_ir():
    return {"name": "t", "query": "simulate",
            "universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.momentum_12_1m"}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 100000000.0, "commission": 0.0003}}


def test_adjust_tool_registered():
    assert "adjust_analysis" in {t["name"] for t in TOOL_SCHEMAS}


def test_run_adjust_one_field_diff(monkeypatch):
    """값 1개만 바꾸고 나머지는 동일 — nl 재컴파일 없는 결정적 재실행(①)."""
    monkeypatch.setattr(chat_tools, "_last_simulate_ir", lambda s, c: _adjust_base_ir())
    monkeypatch.setattr(chat_tools, "_load_dataset", lambda ir: {})
    seen = {}
    monkeypatch.setattr(chat_tools, "strategy_from_spec",
                        lambda ir, ds: seen.update(ir=ir) or {"success": True, "equity": [1, 2],
                                                              "metrics": {"cagr": 1.0}})
    out = chat_tools.run_adjust(None, 1, {"changes": [{"path": "simulation.commission", "value": 0}]})
    assert out["success"] is True
    assert seen["ir"]["simulation"]["commission"] == 0                  # 바뀜
    assert seen["ir"]["simulation"]["initial_capital"] == 100000000.0   # 그대로
    assert seen["ir"]["position"]["entry"]["top_n"] == 2                # 그대로
    assert out["adjusted"] == ["simulation.commission=0.0"]
    assert "adjustable" in out                                          # 조정 패널 재동봉


def test_run_adjust_invalid_path(monkeypatch):
    monkeypatch.setattr(chat_tools, "_last_simulate_ir", lambda s, c: _adjust_base_ir())
    out = chat_tools.run_adjust(None, 1, {"changes": [{"path": "signal.op", "value": "x"}]})
    assert out["success"] is False and "조정 불가" in out["error"]


def test_run_adjust_no_prior(monkeypatch):
    monkeypatch.setattr(chat_tools, "_last_simulate_ir", lambda s, c: None)
    out = chat_tools.run_adjust(None, 1, {"changes": [{"path": "simulation.commission", "value": 0}]})
    assert out["success"] is False and "직전 분석" in out["error"]


def test_run_adjust_clamps_to_range(monkeypatch):
    """범위 밖 값은 매니페스트 min/max로 클램프(가드)."""
    monkeypatch.setattr(chat_tools, "_last_simulate_ir", lambda s, c: _adjust_base_ir())
    monkeypatch.setattr(chat_tools, "_load_dataset", lambda ir: {})
    seen = {}
    monkeypatch.setattr(chat_tools, "strategy_from_spec",
                        lambda ir, ds: seen.update(ir=ir) or {"success": True, "equity": [1]})
    chat_tools.run_adjust(None, 1, {"changes": [{"path": "simulation.commission", "value": 5}]})
    assert seen["ir"]["simulation"]["commission"] == 0.01              # max로 클램프


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
                          {"success": True, "report": "single", "symbol": "005930", "sector": "반도체",
                           "price": {"last": 72000}, "fundamentals": {"pb_ratio": 1.2,
                                                                      "trailing_pe": 10.0}})
    assert "005930" in out and "PBR" in out


def test_compact_inspect():
    out = compact_summary("inspect",
                          {"success": True, "query": "inspect", "symbol": "005930",
                           "columns": ["consensus_target"],
                           "dates": ["2026-06-16", "2026-06-17"],
                           "series": {"consensus_target": [81000.0, 82000.0]}})
    assert "005930" in out and "82000" in out                  # 최근값 표면화


# ── Task 2: run_simulate NL 위임 ─────────────────────────────────────────────

def test_run_simulate_delegates_to_compiler(monkeypatch):
    from app.chat import tools
    monkeypatch.setattr(tools, "compile_strategy", lambda s, uid, nl: {
        "success": True,
        "ir": {"universe": {"kind": "single", "symbols": ["005930"]},
               "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "query": "backtest"},
        "assumptions": ["가정1"], "explanation": {"summary": "단일종목 백테스트"}})
    monkeypatch.setattr(tools, "_load_dataset", lambda ir: {})
    monkeypatch.setattr(tools, "strategy_from_spec",
                        lambda ir, ds: {"success": True, "metrics": {"cagr": 0.1}})
    out = tools.run_simulate(session=None, user_id=1, tool_input={"nl": "삼성전자 종가 전략"})
    assert out["success"] is True and out["metrics"]["cagr"] == 0.1
    assert out["ir"]["universe"]["symbols"] == ["005930"]   # 검증 IR 동봉(저장 재사용·표시)
    assert out["explanation"]["summary"] == "단일종목 백테스트"


def test_run_simulate_compile_failure_is_graceful(monkeypatch):
    from app.chat import tools
    monkeypatch.setattr(tools, "compile_strategy", lambda s, uid, nl: {
        "success": False, "error": "검증을 통과하는 IR을 생성하지 못했습니다.", "ir": {}})
    out = tools.run_simulate(session=None, user_id=1, tool_input={"nl": "표현 불가"})
    assert out["success"] is False and "생성하지 못" in out["error"]


# ── Task 4: save_strategy NL 위임 + 마지막 IR 재사용 ──────────────────────────

def test_save_reuses_last_simulate_ir(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import User, Conversation, Message
    from app.chat import tools
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    saved = {}
    monkeypatch.setattr(tools, "save_ir_draft",
                        lambda s, uid, ir: saved.update({"ir": ir}) or type("R", (), {"id": 7, "name": ir.get("name")}))
    compiled = {"called": False}
    monkeypatch.setattr(tools, "compile_strategy",
                        lambda *a: compiled.update(called=True) or {"success": True, "ir": {"x": 1}})
    with Session(eng) as s:
        u = User(email="t@e.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        # 직전 simulate tool_result(검증 IR 동봉)를 영속
        s.add(Message(conversation_id=c.id, role="assistant", parts=[
            {"type": "tool_result", "name": "simulate",
             "result": {"success": True, "ir": {"universe": {"kind": "single", "symbols": ["005930"]}}}}]))
        s.commit()
        out = tools.save_strategy_tool(s, u.id, c.id, {"name": "내전략"})
    assert out["success"] is True and out["strategy_id"] == 7
    assert saved["ir"]["universe"]["symbols"] == ["005930"]   # 마지막 IR 재사용
    assert saved["ir"]["name"] == "내전략"
    assert compiled["called"] is False                         # 재컴파일 0(토큰 절감)


def test_save_compiles_when_no_prior_simulate(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import User, Conversation
    from app.chat import tools
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(tools, "compile_strategy",
                        lambda s, uid, nl: {"success": True, "ir": {"compiled": True}})
    monkeypatch.setattr(tools, "save_ir_draft",
                        lambda s, uid, ir: type("R", (), {"id": 9, "name": ir.get("name")}))
    with Session(eng) as s:
        u = User(email="t2@e.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        out = tools.save_strategy_tool(s, u.id, c.id, {"name": "새전략", "nl": "삼성 전략"})
    assert out["success"] is True and out["strategy_id"] == 9


# ── Task 5: screen sector 필터 ────────────────────────────────────────────────

def test_screen_sector_builds_screener():
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 3,
                                 "descending": False, "sector": "반도체"})
    sc = ir["universe"]["screener"]["condition"]
    assert sc["op"] == "is_in"
    assert sc["inputs"]["signal"]["op"] == "attribute"
    # attr=Industry(KSIC) 고정 — Sector(소속부)는 대형주 NaN이라 contains-match가 0건이 되는
    # 회귀 가드(실데이터 검증: 반도체주 Industry="반도체 제조업", Sector는 삼성 등 nan).
    assert sc["inputs"]["signal"]["params"]["attr"] == "Industry"
    assert sc["params"]["values"] == ["반도체"] and sc["params"]["match"] == "contains"


def test_screen_without_sector_unchanged():
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 3, "symbols": ["005930"]})
    assert ir["universe"] == {"kind": "list", "symbols": ["005930"]}
    assert "screener" not in ir["universe"]


# ── Task 6: inspect 미존재 컬럼 시 유효 컬럼 목록 피드백 ─────────────────────────

def test_inspect_unknown_column_returns_valid_options(monkeypatch):
    import pandas as pd
    from app.chat import tools
    df = pd.DataFrame({"Close": [1.0, 2.0], "consensus_target": [3.0, 4.0]})
    monkeypatch.setattr(tools.qc, "load_dataset_for", lambda syms: {"005930": df})
    out = tools.run_inspect({"symbol": "005930", "columns": ["target_price"]})
    assert out["success"] is False
    assert "Close" in out["error"] and "consensus_target" in out["error"]   # 유효 컬럼 제시


# ── P3: 실시간 변수조정 — 결과에 조정가능 매니페스트 동봉 ──────────────────────

def test_run_tool_attaches_adjustable_manifest(monkeypatch):
    """screen(select) 결과에 '변수 조정' 매니페스트(adjustable) 동봉."""
    monkeypatch.setattr(chat_tools, "_load_dataset", lambda ir: {"A": object()})
    monkeypatch.setattr(chat_tools, "strategy_from_spec",
                        lambda ir, ds: {"success": True, "query": "select", "results": []})
    out = chat_tools.run_tool("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 4, "descending": False})
    assert isinstance(out.get("adjustable"), list)
    assert "select.top_n" in {p["path"] for p in out["adjustable"]}


def test_run_simulate_attaches_adjustable_manifest(monkeypatch):
    """simulate 결과에 조정 노브(top_n·commission·initial_capital) 동봉."""
    from app.chat import tools
    monkeypatch.setattr(tools, "compile_strategy", lambda s, uid, nl: {
        "success": True,
        "ir": {"universe": {"kind": "all"},
               "signal": {"op": "data", "params": {"ref": "__SELF__.momentum_12_1m"}},
               "query": "simulate",
               "position": {"entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 5}},
               "simulation": {"initial_capital": 1e7}},
        "assumptions": [], "explanation": {}})
    monkeypatch.setattr(tools, "_load_dataset", lambda ir: {})
    monkeypatch.setattr(tools, "strategy_from_spec",
                        lambda ir, ds: {"success": True, "metrics": {"cagr": 0.1}})
    out = tools.run_simulate(session=None, user_id=1, tool_input={"nl": "모멘텀"})
    paths = {p["path"] for p in out["adjustable"]}
    assert {"position.entry.top_n", "simulation.commission", "simulation.initial_capital"} <= paths
