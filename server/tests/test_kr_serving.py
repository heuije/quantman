"""HOME 수급·컨센서스 서빙 — 데이터엔진 우선(flow_kr·reports_kr) + 폴백 (무네트워크).

수급=flow_kr(거래대금·원, 라이브 폴백 없음: 단위 상이). 컨센=reports_kr 증권사별 standing + 라이브 폴백.
"""
import pandas as pd

from app import krdata


def test_investor_from_flow_feed(monkeypatch):
    import quant_core.data_fetcher as data_fetcher
    idx = pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"])
    df = pd.DataFrame({"inst_net_buy": [1_000_000_000, -2_000_000_000, None],
                       "foreign_net_buy": [500_000_000, 1_000_000_000, 3_000_000_000]}, index=idx)
    monkeypatch.setattr(data_fetcher, "load_stock_flow", lambda c: df)
    krdata._investor_cached.cache_clear()
    out = krdata.investor("005930")
    assert [o["date"] for o in out] == ["2026.06.03", "2026.06.02", "2026.06.01"]   # 최신순
    o0 = out[0]                                   # 06-03: inst None→0, foreign 3e9(거래대금 원)
    assert o0["inst"] == 0 and o0["foreign"] == 3_000_000_000
    assert o0["indiv"] == -3_000_000_000          # ≈ −(기관+외국인)
    assert set(o0) == {"date", "inst", "foreign", "indiv"}


def test_investor_empty_when_no_flow(monkeypatch):
    import quant_core.data_fetcher as data_fetcher
    monkeypatch.setattr(data_fetcher, "load_stock_flow", lambda c: pd.DataFrame())
    krdata._investor_cached.cache_clear()
    assert krdata.investor("999999") == []        # 미커버 → 빈 결과(라이브 폴백 없음: 단위 불일치)


def _reports_raw(rows):
    return pd.DataFrame(rows, columns=["nid", "as_of", "code", "broker", "title", "url", "target", "opinion"])


def test_consensus_from_reports_standing(monkeypatch):
    import quant_core.data_fetcher as data_fetcher
    raw = _reports_raw([
        (1, "2026-05-01", "005930", "삼성증권", "t", "u", 100000, 1),
        (2, "2026-06-01", "005930", "삼성증권", "t", "u", 120000, 1),      # 삼성 갱신 100→120
        (3, "2026-06-10", "005930", "미래에셋증권", "t", "u", 150000, 0),
    ])
    monkeypatch.setattr(data_fetcher, "load_stock_reports", lambda c: raw)
    krdata._consensus_from_feed.cache_clear()
    out = krdata.consensus("005930")
    by = {r["broker"]: r for r in out}
    assert by["삼성증권"]["target"] == 120000 and by["삼성증권"]["prev_target"] == 100000
    assert by["삼성증권"]["change_pct"] == 20.0 and by["삼성증권"]["opinion"] == "BUY"
    assert by["미래에셋증권"]["target"] == 150000 and by["미래에셋증권"]["prev_target"] is None
    assert by["미래에셋증권"]["opinion"] == "HOLD"
    assert out[0]["broker"] == "미래에셋증권"        # 최신 리포트(06-10) 증권사 우선
    assert set(out[0]) == {"broker", "date", "target", "prev_target", "change_pct", "opinion"}


def test_consensus_falls_back_to_live(monkeypatch):
    monkeypatch.setattr(krdata, "_consensus_from_feed", lambda c, d: [])
    called = {}

    def fake_live(code):
        called["c"] = code
        return [{"broker": "X증권", "date": "26.01.01", "target": 1,
                 "prev_target": None, "change_pct": None, "opinion": "BUY"}]

    monkeypatch.setattr(krdata, "_consensus_live", fake_live)
    out = krdata.consensus("123456")
    assert called["c"] == "123456" and out[0]["broker"] == "X증권"
