"""기사 본문 추출 — <p> 폴백·길이상한·실패 graceful (HTTP mock; trafilatura 미설치 경로)."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.data.feeds import news_body  # noqa: E402


class _Resp:
    def __init__(self, text):
        self.text = text


def test_fetch_body_p_fallback(monkeypatch):
    html = "<html><body><p>첫 문단.</p><p>둘째 <b>문단</b>.</p></body></html>"
    monkeypatch.setattr(news_body.requests, "get", lambda *a, **k: _Resp(html))
    news_body.fetch_body.cache_clear()
    body = news_body.fetch_body("http://x.com/article-1")
    # 추출기(trafilatura/폴백)별 구두점 간격 차이가 있어 단어 단위로 검증
    assert "첫 문단" in body and "둘째 문단" in body


def test_fetch_body_length_cap(monkeypatch):
    html = "<p>" + ("가" * 9000) + "</p>"
    monkeypatch.setattr(news_body.requests, "get", lambda *a, **k: _Resp(html))
    news_body.fetch_body.cache_clear()
    assert len(news_body.fetch_body("http://x.com/article-2")) <= news_body._MAX


def test_fetch_body_graceful(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(news_body.requests, "get", boom)
    news_body.fetch_body.cache_clear()
    assert news_body.fetch_body("http://x.com/article-3") == ""
