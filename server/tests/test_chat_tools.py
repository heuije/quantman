"""전략 연구소 챗봇 도구 테스트 — Task 3/4/5.

모든 테스트는 HERMETIC: 실데이터·엔진 실행 없이 monkeypatch로 격리.
conftest가 이미 sys.path를 in-repo core·server로 설정한다.
"""
# ── Task 3: tool schemas + assemble_ir ──────────────────────────────────────
from quant_core.ir_engine import StrategyIR
from app.chat.tools import assemble_ir, TOOL_SCHEMAS


class _NoDbSession:
    """순수-로직 도구 테스트용 세션 스텁 — DB 접근(_last_simulate_ir·compile_strategy 등)은 전부
    monkeypatch로 대체되므로 commit()만 제공한다. run_simulate/run_adjust가 백테스트 compute 전
    커넥션을 반납(session.commit·C1 부류 수정)한 이후 이 경로도 세션 객체를 요구한다
    (프로덕션 세션은 항상 실제 요청 세션이라 None이 아니다)."""
    def commit(self):
        pass


def test_tool_schemas_present():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"screen", "compare", "simulate", "save_strategy", "describe", "inspect",
                     "adjust_analysis", "research_news", "resolve_symbol"}


def test_assemble_compare_makes_valid_compare_ir():
    """compare 도구 → select mode=compare IR(랭킹 score 불요·지정 종목 나란히). 검증 통과해야."""
    ir = assemble_ir("compare", {"symbols": ["005930", "000660"],
                                 "metrics": ["trailing_pe", "pb_ratio", "market_cap"],
                                 "sort_by": "market_cap"})
    s = StrategyIR.model_validate(ir)          # 예외 없이 유효
    assert s.query == "select" and s.select.mode == "compare"
    assert s.select.display == ["trailing_pe", "pb_ratio", "market_cap"]
    assert s.select.sort_by == "market_cap"
    assert s.universe.kind == "list" and s.universe.symbols == ["005930", "000660"]
    import pytest
    with pytest.raises(ValueError):            # 종목 1개는 비교 불가
        assemble_ir("compare", {"symbols": ["005930"], "metrics": ["pb_ratio"]})


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


