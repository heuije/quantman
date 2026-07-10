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
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


# ── KST 05:00 cutoff 회귀 (2026-05-28 사용자 발견 사건) ─────────────────
#
# v0.9.5 catch-up에서 미장 매수 0건 사고. 원인: preview_engine이 KST 자정 cutoff
# 만 사용해 KST 0:00~5:00 사이에는 어제 KST(5/27)의 US 거래를 "이미 마감"으로
# 오인. 실제로는 EDT 16:00 = KST 5/28 05:00에 마감 → KST 0:08 시점엔 5/27 US가
# 장중. dataset의 5/26(=그제) 데이터를 1일 stale로 잘못 판단해 모든 US 종목이
# 후보에서 제외. 매수 0건 영구 차단.

def test_us_freshness_kst_before_5am_uses_day_before_yesterday(monkeypatch):
    """KST 0:00~5:00 — 어제 KST의 US 거래는 EDT 16:00(KST 익일 5시)에 마감.
    그 이전에는 그제(N-2)가 직전 마감 거래일이어야 함."""
    from app import preview_engine

    today = date(2026, 5, 28)
    # 5/26(화), 5/27(수) US 거래일, 5/28(목) 진행 중
    us_sessions = {date(2026, 5, 26), date(2026, 5, 27), date(2026, 5, 28)}
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: market == "US" and d in us_sessions)

    # KST 00:08 = US 장중 5/27. 5/27 종가 미확정 → ref = 5/26.
    now_kst = datetime(2026, 5, 28, 0, 8, tzinfo=ZoneInfo("Asia/Seoul"))
    dataset = {"AAPL": _df_with_last_date("2026-05-26")}
    ok, msg = preview_engine._data_freshness_ok(
        dataset, "AAPL", today, now_kst=now_kst)
    assert ok is True, f"KST 5am 이전 — 5/26 데이터는 stale 아님. msg={msg}"


def test_us_freshness_kst_after_5am_uses_yesterday(monkeypatch):
    """KST 05:00 이후 — 어제 KST의 US 거래는 마감 끝남 (EDT 16:00 = KST 5시).
    어제(N-1)가 직전 마감 거래일이어야 함."""
    from app import preview_engine

    today = date(2026, 5, 28)
    us_sessions = {date(2026, 5, 26), date(2026, 5, 27)}
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: market == "US" and d in us_sessions)

    # KST 06:00 5/28 = 5/27 미장 마감 후 1시간. ref = 5/27.
    now_kst = datetime(2026, 5, 28, 6, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    dataset = {"AAPL": _df_with_last_date("2026-05-26")}
    ok, msg = preview_engine._data_freshness_ok(
        dataset, "AAPL", today, now_kst=now_kst)
    assert ok is False, "KST 5am 이후 — 5/26 데이터는 1일 stale"
    assert "1일 지연" in msg


def test_kr_freshness_unaffected_by_us_cutoff(monkeypatch):
    """KR 시장은 US cutoff 로직과 무관 — 본 fix가 KR 동작 회귀시키지 않음 확인."""
    from app import preview_engine

    today = date(2026, 5, 28)
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: market == "KR" and d == date(2026, 5, 27))

    # KST 0시 (US cutoff 이전) 호출이어도 KR은 today 기준 그대로.
    now_kst = datetime(2026, 5, 28, 0, 8, tzinfo=ZoneInfo("Asia/Seoul"))
    dataset = {"005930": _df_with_last_date("2026-05-27")}
    ok, _ = preview_engine._data_freshness_ok(
        dataset, "005930", today, now_kst=now_kst)
    assert ok is True


# ── KR 19:00 수집 cutoff 회귀 (2026-06-10 무발주 인시던트 D4-2) ─────────────
#
# KR 종가 수집은 18:15 cron — 그 전에 preview가 rebuild되면(재배포 boot refresh,
# 07:30 글로벌 cron의 trigger 등) "오늘 종가"는 아직 존재할 수 없는데 ref_anchor가
# today에 고정돼 있어, 가장 신선한 상태인 전일 종가를 "1일 지연 stale"로 오판 →
# KR 후보 전멸·무발주. 실측: 2026-06-10 04:27 KST 생성 preview가 코스피200선물을
# "데이터 stale (last=2026-06-09, 직전 KR 거래일 2026-06-10, 1일 지연)"으로 skip.
# docs/incidents/2026-06-10-autotrading-week-retrospective.md 참조.

