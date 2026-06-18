"""compile_strategy 공유 헬퍼 — compile_nl 배선을 단일화(router·chat 공용).
compile_nl(LLM)은 monkeypatch로 격리한다(실 API·네트워크 없음)."""
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import User


def _engine_user():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        u = User(email="t@example.com"); s.add(u); s.commit(); s.refresh(u)
        return e, u.id


def test_compile_strategy_success(monkeypatch):
    import app.compile_service as cs
    captured = {}
    def _fake_compile_nl(nl, **kw):
        captured["nl"] = nl; captured["kw"] = kw
        return {"success": True, "ir": {"universe": {"kind": "single", "symbols": ["005930"]},
                "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}},
                "assumptions": ["가정1"], "issues": [], "repair_count": 0}
    monkeypatch.setattr(cs, "compile_nl", _fake_compile_nl)
    eng, uid = _engine_user()
    with Session(eng) as s:
        out = cs.compile_strategy(s, uid, "삼성전자 종가 전략")
    assert out["success"] is True and out["ir"]["universe"]["symbols"] == ["005930"]
    assert captured["nl"] == "삼성전자 종가 전략"
    # 배선 인자가 모두 전달됐는지(추출 정확성)
    assert set(captured["kw"]) >= {"catalog", "capabilities", "indicator_cols",
                                   "valid_keys", "name_map", "validate_fn"}
    assert "explanation" in out and out["explanation"] is not None  # explain_ir 부착


def test_validate_flags_unknown_simulation_field(monkeypatch):
    """_validate가 commission_pct(SimSpec에 없는 환각 필드)를 repair 이슈로 잡아 ok=False —
    extra='ignore'가 조용히 버리기 전에 raw에서 포착(라이브 '비용 무시' 결함의 구조적 차단)."""
    import app.compile_service as cs
    captured = {}
    def _fake(nl, **kw):
        captured["validate_fn"] = kw["validate_fn"]
        return {"success": True, "ir": {}, "assumptions": [], "issues": [], "repair_count": 0}
    monkeypatch.setattr(cs, "compile_nl", _fake)
    eng, uid = _engine_user()
    with Session(eng) as s:
        cs.compile_strategy(s, uid, "x")
    vf = captured["validate_fn"]
    sig = {"op": "data", "params": {"ref": "__SELF__.Close"}}
    base = {"universe": {"kind": "single", "symbols": ["005930"]}, "signal": sig}
    issues, ok = vf({**base, "simulation": {"commission_pct": 0.01}})
    assert ok is False
    assert any(i["path"] == "simulation.commission_pct" and i["is_error"] for i in issues)
    # 정상 필드(commission 분수)는 unknown_field로 거짓 거부하지 않는다
    issues2, _ = vf({**base, "simulation": {"commission": 0.0001}})
    assert not any(i.get("rule") == "unknown_field" for i in issues2)


def test_compile_strategy_failure(monkeypatch):
    import app.compile_service as cs
    monkeypatch.setattr(cs, "compile_nl", lambda nl, **kw: {
        "success": False, "error": "검증을 통과하는 IR을 생성하지 못했습니다.",
        "ir": {}, "assumptions": [], "issues": [], "repair_count": 2})
    eng, uid = _engine_user()
    with Session(eng) as s:
        out = cs.compile_strategy(s, uid, "표현 불가")
    assert out["success"] is False and "생성하지 못" in out["error"]
    assert out["explanation"] is None