def test_assemble_screen_composite_grouped():
    """저평가 = 다밸류 지표 백분위 합 composite + 섹터별 top-N + 다섹터(P2) + 밸류>0 자격."""
    ir = assemble_ir("screen", {
        "score_refs": ["__SELF__.pb_ratio", "__SELF__.trailing_pe", "__SELF__.ev_ebitda"],
        "sectors": ["반도체", "배터리"], "group_by": "Sector", "top_n": 3})
    s = StrategyIR.model_validate(ir)                 # composite IR 유효(예외 없음)
    assert s.query == "select"
    assert s.select.group_by == "Sector" and s.select.top_n == 3
    assert s.select.descending is False              # 백분위 합 낮을수록 저평가
    assert s.signal.op == "binary"                   # composite = rank 합 트리
    assert {"pb_ratio", "trailing_pe", "ev_ebitda"} <= set(s.select.display)  # 팩터 표시(투명)
    # 자격 = 섹터 필터(is_in) + 밸류 멀티플 각각 >0(적자·음수 제외)을 AND 결합.
    cond = ir["universe"]["screener"]["condition"]
    assert cond["op"] == "logic" and cond["params"]["logic"] == "AND"
    sub = list(cond["inputs"].values())
    # 사용자어("배터리")가 업종으로 정규화·확장 — 반도체=KSIC(KR)+GICS(US), 2차전지=KSIC만.
    sector_c = next(c for c in sub if c["op"] == "is_in")
    assert sector_c["params"]["values"] == \
        ["반도체 제조업", "Semiconductors", "Semiconductor Materials & Equipment",
         "일차전지 및 이차전지 제조업"]
    # 3개 밸류 멀티플 각각 >0 자격 — 음수(적자) 종목이 '최저평가'로 오선별되는 것 차단.
    pos_refs = {c["inputs"]["left"]["params"]["ref"] for c in sub if c["op"] == "compare"}
    assert pos_refs == {"__SELF__.pb_ratio", "__SELF__.trailing_pe", "__SELF__.ev_ebitda"}
    assert all(c["params"]["op"] == ">" and c["inputs"]["right"]["params"]["value"] == 0.0
               for c in sub if c["op"] == "compare")


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

    def fake_run(ir, dataset, **kw):
        captured["ran"] = (ir, dataset)
        return {"success": True, "query": "select", "results": []}

    monkeypatch.setattr(chat_tools, "_load_dataset", fake_load)
    monkeypatch.setattr(chat_tools, "_manifest", lambda ds: None)   # 게이트 빌드 격리(HERMETIC)
    monkeypatch.setattr(chat_tools, "strategy_from_spec", fake_run)

    out = chat_tools.run_tool("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 2})
    assert out["success"] is True
    assert captured["ir"]["query"] == "select"          # 조립된 IR이 전달됨
    assert captured["ran"][0]["query"] == "select"


def test_run_tool_bad_input_returns_error_not_raises(monkeypatch):
    # assemble_ir가 실패하면 예외 대신 error dict(루프가 모델에 피드백)
    out = chat_tools.run_tool("screen", {"top_n": 2})   # score_ref 누락 → KeyError 내부
    assert out["success"] is False and "error" in out


# ── P0 (구조 재설계): 엔진 예외 계약화(#C) + 심볼 별칭(#D) ─────────────────────
def test_run_engine_contracts_engine_exception(monkeypatch):
    """#C: 엔진의 예기치 못한 raise를 정직한 infeasible 결과로 수렴 — agent 캐치올의 일반 오귀인
    ('조건을 단순하게 하거나 종목·기간을 좁혀')을 없애고 종목·예외타입을 진단에 보존한다. 프로덕션 실측:
    삼성전자 이벤트스터디가 같은 조건인데 첫 호출은 infeasible·재시도는 성공(비결정 예외)이라 '조건 문제'가
    아니었는데 사용자에게 조건 단순화를 요구하던 부류를 닫는다."""
    monkeypatch.setattr(chat_tools, "_manifest", lambda ds: None)

    def boom(ir, dataset, **kw):
        raise RuntimeError("simulated engine crash")

    monkeypatch.setattr(chat_tools, "strategy_from_spec", boom)
    out = chat_tools._run_engine({"universe": {"symbols": ["005930", "000660"]},
                                  "query": "relate"}, {"005930": object()})
    assert out["success"] is False and out["status"] == "infeasible"
    assert out["diagnostics"]["symbols"] == ["005930", "000660"]
    assert out["diagnostics"]["exc"] == "RuntimeError"
    assert "005930" in out["error"] and "RuntimeError" in out["error"]
    assert "조건을 단순하게" not in out["error"]           # 오귀인 제거(#D2)


def test_run_engine_passes_success_through(monkeypatch):
    """정상 결과는 그대로 통과 — 계약이 성공 경로를 왜곡하지 않는다."""
    monkeypatch.setattr(chat_tools, "_manifest", lambda ds: None)
    monkeypatch.setattr(chat_tools, "strategy_from_spec",
                        lambda ir, ds, **kw: {"success": True, "query": "relate"})
    out = chat_tools._run_engine({"universe": {"symbols": ["005930"]}}, {"005930": object()})
    assert out["success"] is True and out["query"] == "relate"


def test_symbol_alias_resolves_fx_and_passes_through():
    """#D: 통용 티커(USDKRW·DXY)를 정식 수집명(원달러환율·무역가중달러지수)으로 — 프로덕션 실측에서
    봇이 inspect(USDKRW)로 4연속 조회 실패했으나 데이터는 '원달러환율'로 이미 수집돼 있었다(데이터 갭
    아닌 해상도 갭). 비매크로(개별종목)는 원문 유지."""
    from app.compile_service import resolve_symbol_alias
    assert resolve_symbol_alias("USDKRW") == "원달러환율"
    assert resolve_symbol_alias("usd/krw") == "원달러환율"
    assert resolve_symbol_alias("KRW=X") == "원달러환율"
    assert resolve_symbol_alias("DXY") == "무역가중달러지수"
    assert resolve_symbol_alias("005930") == "005930"     # 개별종목 코드 pass-through
    assert resolve_symbol_alias("AAPL") == "AAPL"


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


def test_compact_surfaces_status_header_for_empty():
    """결과 품질 계약(R1) — status≠ok면 compact_summary가 ⚠ 헤더로 모델에 표면화해
    '거래가 없어 0%'를 '손실로 0%'와 구분하고 맹목 재실행을 막는다."""
    res = {"success": True, "shape": "simulate", "status": "empty", "equity": [100, 100],
           "verdict": "거래 0건 — 신호가 한 번도 충족되지 않았습니다.",
           "metrics": {"cagr": 0.0, "total_return": 0.0, "n_trades": 0}}
    out = compact_summary("simulate", res)
    assert out.startswith("⚠ 결과상태=empty") and "재실행하지 말고" in out


def test_compact_ok_no_warning_header():
    res = {"success": True, "shape": "simulate", "status": "ok", "verdict": "",
           "equity": [100, 110], "metrics": {"cagr": 5.0, "total_return": 10.0, "n_trades": 50}}
    out = compact_summary("simulate", res)
    assert "결과상태" not in out and "백테스트" in out


def test_compact_surfaces_ok_caveat_verdict():
    """P3-c: status=ok지만 verdict에 정량 caveat(P3-a 레짐 편중 등)가 있으면 compact_summary가
    '[참고: …]'로 모델에 노출한다 — ok라고 caveat가 조용히 드롭되면 함정 경고가 사용자에 미도달."""
    res = {"success": True, "shape": "event_study", "status": "ok", "n_events": 25,
           "verdict": "주의(레짐 편중) — 이벤트 25건 중 20건(80%)이 2023년에 집중됐습니다.",
           "diagnostics": {"top_year_share": 0.8}, "overall": {}, "windows": []}
    out = compact_summary("relate", res)
    assert "[참고:" in out and "레짐 편중" in out and "80%" in out   # 편중 경고가 모델 식단 맨 앞에


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


# ── T1 (Wave 2): 방법론 자기서술 — 모델이 백테스트 로직을 봄 ────────────────────
_BT_IR = {"universe": {"kind": "list", "symbols": ["005930"]},
          "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
              "left": {"op": "data", "params": {"ref": "__SELF__.pct_change_1d"}},
              "right": {"op": "const", "params": {"value": 0.001}}}},
          "position": {"direction": "long", "entry": {"mode": "on_signal"},
                       "sizing": {"mode": "pct_cash", "amount_pct": 10.0},
                       "exit": {"hold_days": 5}}}