def test_kr_freshness_morning_rebuild_accepts_prev_close(monkeypatch):
    """KST 19:00 이전(예: 새벽 boot rebuild) — 전일 종가가 신선도 기준이어야 함."""
    from app import preview_engine

    today = date(2026, 6, 10)
    kr_sessions = {date(2026, 6, 9), date(2026, 6, 10)}
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: market == "KR" and d in kr_sessions)

    # 04:27 KST — 06-10 종가는 18:15에야 수집 가능. last=06-09가 최신 상태.
    now_kst = datetime(2026, 6, 10, 4, 27, tzinfo=ZoneInfo("Asia/Seoul"))
    dataset = {"005930": _df_with_last_date("2026-06-09")}
    ok, msg = preview_engine._data_freshness_ok(
        dataset, "005930", today, now_kst=now_kst)
    assert ok is True, f"19:00 이전 — 전일 종가는 stale 아님. msg={msg}"


def test_kr_freshness_evening_requires_today_close(monkeypatch):
    """KST 19:00 이후 — 오늘 종가(18:15 수집)가 있어야 신선. 전일 종가는 1일 stale."""
    from app import preview_engine

    today = date(2026, 6, 10)
    kr_sessions = {date(2026, 6, 9), date(2026, 6, 10)}
    monkeypatch.setattr(
        preview_engine._mc, "is_session_day",
        lambda market, d: market == "KR" and d in kr_sessions)

    now_kst = datetime(2026, 6, 10, 19, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    dataset = {"005930": _df_with_last_date("2026-06-09")}
    ok, msg = preview_engine._data_freshness_ok(
        dataset, "005930", today, now_kst=now_kst)
    assert ok is False, "19:00 이후 — 전일 종가는 1일 stale"
    assert "1일 지연" in msg

    dataset = {"005930": _df_with_last_date("2026-06-10")}
    ok, _ = preview_engine._data_freshness_ok(
        dataset, "005930", today, now_kst=now_kst)
    assert ok is True


# ── R-1 (2026-07-10 리뷰) — 신호 참조 심볼 신선도 게이트 ─────────────────────────
def test_signal_ref_stale_blocks(monkeypatch):
    """신호 참조(S&P500)가 stale이면 (symbol, 사유) 반환 — 거래심볼이 fresh여도 차단."""
    from app import preview_engine
    now = datetime(2026, 7, 10, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(preview_engine._mc, "is_session_day",
                        lambda market, d: d.weekday() < 5)
    dataset = {"코스피200선물": _df_with_last_date("2026-07-09"),
               "S&P500": _df_with_last_date("2026-07-06")}   # 직전 US 거래일(7/9) 대비 지연
    hit = preview_engine._stale_signal_ref(
        dataset, {"S&P500", "코스피200선물"}, ["코스피200선물"], now)
    assert hit is not None
    assert hit[0] == "S&P500" and "stale" in hit[1]


def test_signal_ref_fresh_passes(monkeypatch):
    from app import preview_engine
    now = datetime(2026, 7, 10, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(preview_engine._mc, "is_session_day",
                        lambda market, d: d.weekday() < 5)
    dataset = {"코스피200선물": _df_with_last_date("2026-07-09"),
               "S&P500": _df_with_last_date("2026-07-09")}
    assert preview_engine._stale_signal_ref(
        dataset, {"S&P500", "코스피200선물"}, ["코스피200선물"], now) is None


def test_signal_ref_non_asset_category_not_gated(monkeypatch):
    """주간성 매크로(심리 카테고리 — 미결제약정 등)는 게이트 미적용(과차단 방지)."""
    from app import preview_engine
    now = datetime(2026, 7, 10, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(preview_engine._mc, "is_session_day",
                        lambda market, d: d.weekday() < 5)
    dataset = {"코스피200선물미결제약정": _df_with_last_date("2026-07-01")}
    assert preview_engine._stale_signal_ref(
        dataset, {"코스피200선물미결제약정"}, [], now) is None


def test_signal_ref_missing_from_dataset_blocks(monkeypatch):
    """참조 자산이 dataset에 아예 없음 → '데이터 없음'으로 차단(조용한 무신호 방지)."""
    from app import preview_engine
    now = datetime(2026, 7, 10, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
    monkeypatch.setattr(preview_engine._mc, "is_session_day",
                        lambda market, d: d.weekday() < 5)
    hit = preview_engine._stale_signal_ref(dataset={}, needed={"S&P500"},
                                           universe_syms=[], now_kst=now)
    assert hit is not None and hit[0] == "S&P500" and "없음" in hit[1]
