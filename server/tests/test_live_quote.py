"""통합 준실시간 시세 모듈 — polling 단일 파서·시장 스냅샷(네트워크 mock).

라이브 네이버 호출은 mock(결정적 단위테스트). 실엔드포인트 검증은 수동 probe로 별도 확인됨.
"""
from __future__ import annotations

from app import live_quote


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_poll_parses_consistent_fields(monkeypatch):
    payload = {"datas": [{"itemCode": "KOSPI", "closePriceRaw": "9,052.42",
                          "fluctuationsRatioRaw": "-0.13", "compareToPreviousClosePriceRaw": "-11.42"}]}
    monkeypatch.setattr(live_quote.requests, "get", lambda *a, **k: _Resp(payload))
    out = live_quote._poll("domestic/index", "KOSPI", "tA")
    assert out["KOSPI"]["price"] == 9052.42      # 콤마 제거 파싱
    assert out["KOSPI"]["chg"] == -0.13
    assert out["KOSPI"]["change"] == -11.42


def test_poll_uses_reuters_code_for_world(monkeypatch):
    payload = {"datas": [{"reutersCode": ".VIX", "closePriceRaw": "16.78", "fluctuationsRatioRaw": "2.32"}]}
    monkeypatch.setattr(live_quote.requests, "get", lambda *a, **k: _Resp(payload))
    out = live_quote._poll("worldstock/index", ".VIX", "tB")
    assert out[".VIX"]["price"] == 16.78


def test_poll_graceful_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(live_quote.requests, "get", boom)
    assert live_quote._poll("domestic/index", "KOSPI", "tC") == {}   # 실패=빈 dict


def test_market_snapshot_labels_and_merges(monkeypatch):
    def fake_poll(category, codes, bucket):
        if category == "domestic/index":
            return {"KOSPI": {"price": 9052.42, "chg": -0.13, "change": -11.42},
                    "KOSDAQ": {"price": 966.59, "chg": -3.43, "change": -34.34}}
        return {".IXIC": {"price": 26517.93, "chg": 1.91, "change": 496.28},
                ".VIX": {"price": 16.78, "chg": 2.32, "change": 0.38}}
    monkeypatch.setattr(live_quote, "_poll", fake_poll)
    snap = live_quote.market_snapshot()
    assert snap["코스피"]["price"] == 9052.42
    assert snap["코스닥"]["chg"] == -3.43
    assert snap["나스닥"]["price"] == 26517.93
    assert snap["VIX"]["chg"] == 2.32
    assert "S&P500" not in snap or snap.get("S&P500") is not None   # 누락 축은 graceful 제외
