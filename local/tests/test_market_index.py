"""P2 — 해외 마스터 파서 + 종목→시장 라우팅 인덱스 단위테스트 (네트워크 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# localapp 패키지 import 가능하도록 local 디렉터리를 path에 추가
_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import kis_master, market_index


def _cod_line(cols: list[str]) -> str:
    """24컬럼 .cod 라인 합성 — 부족분은 빈 칸으로 채움."""
    full = list(cols) + [""] * (24 - len(cols))
    return "\t".join(full)


def _make_raw(rows: list[list[str]]) -> bytes:
    return "\n".join(_cod_line(r) for r in rows).encode("cp949")


# ── 파서 ────────────────────────────────────────────────────────────────────

def test_parse_overseas_basic():
    raw = _make_raw([
        # 0    1    2     3   4(ticker) 5        6(kr)  7(en)        8 9
        ["US", "22", "NAS", "", "AAPL", "NASAAPL", "애플", "APPLE INC", "2", "USD"],
        ["US", "22", "NAS", "", "QQQ",  "NASQQQ",  "QQQ", "INVESCO QQQ", "3", "USD"],
    ])
    rows = kis_master.parse_overseas_master(raw, "NAS")
    assert len(rows) == 2
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert aapl["exchange"] == "NAS"
    assert aapl["kind"] == "stock"
    assert aapl["currency"] == "USD"
    qqq = next(r for r in rows if r["symbol"] == "QQQ")
    assert qqq["kind"] == "etf_etn"          # sec_type 3 → ETF


def test_parse_overseas_skips_malformed():
    raw = _make_raw([
        ["US", "22", "NAS", "", "AAPL", "x", "애플", "APPLE", "2", "USD"],
    ])
    # 탭 없는 헤더/짧은 줄 섞임
    bad = (raw.decode("cp949") + "\nGARBAGE_NO_TABS\nshort\tline").encode("cp949")
    rows = kis_master.parse_overseas_master(bad, "NAS")
    assert [r["symbol"] for r in rows] == ["AAPL"]


# ── 라우팅 인덱스 (주입) ──────────────────────────────────────────────────────

@pytest.fixture
def fake_index(monkeypatch):
    idx = {
        "AAPL": {"exchange": "NAS", "currency": "USD", "kind": "stock"},
        "BRK/B": {"exchange": "NYS", "currency": "USD", "kind": "stock"},  # KIS 슬래시
        "SPY": {"exchange": "AMS", "currency": "USD", "kind": "etf_etn"},
    }
    monkeypatch.setattr(market_index, "_state",
                        {"by_ticker": idx, "fetched_at": "2026-05-22T00:00:00+00:00"})
    return idx


def test_exchange_of(fake_index):
    assert market_index.exchange_of("AAPL") == "NAS"
    assert market_index.exchange_of("BRK/B") == "NYS"
    assert market_index.exchange_of("SPY") == "AMS"
    assert market_index.exchange_of("005930") is None    # 삼성전자(국내)


def test_symbology_normalization(fake_index):
    # 점(위키)·대시(yfinance) 표기를 KIS 슬래시 키로 정규화 조회
    assert market_index.exchange_of("BRK.B") == "NYS"
    assert market_index.exchange_of("BRK-B") == "NYS"
    assert market_index.is_us("BRK.B") is True


def test_kis_ticker_of(fake_index):
    # 주문 PDNO는 KIS 슬래시 형식으로 정규화
    assert market_index.kis_ticker_of("BRK.B") == "BRK/B"
    assert market_index.kis_ticker_of("BRK-B") == "BRK/B"
    assert market_index.kis_ticker_of("AAPL") == "AAPL"
    assert market_index.kis_ticker_of("005930") == "005930"  # 국내는 그대로


def test_is_us_and_currency(fake_index):
    assert market_index.is_us("AAPL") is True
    assert market_index.is_us("005930") is False
    assert market_index.currency_of("AAPL") == "USD"
    assert market_index.currency_of("005930") == "KRW"


def test_market_group(fake_index):
    assert market_index.market_group_of("AAPL") == "US"
    assert market_index.market_group_of("005930") == "KRX"   # 6자리 국내


def test_market_group_unknown_ticker_raises(fake_index):
    # 인덱스에 없는 영문 티커 → 추측 금지, RoutingError
    with pytest.raises(market_index.RoutingError):
        market_index.market_group_of("TSLA")


def test_market_group_futures_uses_spec(fake_index):
    """선물은 계약 스펙(instrument_spec)이 SSOT — US 마스터·도메스틱 휴리스틱보다 우선.

    국내선물(KRW)=KRX(코스피200 진입 사이클 보존), 해외선물(USD)=US.
    회귀 가드: 한글 해외선물명('원유선물')이 폴백 KRX로 새던 잠재 오라우팅 차단."""
    assert market_index.market_group_of("코스피200선물") == "KRX"
    for sym in ("원유선물", "천연가스선물", "금선물", "나스닥선물", "비트코인선물"):
        assert market_index.market_group_of(sym) == "US", sym


def test_case_insensitive(fake_index):
    assert market_index.exchange_of("aapl") == "NAS"
