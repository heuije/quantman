"""타임라인 preview 슬롯 판정 — "오늘 신선분" 기준 (trading._preview_events).

라이브 실증 결함(2026-06-12): 07:30 글로벌 refresh가 FRED 장애·배포 재시작으로
오래 걸려 preview가 08:10에야 생성됐다. 옛 판정(`latest_gen >= sched - 2min`)은
생성 완료 전까지 "missed"를 내 GUI/웹에 "국장 매매 후보 결정: 누락"이 표시됐다.
cron은 backoff([5,15,30,60,120]분) 재시도로 자가회복하므로 그 구간의 "누락"은 거짓.

새 판정 — 슬롯 날(KST) 신선분 여부:
  · 슬롯 날(또는 이후) 생성분 존재 → done (슬롯시각 이전/이후 생성 무관 —
    preview는 전일 마감 데이터 기반이라 같은 날 생성이면 신선. 이후 날 생성은
    supersede로 done 유지).
  · 슬롯시각 경과 ~ 하드 데드라인(KRX 09:00 개장 / US 23:30 KST 개장) 전 →
    status "scheduled" 재사용 + summary "갱신중" (재시도 진행 중일 수 있음).
  · 데드라인 경과에도 그 날 생성분 없음 → 진짜 missed.
  · 슬롯시각 이전 → 기존 scheduled(빈 summary) 보존.
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

# 사건 당일(금요일). _preview_events는 캘린더 무관하게 매일 슬롯을 emit한다.
TODAY = datetime(2026, 6, 12, tzinfo=KST).date()
KRX_SLOT = datetime(2026, 6, 12, 7, 30, tzinfo=KST)
US_SLOT = datetime(2026, 6, 12, 18, 15, tzinfo=KST)
YDAY_US_SLOT = datetime(2026, 6, 11, 18, 15, tzinfo=KST)


def _preview(generated_at: str) -> dict:
    """국장 1건(005930)·미장 1건(AAPL) 후보를 담은 next_day_preview dict."""
    return {
        "generated_at": generated_at,
        "by_strategy": [{
            "candidates": [{"symbol": "005930"}, {"symbol": "AAPL"}],
        }],
    }


def _events(preview: dict | None, now: datetime) -> list[dict]:
    return trading._preview_events(
        preview, now, now - timedelta(hours=24), now + timedelta(hours=24))


def _slot_event(events: list[dict], kind: str, at: datetime) -> dict:
    matches = [e for e in events if e["kind"] == kind and e["at"] == at.isoformat()]
    assert len(matches) == 1, (kind, at.isoformat(), events)
    return matches[0]


# ── ① 늦은 생성(슬롯 후) — 자가회복은 done ─────────────────────────────────────

def test_krx_late_generation_after_slot_is_done():
    """오늘 08:10 생성(07:30 슬롯 지연 자가회복) → done '1건'."""
    now = datetime(2026, 6, 12, 8, 30, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-12T08:10:00+09:00"), now),
                     "krx_preview", KRX_SLOT)
    assert ev["status"] == "done", ev
    assert ev["summary"] == "1건", ev


# ── ② 오늘 미생성 + 데드라인 전 — "갱신중"(누락 아님) ──────────────────────────

def test_krx_no_fresh_generation_before_deadline_is_refreshing():
    """어제 생성뿐 + 현재 08:00(데드라인 09:00 전) → scheduled + '갱신중'.

    2026-06-12 거짓 '누락'의 직접 재현 — cron backoff 재시도가 진행 중인 구간.
    """
    now = datetime(2026, 6, 12, 8, 0, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-11T18:16:00+09:00"), now),
                     "krx_preview", KRX_SLOT)
    assert ev["status"] == "scheduled", ev
    assert ev["summary"] == "갱신중", ev
    assert ev["detail"], ev          # hover 설명 존재


def test_us_no_fresh_generation_before_deadline_is_refreshing():
    """미장 슬롯도 동일 판정 — 어제 생성뿐 + 19:00(데드라인 23:30 전) → 갱신중."""
    now = datetime(2026, 6, 12, 19, 0, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-11T18:16:00+09:00"), now),
                     "us_preview", US_SLOT)
    assert ev["status"] == "scheduled", ev
    assert ev["summary"] == "갱신중", ev


# ── ③ 데드라인 경과에도 오늘 미생성 — 진짜 missed ──────────────────────────────

def test_krx_no_fresh_generation_after_deadline_is_missed():
    """어제 생성뿐 + 현재 09:30(국장 개장 후) → missed(빈 summary → 소비자 '누락')."""
    now = datetime(2026, 6, 12, 9, 30, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-11T18:16:00+09:00"), now),
                     "krx_preview", KRX_SLOT)
    assert ev["status"] == "missed", ev
    assert ev["summary"] == "", ev   # gui.py `or "누락"` / web `|| "누락"` 경로
    assert "서버" in ev["detail"], ev


def test_krx_deadline_boundary_exactly_0900_is_missed():
    """현재가 정확히 09:00이면 이미 개장 — missed로 확정(now < deadline 경계)."""
    now = datetime(2026, 6, 12, 9, 0, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-11T18:16:00+09:00"), now),
                     "krx_preview", KRX_SLOT)
    assert ev["status"] == "missed", ev


def test_us_no_fresh_generation_after_deadline_is_missed():
    """어제 생성뿐 + 현재 23:45(미장 개장 후) → missed."""
    now = datetime(2026, 6, 12, 23, 45, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-11T18:16:00+09:00"), now),
                     "us_preview", US_SLOT)
    assert ev["status"] == "missed", ev


# ── ④ 슬롯시각 이전 — 기존 scheduled(예정) 보존 ────────────────────────────────

def test_before_slot_time_is_plain_scheduled():
    """06:00 조회 → 오늘 07:30 슬롯은 scheduled, summary 비어 있음('갱신중' 아님)."""
    now = datetime(2026, 6, 12, 6, 0, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-11T18:16:00+09:00"), now),
                     "krx_preview", KRX_SLOT)
    assert ev["status"] == "scheduled", ev
    assert ev["summary"] == "", ev   # 소비자는 상대시각("1h 30m 후")을 그림


# ── ⑤ 같은 날 슬롯 전 생성 — done ─────────────────────────────────────────────

def test_krx_same_day_generation_before_slot_is_done():
    """오늘 07:00 생성(슬롯 07:30 전) → done '1건'. 같은 날 생성이면 신선."""
    now = datetime(2026, 6, 12, 8, 0, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-12T07:00:00+09:00"), now),
                     "krx_preview", KRX_SLOT)
    assert ev["status"] == "done", ev
    assert ev["summary"] == "1건", ev


def test_us_same_day_morning_generation_is_done():
    """오늘 08:10 생성(07:30 cron 산출)뿐이어도 미장 18:15 슬롯은 done — 같은 날 신선."""
    now = datetime(2026, 6, 12, 19, 0, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-12T08:10:00+09:00"), now),
                     "us_preview", US_SLOT)
    assert ev["status"] == "done", ev
    assert ev["summary"] == "1건", ev


# ── 과거 슬롯 supersede — 이후 날 생성도 done 유지(회귀 가드) ───────────────────

def test_yesterday_slot_superseded_by_today_generation_stays_done():
    """어제 18:15 슬롯을 오늘 아침 조회, 최신 생성=오늘 → 어제 슬롯 done 유지.

    '슬롯 날과 정확히 같은 날만 done'으로 좁히면 과거 슬롯이 거짓 missed가 된다 —
    최신 preview 1개만 보는 구조에서 이후 날 생성은 supersede로 done 처리.
    """
    now = datetime(2026, 6, 12, 8, 30, tzinfo=KST)
    ev = _slot_event(_events(_preview("2026-06-12T08:10:00+09:00"), now),
                     "us_preview", YDAY_US_SLOT)
    assert ev["status"] == "done", ev