def test_compact_includes_methodology_for_backtest():
    """T1: 백테스트 결과면 compact_summary가 방법론(기간·기준자본·종목·방향·사이징)을 모델 식단
    앞에 노출 → 모델이 백테스트 로직을 답에 서술한다(#7·#3). 기간은 equity 날짜에서 파생."""
    res = {"success": True, "shape": "simulate", "ir": _BT_IR,
           "equity": [{"date": "2015-01-02", "value": 100.0},
                      {"date": "2024-12-30", "value": 150.0}],
           "metrics": {"cagr": 3.46, "sharpe": 0.26, "mdd": -20.0,
                       "total_return": 50.0, "n_trades": 30}}
    out = compact_summary("simulate", res)
    assert "[분석 방법]" in out
    assert "2015-01-02~2024-12-30" in out          # 기간(equity 날짜)
    assert "100,000,000" in out                    # 기준자본 1억
    assert "[백테스트]" in out and "3.46" in out     # 메트릭은 그대로 유지
    assert out.index("[분석 방법]") < out.index("[백테스트]")   # 방법론이 메트릭 앞(맥락 먼저)


def test_compact_no_methodology_for_non_backtest():
    """스크린·종목분석 등 비백테스트는 방법론 블록을 붙이지 않는다(방향/청산이 무의미)."""
    res = {"success": True, "query": "select", "shape": "select", "as_of": "2026-06-17",
           "universe_size": 10, "results": [{"symbol": "AAA", "score": 0.8}],
           "ir": {"universe": {"kind": "all"}, "signal": {"op": "data", "params": {"ref": "x"}},
                  "query": "select", "select": {"top_n": 3}}}
    out = compact_summary("screen", res)
    assert "[분석 방법]" not in out and "AAA" in out


