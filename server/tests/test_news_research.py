"""research_news 오케스트레이션 — 라우팅·dedup·상한·인용 결정적·graceful (fetcher·Haiku mock)."""
from __future__ import annotations

from app.chat import news_research as nr


def test_recent_routes_to_naver(monkeypatch):
    monkeypatch.setattr(nr.news_kr, "fetch_news",
                        lambda q, display=10: [{"title": "T", "link": "http://a", "desc": "d", "pub": "p"}])
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: "다이제스트[1]")
    out = nr.research_news(["삼성전자"], {"kind": "recent", "days": 7}, depth="full")
    assert out["shape"] == "news_research"
    assert out["citations"][0]["url"] == "http://a"
    assert out["digest"] == "다이제스트[1]"


def test_range_routes_to_gdelt_and_dedups(monkeypatch):
    arts = [{"title": "T1", "url": "http://a", "domain": "a", "date": "20260510", "lang": "ko"},
            {"title": "T2", "url": "http://a", "domain": "a", "date": "20260510", "lang": "ko"}]  # 중복 url
    monkeypatch.setattr(nr.news_gdelt, "fetch_gdelt", lambda q, start=None, end=None, max_records=20: arts)
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: "D")
    out = nr.research_news(["X"], {"kind": "range", "start": "2026-05-01", "end": "2026-05-31"})
    assert len(out["citations"]) == 1            # url 중복 제거


def test_caps_queries_and_articles(monkeypatch):
    monkeypatch.setattr(nr.news_gdelt, "fetch_gdelt",
                        lambda q, start=None, end=None, max_records=20:
                        [{"title": q, "url": f"http://{q}", "domain": "d", "date": "1", "lang": "ko"}])
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: "D")
    out = nr.research_news([f"q{i}" for i in range(10)],   # 10개 쿼리 → ≤4만 사용
                           {"kind": "range", "start": "2026-05-01", "end": "2026-05-31"}, max_articles=3)
    assert len(out["citations"]) <= 3


def test_digest_failure_falls_back_to_headlines(monkeypatch):
    monkeypatch.setattr(nr.news_kr, "fetch_news",
                        lambda q, display=10: [{"title": "헤드라인", "link": "http://a", "desc": "스니펫", "pub": "p"}])
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: None)   # 다이제스트 실패
    out = nr.research_news(["삼성전자"], {"kind": "recent", "days": 7})
    assert "헤드라인" in out["digest"]           # 폴백: 헤드라인+스니펫


def test_no_articles_returns_empty_note(monkeypatch):
    monkeypatch.setattr(nr.news_kr, "fetch_news", lambda q, display=10: [])
    out = nr.research_news(["없는종목"], {"kind": "recent", "days": 7})
    assert out["success"] is True and out["n"] == 0
