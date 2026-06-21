"""P4 context 사이드카 — attach_context가 엔진 밖에서 준실시간 시세·뉴스를 best-effort로 붙인다.

골든 무누출(엔진 결과에 표시·해석용 키만 추가)·형상 선택성·graceful 실패를 고정한다.
"""
from __future__ import annotations

from app.chat import context as ctx


def test_attach_context_describe_single(monkeypatch):
    monkeypatch.setattr(ctx, "_naver_quotes", lambda codes, b: {"005930": {"close": 71500, "chg": 2.1}})
    monkeypatch.setattr(ctx, "_bucket", lambda: "t1")
    monkeypatch.setattr(ctx.kis_master_cache, "get_name", lambda c: "삼성전자")
    monkeypatch.setattr(ctx.news_kr, "fetch_news", lambda q, display=5: [{"title": "뉴스1", "link": "x"}])
    out = ctx.attach_context({"success": True, "shape": "describe_single", "symbol": "005930"})
    assert out["context"]["quotes"]["005930"]["close"] == 71500
    assert out["context"]["news"][0]["title"] == "뉴스1"
    assert "source" in out["context"]


def test_attach_context_skips_non_describe(monkeypatch):
    """simulate 등 대상 외 형상엔 무변경 — 대상 종목이 없어 시세 조회조차 안 한다."""
    called: list = []
    monkeypatch.setattr(ctx, "_naver_quotes", lambda codes, b: called.append(1) or {})
    out = ctx.attach_context({"success": True, "shape": "simulate", "equity": []})
    assert "context" not in out and not called


def test_attach_context_graceful_on_network_fail(monkeypatch):
    """시세·뉴스 실패가 결과를 깨지 않는다(graceful) — context 없이 원결과 반환."""
    def boom(*a, **k):
        raise RuntimeError("network")
    monkeypatch.setattr(ctx, "_naver_quotes", boom)
    monkeypatch.setattr(ctx, "_bucket", lambda: "t1")
    monkeypatch.setattr(ctx.kis_master_cache, "get_name", lambda c: "삼성전자")
    monkeypatch.setattr(ctx.news_kr, "fetch_news", boom)
    out = ctx.attach_context({"success": True, "shape": "describe_single", "symbol": "005930"})
    assert out["success"] is True and "context" not in out


def test_attach_context_only_kr_codes(monkeypatch):
    """네이버 국내 시세 — KR 6자리 코드만 대상(미국 티커 등 제외)."""
    seen: dict = {}
    monkeypatch.setattr(ctx, "_naver_quotes", lambda codes, b: seen.update({"codes": codes}) or {})
    monkeypatch.setattr(ctx, "_bucket", lambda: "t1")
    ctx.attach_context({"success": True, "shape": "select",
                        "results": [{"symbol": "005930"}, {"symbol": "AAPL"}, {"symbol": "000660"}]})
    assert set(seen["codes"]) == {"005930", "000660"}   # AAPL(비 6자리) 제외


def test_attach_context_noop_on_failed_result():
    """success=False 결과엔 손대지 않는다."""
    out = ctx.attach_context({"success": False, "error": "x"})
    assert "context" not in out
