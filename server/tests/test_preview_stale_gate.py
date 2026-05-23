"""S-05 회귀 — preview 어제 종가 stale 게이트.

서버 preview가 fetch 실패·거래정지·상폐 종목의 옛 종가로 매수 후보를 만들면
사용자가 잘못된 투명성 정보로 발주를 신뢰하게 된다. dataset의 마지막 데이터
일자가 시장의 직전 거래일과 일치하지 않으면 후보에서 차단.

검증:
1. _is_kr_symbol: 6자리 숫자=KR, 그 외=US
2. _data_freshness_ok: 최신 데이터=통과, N일 stale=차단 + 사유
3. 캘린더 비정상 시 fail-open (다른 신호가 잡음)
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


def _df_with_last_date(last_iso: str) -> pd.DataFrame:
    """dataset 형식 mini DataFrame — 마지막 인덱스만 last_iso."""
    idx = pd.to_datetime([last_iso])
    return pd.DataFrame({"Close": [100.0]}, index=idx)


def test_is_kr_symbol():
    from app.preview_engine import _is_kr_symbol
    assert _is_kr_symbol("005930") is True
    assert _is_kr_symbol("000660") is True
    assert _is_kr_symbol("NVDA") is False
    assert _is_kr_symbol("AAPL") is False
    assert _is_kr_symbol("BRK.B") is False
    assert _is_kr_symbol("12345") is False    # 5자리
    assert _is_kr_symbol("1234567") is False  # 7자리


def test_freshness_ok_when_last_matches_session(monkeypatch):
    """dataset last_date가 KR 캘린더 직전 거래일과 일치 → 통과."""
    from app import preview_engine

    today = date(2026, 5, 23)
    # mock: 2026-05-22가 KR 거래일이라고 가정
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: d == date(2026, 5, 22))

    dataset = {"005930": _df_with_last_date("2026-05-22")}
    ok, msg = preview_engine._data_freshness_ok(dataset, "005930", today)
    assert ok is True
    assert msg == ""


def test_freshness_blocks_stale_kr_symbol(monkeypatch):
    """dataset이 직전 거래일보다 7일 뒤처짐 → 차단 + 사유."""
    from app import preview_engine

    today = date(2026, 5, 23)
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: d == date(2026, 5, 22))

    dataset = {"005930": _df_with_last_date("2026-05-15")}
    ok, msg = preview_engine._data_freshness_ok(dataset, "005930", today)
    assert ok is False
    assert "stale" in msg
    assert "2026-05-15" in msg
    assert "2026-05-22" in msg
    assert "KR" in msg
    assert "7일 지연" in msg


def test_freshness_blocks_stale_us_symbol(monkeypatch):
    """미국 종목도 동일 게이트(US 캘린더 기준)."""
    from app import preview_engine

    today = date(2026, 5, 23)
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: (market == "US" and d == date(2026, 5, 22)))

    dataset = {"NVDA": _df_with_last_date("2026-05-10")}
    ok, msg = preview_engine._data_freshness_ok(dataset, "NVDA", today)
    assert ok is False
    assert "US" in msg
    assert "NVDA" not in msg or "stale" in msg


def test_freshness_no_data_returns_false():
    """dataset에 종목 자체가 없으면 차단."""
    from app import preview_engine
    ok, msg = preview_engine._data_freshness_ok({}, "005930", date(2026, 5, 23))
    assert ok is False
    assert "데이터 없음" in msg


def test_freshness_fail_open_when_calendar_broken(monkeypatch):
    """캘린더가 예외나 30일 역행으로도 거래일 못 찾으면 fail-open(통과)."""
    from app import preview_engine

    today = date(2026, 5, 23)
    # 모든 날짜가 휴장이라고 가정 → ref=None
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: False)

    dataset = {"005930": _df_with_last_date("2026-05-15")}
    ok, msg = preview_engine._data_freshness_ok(dataset, "005930", today)
    assert ok is True
    assert msg == ""


def test_freshness_data_ahead_of_reference_passes(monkeypatch):
    """dataset last가 ref와 같거나 더 최근이면 통과 (지나치게 엄격하지 않음)."""
    from app import preview_engine

    today = date(2026, 5, 23)
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: d == date(2026, 5, 22))

    # last_date가 ref와 같음
    dataset = {"005930": _df_with_last_date("2026-05-22")}
    ok, _ = preview_engine._data_freshness_ok(dataset, "005930", today)
    assert ok is True

    # last_date가 ref보다 더 최근(이론상 inintraday 갱신 케이스)
    dataset = {"005930": _df_with_last_date("2026-05-23")}
    ok, _ = preview_engine._data_freshness_ok(dataset, "005930", today)
    assert ok is True


def test_last_session_on_or_before_walks_back(monkeypatch):
    """주말 등 휴장일이면 직전 거래일까지 역행."""
    from app import preview_engine

    # 2026-05-23(토)·24(일) 휴장, 22(금) 거래일
    is_open = {date(2026, 5, 22)}
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: d in is_open)

    ref = preview_engine._last_session_on_or_before("KR", date(2026, 5, 24))
    assert ref == date(2026, 5, 22)


def test_last_session_on_or_before_returns_none_after_31_days(monkeypatch):
    """31일 이상 역행해도 거래일 못 찾으면 None (캘린더 비정상 신호)."""
    from app import preview_engine

    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: False)

    ref = preview_engine._last_session_on_or_before("KR", date(2026, 5, 23))
    assert ref is None
