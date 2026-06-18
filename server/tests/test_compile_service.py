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
