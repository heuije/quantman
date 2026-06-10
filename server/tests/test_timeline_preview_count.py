"""타임라인 '매매 후보 결정' 시장별 카운트 — trading._preview_events.

회귀 가드(보고된 버그): sym.isdigit()로 국장/미장을 가르면 한글 선물 심볼
('코스피200선물')이 미장으로 오집계돼 국장 0건·미장 2건이 됐다. instrument_region
(SSOT)으로 코스피200선물=국장, 애플=미장으로 정확히 분리되는지 확인.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from datetime import datetime

from app.routers import trading

KST = ZoneInfo("Asia/Seoul")


def _preview(generated_at: str, symbols: list[str]) -> dict:
    """next_day_preview 한 전략·여러 후보를 담은 preview dict.

    Neon egress 절감 리팩토링(D1) 후 _preview_events는 전체 snaps 대신 윈도우 내
    최신 preview dict 1개를 받는다 — 여기서 그 dict를 직접 구성.
    """
    return {
        "generated_at": generated_at,
        "by_strategy": [{
            "candidates": [{"symbol": s} for s in symbols],
        }],
    }


def _done_summaries(symbols: list[str]) -> dict[str, str]:
    """주어진 후보로 _preview_events를 돌려 kind별 'done' 이벤트 summary 매핑."""
    now = datetime(2026, 6, 9, 19, 0, tzinfo=KST)         # 18:15·07:30 둘 다 지난 시각
    preview = _preview("2026-06-09T18:16:00+09:00", symbols)  # 18:15 직후 생성(=done 조건)
    events = trading._preview_events(
        preview, now, now - timedelta(hours=24), now + timedelta(hours=24))
    out = {}
    for e in events:
        if e["status"] == "done":
            out[e["kind"]] = e["summary"]
    return out


def test_futures_candidate_counts_as_krx_not_us():
    """코스피200선물 + 애플 → 국장 1건(선물) · 미장 1건(애플). (버그: 국장0·미장2)"""
    s = _done_summaries(["코스피200선물", "AAPL"])
    assert s["krx_preview"] == "1건", s     # 코스피200선물 = 국장
    assert s["us_preview"] == "1건", s      # 애플 = 미장 (코스피200선물 안 섞임)


def test_only_kr_futures_is_krx_zero_us():
    """코스피200선물 단독 → 국장 1건 · 미장 0건."""
    s = _done_summaries(["코스피200선물"])
    assert s["krx_preview"] == "1건", s
    assert s["us_preview"] == "후보 0건", s


def test_overseas_futures_counts_as_us():
    """해외선물(원유)은 미장. 국내주식(005930)은 국장."""
    s = _done_summaries(["005930", "원유선물"])
    assert s["krx_preview"] == "1건", s     # 005930 국내주식
    assert s["us_preview"] == "1건", s      # 원유선물 해외선물
