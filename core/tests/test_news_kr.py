"""B2 뉴스 — 네이버 검색 API 파싱·HTML정리·키미설정 처리 검증.

    cd platform/core && pytest tests/test_news_kr.py -v
"""
from quant_core.data.feeds import news_kr


def test_fetch_news_no_key_returns_empty(monkeypatch):
    """키 미설정이면 빈 리스트(정직 — 가짜 채움 0)."""
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    assert news_kr.fetch_news("삼성전자") == []


def test_clean_strips_tags_and_entities():
    """네이버 응답의 <b>·&quot;·&amp; 제거/디코드."""
    assert news_kr._clean('&quot;<b>삼성전자</b>&quot; HBM &amp; AI') == '"삼성전자" HBM & AI'


def test_fetch_news_parses(monkeypatch):
    """정상 응답 → title/link/desc 정리되어 파싱(originallink 우선)."""
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "y")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"title": "<b>삼성전자</b> 신고가", "originallink": "http://orig",
                               "link": "http://naver", "description": "HBM &amp; AI 동맹",
                               "pubDate": "Mon, 08 Jun 2026 21:00:00 +0900"}]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    out = news_kr.fetch_news("삼성전자", display=1)
    assert len(out) == 1
    assert out[0]["title"] == "삼성전자 신고가"
    assert out[0]["link"] == "http://orig"          # originallink 우선
    assert out[0]["desc"] == "HBM & AI 동맹"


def test_fetch_news_swallows_api_error(monkeypatch):
    """외부 API 예외는 빈 결과로 삼킨다(정직·무중단)."""
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "y")
    import requests

    def _boom(*a, **k):
        raise RuntimeError("network")

    monkeypatch.setattr(requests, "get", _boom)
    assert news_kr.fetch_news("삼성전자") == []
