"""LS 해외주식(미국) 경로 — 시장판정·잔고·시세·주문·취소·조회·예약 전수.
⚠ fixture는 research(overseas-stock-research.md) 기반. 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_broker as lb


def _broker():
    b = object.__new__(lb.LsBroker)
    b.account_no = "55500000000"
    b.virtual = True
    return b


def test_detect_market_domestic(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    assert b._detect_market("000660") == "DOMESTIC"


def test_detect_market_us(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: "NAS", raising=False)
    assert b._detect_market("AAPL") == "NAS"


def test_detect_market_unknown_us_ticker_raises(monkeypatch):
    """미국 티커 형태인데 인덱스에 없으면 추측 금지 → RoutingError(발주 차단)."""
    import pytest
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: False, raising=False)
    with pytest.raises(lb.market_index.RoutingError):
        b._detect_market("XYZ")


def test_ls_excd_mapping():
    b = _broker()
    assert b._ls_excd("NAS") == "82"   # NASDAQ
    assert b._ls_excd("NYS") == "81"   # NYSE
    assert b._ls_excd("AMS") == "81"   # AMEX→NYSE 통합(G23-1)


def test_ls_ticker_bare():
    """LS IsuNo/keysymbol는 bare 티커(A접두·슬래시 없음)."""
    b = _broker()
    assert b._ls_ticker("AAPL") == "AAPL"
    assert b._ls_ticker("aapl") == "AAPL"
