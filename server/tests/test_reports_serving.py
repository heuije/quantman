"""HOME 리포트 서빙 — reports_kr 피드 우선(원칙6) + 라이브 폴백 (무네트워크).

검증: 피드 parquet → 웹 KrReport 계약 매핑(12개월·최신순·목표가 None)과 피드 공백 시 라이브 폴백.
"""
import datetime as dt

import pandas as pd

from app import krdata


def _feed_df(rows):
    return pd.DataFrame(rows, columns=["nid", "as_of", "code", "broker", "title", "url", "target", "opinion"])


def test_reports_from_feed_maps_web_contract(monkeypatch):
    import quant_core.data_fetcher as data_fetcher
    today = dt.date.today()
    recent = (today - dt.timedelta(days=10)).isoformat()
    mid = (today - dt.timedelta(days=30)).isoformat()
    old = (today - dt.timedelta(days=400)).isoformat()
    monkeypatch.setattr(data_fetcher, "load_stock_reports", lambda code: _feed_df([
        (1, recent, "005930", "메리츠증권", "최신 리포트", "http://x/1.pdf", 560000, 1),
        (2, mid, "005930", "NH투자증권", "한달전 리포트", "http://x/2.pdf", None, 0),
        (3, old, "005930", "옛증권", "1년+ 전 리포트", "http://x/3.pdf", 999, 1),
    ]))
    krdata._reports_from_feed.cache_clear()
    out = krdata._reports_from_feed("005930", today.isoformat())
    # 12개월 컷오프 → old(400일) 제외, 최신순 2건
    assert [o["title"] for o in out] == ["최신 리포트", "한달전 리포트"]
    o = out[0]
    assert set(o) == {"date", "title", "broker", "url", "target"}
    assert o["date"] == (today - dt.timedelta(days=10)).strftime("%y.%m.%d")
    assert o["broker"] == "메리츠증권" and o["url"] == "http://x/1.pdf"
    assert o["target"] == 560000          # 피드가 상세에서 수집한 목표가 passthrough
    assert out[1]["target"] is None       # 미수집(Not Rated/백필 진행) → None


def test_reports_from_feed_empty_when_no_file(monkeypatch):
    import quant_core.data_fetcher as data_fetcher
    monkeypatch.setattr(data_fetcher, "load_stock_reports", lambda code: pd.DataFrame())
    krdata._reports_from_feed.cache_clear()
    assert krdata._reports_from_feed("999999", dt.date.today().isoformat()) == []


def test_reports_empty_feed_falls_back_to_live(monkeypatch):
    monkeypatch.setattr(krdata, "_reports_from_feed", lambda code, day: [])
    called = {}

    def fake_live(code):
        called["code"] = code
        return [{"date": "26.01.01", "title": "라이브", "broker": "X", "url": "u", "target": None}]

    monkeypatch.setattr(krdata, "_reports_live", fake_live)
    out = krdata.reports("123456")
    assert called["code"] == "123456" and out[0]["title"] == "라이브"


def test_reports_prefers_feed_over_live(monkeypatch):
    monkeypatch.setattr(krdata, "_reports_from_feed",
                        lambda code, day: [{"date": "26.07.06", "title": "피드",
                                            "broker": "M", "url": "u", "target": None}])

    def boom(code):
        raise AssertionError("피드가 비지 않으면 라이브 폴백 호출 금지")

    monkeypatch.setattr(krdata, "_reports_live", boom)
    assert krdata.reports("005930")[0]["title"] == "피드"
