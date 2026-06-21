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
    monkeypatch.setattr(live_quote, "_fx", lambda code, b: {"price": 1533.0, "chg": -0.33, "change": -5.0})
    snap = live_quote.market_snapshot()
    assert snap["코스피"]["price"] == 9052.42
    assert snap["코스닥"]["chg"] == -3.43
    assert snap["나스닥"]["price"] == 26517.93
    assert snap["VIX"]["chg"] == 2.32
    assert snap["원/달러"]["price"] == 1533.0      # FX 합류


def test_quotes_for_routes_by_symbol_type(monkeypatch):
    """KR(6자리)→domestic/stock · US(티커)→worldstock · 크립토→binance 자동 분류."""
    monkeypatch.setattr(live_quote, "_poll",
                        lambda cat, codes, b: {c: {"price": 1.0, "chg": 0.0} for c in codes.split(",")})
    monkeypatch.setattr(live_quote, "world_stock_quotes", lambda us: {t: {"price": 2.0} for t in us})
    monkeypatch.setattr(live_quote, "crypto_quotes", lambda cs: {n: {"price": 3.0} for n in cs})
    out = live_quote.quotes_for(("005930", "AAPL", "비트코인"))
    assert out["005930"]["price"] == 1.0     # KR
    assert out["AAPL"]["price"] == 2.0       # US
    assert out["비트코인"]["price"] == 3.0    # 크립토


def test_world_stock_quotes_resolves_reuters(monkeypatch):
    """티커→reutersCode resolve 후 worldstock 조회·역매핑(reutersCode→원티커)."""
    monkeypatch.setattr(live_quote, "_resolve_us", lambda t: f"{t}.O")
    monkeypatch.setattr(live_quote, "_poll", lambda cat, codes, b: {"AAPL.O": {"price": 298.0, "chg": 0.7}})
    out = live_quote.world_stock_quotes(("AAPL",))
    assert out["AAPL"]["price"] == 298.0


def test_crypto_quotes_maps_engine_name(monkeypatch):
    monkeypatch.setattr(live_quote, "_binance",
                        lambda sym, b: {"price": 64000.0, "chg": 0.8} if sym == "BTCUSDT" else None)
    out = live_quote.crypto_quotes(("비트코인",))
    assert out["비트코인"]["price"] == 64000.0


def test_fx_parses_calc_price(monkeypatch):
    payload = {"exchangeInfo": {"calcPrice": "1533.0", "fluctuationsRatio": "-0.33", "fluctuations": "-5.0"}}
    monkeypatch.setattr(live_quote.requests, "get", lambda *a, **k: _Resp(payload))
    out = live_quote._fx("FX_USDKRW", "tF")
    assert out["price"] == 1533.0 and out["chg"] == -0.33
