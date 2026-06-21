"""GDELT 과거/글로벌 뉴스 fetcher — 파싱·날짜범위·rate-limit graceful (HTTP mock)."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.data.feeds import news_gdelt  # noqa: E402


class _Resp:
    def __init__(self, text):
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


def test_fetch_gdelt_parses_articles(monkeypatch):
    payload = ('{"articles":[{"title":"Samsung earnings beat","url":"http://x.com/a",'
               '"domain":"x.com","seendate":"20260510T120000Z","language":"Korean"}]}')
    monkeypatch.setattr(news_gdelt, "_throttle", lambda: None)
    monkeypatch.setattr(news_gdelt.requests, "get", lambda *a, **k: _Resp(payload))
    out = news_gdelt.fetch_gdelt("Samsung", start="2026-05-01", end="2026-05-31")
    assert out[0]["url"] == "http://x.com/a"
    assert out[0]["title"] == "Samsung earnings beat"
    assert out[0]["date"] == "20260510T120000Z"


def test_fetch_gdelt_graceful_on_ratelimit_text(monkeypatch):
    monkeypatch.setattr(news_gdelt, "_throttle", lambda: None)
    monkeypatch.setattr(news_gdelt.requests, "get",
                        lambda *a, **k: _Resp("Please limit requests to one every 5 seconds"))
    assert news_gdelt.fetch_gdelt("KOSPI") == []   # 비-JSON 안내문 → 빈 리스트


def test_fetch_gdelt_date_params(monkeypatch):
    seen = {}
    monkeypatch.setattr(news_gdelt, "_throttle", lambda: None)
    monkeypatch.setattr(news_gdelt.requests, "get",
                        lambda url, params=None, **k: seen.update(params) or _Resp('{"articles":[]}'))
    news_gdelt.fetch_gdelt("X", start="2026-05-01", end="2026-05-31")
    assert seen["startdatetime"] == "20260501000000" and seen["enddatetime"] == "20260531235959"