def test_serialize_preserves_event_composition():
    """T7: axis 직렬화가 composition을 보존한다(모델·UI·엑셀이 구성 분해를 받게 — 화이트리스트)."""
    from app.serialize import serialize_ir_result
    raw = {"success": True, "axis": "time", "basis": "close", "n_events": 3, "windows": ["5"],
           "overall": {"5": {"mean": 1.0}}, "shape": "event_study",
           "composition": {"by_symbol": {"AAA": 2, "BBB": 1}, "by_year": {"2020": 3}}}
    out, kind = serialize_ir_result(raw)
    assert kind == "axis"
    assert out.get("composition", {}).get("by_symbol", {}).get("AAA") == 2


def test_attach_methodology_for_backtest():
    """T2: 백테스트 결과에 structured 방법론(기간·기준자본·confirmed/assumed)이 붙어 웹이 패널 렌더."""
    res = {"success": True, "shape": "simulate", "ir": _BT_IR,
           "equity": [{"date": "2015-01-02", "value": 100.0},
                      {"date": "2024-12-30", "value": 150.0}],
           "metrics": {"cagr": 3.0}}
    out = chat_tools.attach_methodology(res)
    m = out["methodology"]
    assert m["period"] == "2015-01-02~2024-12-30"
    assert m["initial_capital"] == 100_000_000          # 기본 1억(#3)
    labels = {d["label"] for d in m["confirmed"]}
    assert "방향" in labels and "사이징" in labels
    assert m["assumed"]                                  # 수수료·슬리피지 등 가정


def test_attach_methodology_data_source_non_backtest():
    """비백테스트(select)도 **데이터 출처**는 붙는다(신뢰 소스 표면화) — 단 기간·기준자본·실행가정
    같은 백테스트 전용 방법론 필드는 붙지 않는다(그건 simulate/sweep에만)."""
    res = {"success": True, "shape": "select", "ir": {"universe": {"kind": "all"},
           "signal": {"op": "data", "params": {"ref": "x"}}, "query": "select",
           "select": {"top_n": 3}}}
    out = chat_tools.attach_methodology(res)
    meth = out.get("methodology") or {}
    assert meth.get("data_source")                       # 데이터 출처는 명시(뉴스 아닌 신뢰 소스)
    assert "period" not in meth and "initial_capital" not in meth   # 백테스트 전용 필드는 미부착


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
                        lambda ir, ds, **kw: seen.update(ir=ir) or {"success": True, "equity": [1, 2],
                                                                    "metrics": {"cagr": 1.0}})
    out = chat_tools.run_adjust(_NoDbSession(), 1, {"changes": [{"path": "simulation.commission", "value": 0}]})
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
                        lambda ir, ds, **kw: seen.update(ir=ir) or {"success": True, "equity": [1]})
    chat_tools.run_adjust(_NoDbSession(), 1, {"changes": [{"path": "simulation.commission", "value": 5}]})
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
                        lambda ir, ds, **kw: {"success": True, "metrics": {"cagr": 0.1}})
    out = tools.run_simulate(session=_NoDbSession(), user_id=1, tool_input={"nl": "삼성전자 종가 전략"})
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
    cond = ir["universe"]["screener"]["condition"]
    # 섹터 필터(is_in) + 밸류 멀티플>0 자격을 AND 결합(pb_ratio는 밸류 멀티플).
    assert cond["op"] == "logic" and cond["params"]["logic"] == "AND"
    sub = list(cond["inputs"].values())
    sector_c = next(c for c in sub if c["op"] == "is_in")
    # attr=Industry(KSIC) 고정 — Sector(소속부)는 대형주 NaN이라 contains-match 0건 회귀 가드.
    assert sector_c["inputs"]["signal"]["params"]["attr"] == "Industry"
    # 사용자어 "반도체" → 업종 정규화·확장: KSIC(KR)+GICS(US) 둘 다.
    assert sector_c["params"]["values"] == \
        ["반도체 제조업", "Semiconductors", "Semiconductor Materials & Equipment"]
    assert sector_c["params"]["match"] == "contains"
    # pb_ratio>0 자격 — 음수 자본(적자) 종목이 '최저평가'로 오선별되는 것 차단.
    pos = next(c for c in sub if c["op"] == "compare")
    assert pos["params"]["op"] == ">" and pos["inputs"]["left"]["params"]["ref"] == "__SELF__.pb_ratio"
    assert pos["inputs"]["right"]["params"]["value"] == 0.0


