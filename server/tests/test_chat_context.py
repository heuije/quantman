"""P4 context 사이드카 — attach_context가 엔진 밖에서 준실시간 시세·뉴스를 best-effort로 붙인다.

골든 무누출(엔진 결과에 표시·해석용 키만 추가)·형상 선택성·graceful 실패를 고정한다.
"""
from __future__ import annotations

from app.chat import context as ctx


def test_attach_context_describe_single(monkeypatch):
    monkeypatch.setattr(ctx.live_quote, "quotes_for", lambda s: {"005930": {"price": 71500, "chg": 2.1}})
    monkeypatch.setattr(ctx.kis_master_cache, "get_name", lambda c: "삼성전자")
    monkeypatch.setattr(ctx.news_kr, "fetch_news", lambda q, display=5: [{"title": "뉴스1", "link": "x"}])
    out = ctx.attach_context({"success": True, "shape": "describe_single", "symbol": "005930"})
    assert out["context"]["quotes"]["005930"]["price"] == 71500
    assert out["context"]["news"][0]["title"] == "뉴스1"
    assert "source" in out["context"]


def test_attach_context_skips_non_describe(monkeypatch):
    """simulate 등 대상 외 형상엔 무변경 — 대상 종목이 없어 시세 조회조차 안 한다."""
    called: list = []
    monkeypatch.setattr(ctx.live_quote, "quotes_for", lambda s: called.append(1) or {})
    out = ctx.attach_context({"success": True, "shape": "simulate", "equity": []})
    assert "context" not in out and not called


def test_attach_context_graceful_on_network_fail(monkeypatch):
    """시세·뉴스 실패가 결과를 깨지 않는다(graceful) — context 없이 원결과 반환."""
    def boom(*a, **k):
        raise RuntimeError("network")
    monkeypatch.setattr(ctx.live_quote, "quotes_for", boom)
    monkeypatch.setattr(ctx.kis_master_cache, "get_name", lambda c: "삼성전자")
    monkeypatch.setattr(ctx.news_kr, "fetch_news", boom)
    out = ctx.attach_context({"success": True, "shape": "describe_single", "symbol": "005930"})
    assert out["success"] is True and "context" not in out


def test_attach_context_passes_all_symbols_to_router(monkeypatch):
    """KR·US·크립토 모두 quotes_for로 전달(분류·라우팅은 live_quote가 담당 — context는 미필터)."""
    seen: dict = {}
    monkeypatch.setattr(ctx.live_quote, "quotes_for", lambda s: seen.update({"syms": set(s)}) or {})
    ctx.attach_context({"success": True, "shape": "select",
                        "results": [{"symbol": "005930"}, {"symbol": "AAPL"}, {"symbol": "비트코인"}]})
    assert seen["syms"] == {"005930", "AAPL", "비트코인"}   # 미국·크립토도 전달(이전엔 KR만)


def test_attach_context_noop_on_failed_result():
    """success=False 결과엔 손대지 않는다."""
    out = ctx.attach_context({"success": False, "error": "x"})
    assert "context" not in out


def test_attach_context_breadth_market_snapshot(monkeypatch):
    """breadth엔 거시 시장 스냅샷(지수·VIX 현재가) 부착 — '코스피 왜'에 지수 레벨 제공."""
    monkeypatch.setattr(ctx.live_quote, "market_snapshot",
                        lambda: {"코스피": {"price": 9052.42, "chg": -0.13},
                                 "VIX": {"price": 16.78, "chg": 2.32}})
    out = ctx.attach_context({"success": True, "shape": "breadth", "n": 5})
    assert out["context"]["market"]["코스피"]["price"] == 9052.42
    assert out["context"]["market"]["VIX"]["chg"] == 2.32


def test_attach_context_breadth_market_graceful(monkeypatch):
    """시장 스냅샷 실패가 breadth 결과를 깨지 않는다."""
    def boom():
        raise RuntimeError("net")
    monkeypatch.setattr(ctx.live_quote, "market_snapshot", boom)
    out = ctx.attach_context({"success": True, "shape": "breadth", "n": 5})
    assert out["success"] is True and "context" not in out
