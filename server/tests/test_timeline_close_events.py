"""θ/N2 — 타임라인 종가청산 마일스톤 + kind-aware 매칭 + warning 상태.

2026-06-12~13 라이브 결함의 관측 축:
  · 선물 종가청산(15:40)이 타임라인에 없어 성공/실패를 로그를 파야 알 수 있었다.
  · 발주-but-체결미확인 사이클이 "✓ 0건" 녹색으로 보였다(미장 GOOG 261주 방치).
  · kind 무관 매칭이라 정산/종가청산/state_sync push가 슬롯을 교차 오표시할 수 있었다.

    cd platform/server && python -m pytest tests/test_timeline_close_events.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.routers import trading

KST = ZoneInfo("Asia/Seoul")

# 2026-06-08(월) — 평일. _is_session_day_safe는 캘린더 예외 시 True fallback이라
# 테스트 환경에서 평일이면 영업일로 취급된다.
DAY = datetime(2026, 6, 8, tzinfo=KST)


def _snap(hh: int, mm: int, kind: str, market: str = "KRX", **cs) -> trading.SnapLite:
    received = DAY.replace(hour=hh, minute=mm)
    summary = {"today": DAY.date().isoformat(), "market": market, "kind": kind, **cs}
    return trading.SnapLite(received_at=received, payload={"cycle_summary": summary})


def _krx(snaps, now_hh=16, now_mm=30):
    now = DAY.replace(hour=now_hh, minute=now_mm)
    events = trading._krx_events(snaps, [], now,
                                 DAY.replace(hour=0, minute=0),
                                 DAY.replace(hour=23, minute=59))
    return {e["kind"]: e for e in events}


# ── 슬롯 존재·시각 ──────────────────────────────────────────────────────────

def test_close_milestones_exist_with_correct_times():
    """종가청산(주식 15:25·선물 15:40) 슬롯이 노출되고 정산은 15:50(θ 재배치)."""
    ev = _krx([], now_hh=8, now_mm=0)     # 모두 미래 → scheduled
    assert ev["krx_close_stock"]["at"].endswith("15:25:00+09:00")
    assert ev["krx_close_futures"]["at"].endswith("15:40:00+09:00")
    assert ev["krx_settlement"]["at"].endswith("15:50:00+09:00"), \
        "정산은 선물 종가창(~15:45) 이후여야(θ)"


# ── kind-aware 매칭 ────────────────────────────────────────────────────────

def test_day_trade_close_push_matches_close_slot_not_settlement():
    """15:41 선물 종가청산 push는 krx_close_futures만 done — 정산 슬롯을 가장하지 않는다."""
    snaps = [_snap(15, 41, "day_trade_close", instrument_class="futures",
                   n_sold=1, n_pending_unresolved=0)]
    ev = _krx(snaps)
    assert ev["krx_close_futures"]["status"] == "done"
    assert ev["krx_close_futures"]["summary"] == "1건 매도"
    assert ev["krx_settlement"]["status"] == "missed", \
        "정산 push 없이 종가청산 push가 정산을 done으로 오표시(kind 무관 매칭)"


def test_instrument_class_separates_stock_and_futures_slots():
    """주식(15:26)·선물(15:41) push가 각자 슬롯에만 매칭."""
    snaps = [
        _snap(15, 26, "day_trade_close", instrument_class="stock",
              n_sold=2, n_pending_unresolved=0),
        _snap(15, 41, "day_trade_close", instrument_class="futures",
              n_sold=1, n_pending_unresolved=0),
    ]
    ev = _krx(snaps)
    assert ev["krx_close_stock"]["summary"] == "2건 매도"
    assert ev["krx_close_futures"]["summary"] == "1건 매도"


def test_state_sync_push_does_not_mark_cycle_done():
    """08:56 state_sync(잡음 push)가 krx_cycle을 done으로 가장하지 못한다."""
    snaps = [_snap(8, 56, "state_sync", market="ALL")]
    ev = _krx(snaps)
    assert ev["krx_cycle"]["status"] == "missed"


def test_legacy_all_market_close_push_still_matches():
    """구버전 로컬앱(day_trade_close market='ALL'·class 부재)도 윈도우 시각으로 매칭
    — 롤링 업그레이드 중 거짓 '누락'을 만들지 않는다."""
    snaps = [_snap(15, 41, "day_trade_close", market="ALL", n_sold=1)]
    ev = _krx(snaps)
    assert ev["krx_close_futures"]["status"] == "done"


def test_legacy_1535_settlement_matches_via_same_day_fallback():
    """구 로컬앱 정산(15:35 push)은 새 15:50 슬롯 윈도우(15:48~) 밖이지만
    같은 거래일 fallback(G-8)으로 done — 서버 먼저 배포돼도 거짓 누락 없음."""
    snaps = [_snap(15, 35, "post_close_settlement", n_pending_unresolved=0)]
    ev = _krx(snaps)
    assert ev["krx_settlement"]["status"] == "done"


# ── warning(N2): 발주-but-체결미확인 ───────────────────────────────────────

def test_unresolved_pending_close_shows_warning_not_green():
    """종가청산이 발주했지만 체결확인 실패(n_pending_unresolved>0) → ⚠ warning."""
    snaps = [_snap(15, 41, "day_trade_close", instrument_class="futures",
                   n_sold=0, n_pending_unresolved=1)]
    ev = _krx(snaps)
    e = ev["krx_close_futures"]
    assert e["status"] == "warning", f"거짓 녹색: {e}"
    assert "미체결 추적 1건" in e["summary"]


def test_unresolved_pending_settlement_shows_warning():
    """정산 후에도 미체결 잔존 → ⚠ (장 마감 뒤 잔존은 전부 비정상)."""
    snaps = [_snap(15, 50, "post_close_settlement", n_pending_unresolved=2)]
    ev = _krx(snaps)
    assert ev["krx_settlement"]["status"] == "warning"
    assert "미체결 추적 2건" in ev["krx_settlement"]["summary"]


def test_morning_cycle_with_working_orders_stays_done():
    """아침 cycle의 미체결 잔존은 정상(DAY 지정가 장중 체결 가능) — 경고 안 함."""
    snaps = [_snap(8, 56, "cycle", n_bought=1, n_pending_unresolved=1)]
    ev = _krx(snaps)
    assert ev["krx_cycle"]["status"] == "done"