def test_screen_without_sector_unchanged():
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 3, "symbols": ["005930"]})
    # 명시 종목 리스트는 사용자 선택 존중 — 밸류 멀티플이어도 자격필터 미적용.
    assert ir["universe"] == {"kind": "list", "symbols": ["005930"]}


def test_screen_valuation_single_ref_positivity_guard():
    """밸류 멀티플 단일 ref(섹터 없음) — >0 자격이 붙어 음수(적자) EV/EBITDA가 제외된다."""
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.ev_ebitda", "top_n": 5, "descending": False})
    cond = ir["universe"]["screener"]["condition"]
    assert cond["op"] == "compare" and cond["params"]["op"] == ">"
    assert cond["inputs"]["left"]["params"]["ref"] == "__SELF__.ev_ebitda"
    assert cond["inputs"]["right"]["params"]["value"] == 0.0


def test_screen_non_valuation_ref_no_positivity_guard():
    """밸류 멀티플이 아닌 지표(모멘텀)는 >0 자격을 붙이지 않는다(음수 모멘텀 정상)."""
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.momentum_12_1m", "top_n": 5})
    assert "screener" not in ir["universe"]   # 자격 조건 없음 → 순수 all
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
    monkeypatch.setattr(chat_tools, "_manifest", lambda ds: None)   # 게이트 빌드 격리(HERMETIC)
    monkeypatch.setattr(chat_tools, "strategy_from_spec",
                        lambda ir, ds, **kw: {"success": True, "query": "select", "results": []})
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
                        lambda ir, ds, **kw: {"success": True, "metrics": {"cagr": 0.1}})
    out = tools.run_simulate(session=_NoDbSession(), user_id=1, tool_input={"nl": "모멘텀"})
    paths = {p["path"] for p in out["adjustable"]}
    assert {"position.entry.top_n", "simulation.commission", "simulation.initial_capital"} <= paths


# ── WS5: 심볼 발견성 — resolve_symbol 도구 + 미스 주도 제안 ─────────────────────
# 프로덕션 실측 부류: 오코드 연쇄(노바렉스·코스맥스엔비티)·매크로 심볼 추측 포기(원달러환율).

def test_resolve_symbol_tool_registered():
    from app.chat.tools import TOOL_SCHEMAS
    assert "resolve_symbol" in {t["name"] for t in TOOL_SCHEMAS}


def test_resolve_symbol_finds_kr_stock_by_name():
    out = chat_tools.run_tool("resolve_symbol", {"query": "노바렉스"})
    assert out["success"] and out["shape"] == "resolve_symbol"
    assert out["candidates"][0]["symbol"] == "194700"


def test_resolve_symbol_alias_then_search():
    """통용 티커(USDKRW)는 별칭(#D) 경유 후 검색 — 정식 매크로 심볼키가 1위."""
    out = chat_tools.run_tool("resolve_symbol", {"query": "USDKRW"})
    assert out["success"] and out["candidates"][0]["symbol"] == "원달러환율"


def test_resolve_symbol_miss_is_honest_failure():
    out = chat_tools.run_tool("resolve_symbol", {"query": "zzz존재하지않는이름zzz"})
    assert out["success"] is False and "찾지 못했습니다" in out["error"]


def test_resolve_symbol_empty_query():
    out = chat_tools.run_tool("resolve_symbol", {"query": "  "})
    assert out["success"] is False


def test_resolve_symbol_compact_summary_lists_candidates():
    out = chat_tools.run_tool("resolve_symbol", {"query": "코스맥스엔비티"})
    text = chat_tools.compact_summary("resolve_symbol", out)
    assert "222040" in text and "코스맥스엔비티" in text


def test_inspect_miss_suggests_candidates(monkeypatch):
    """심볼 미해결 실패가 유사 후보를 동봉 — 봇의 연쇄 추측(USDKRW→KRW=X…)을 근본 차단."""
    from app.chat import tools
    monkeypatch.setattr(tools.qc, "load_dataset_for", lambda syms: {})
    out = tools.run_inspect({"symbol": "노바렉수", "columns": ["Close"]})   # 오타 질의
    assert out["success"] is False
    assert "노바렉스" in out["error"] and "194700" in out["error"]
