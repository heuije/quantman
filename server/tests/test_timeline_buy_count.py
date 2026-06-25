"""타임라인 '자동매매 시작 N건 매수'가 그날 실제 매수(체결+체결대기)를 반영하는지.

시가매수 주문은 08:55 사이클 시점엔 09:00 개장 단일가 체결 전이라 n_bought(인사이클
체결)에 안 잡혀 '2후보 1매수'로 과소표시됐다(2026-06-25 발견·A안 수정). 로컬이
n_buy_placed(=n_bought + 매수 체결대기·side=buy만)를 기록 → 서버 _summarize_cycle가
표시. 구버전 스냅샷(n_buy_placed 부재)은 n_bought 폴백(기존 동작 보존).

    cd platform/server && python -m pytest tests/test_timeline_buy_count.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.routers import trading

KST = ZoneInfo("Asia/Seoul")


def _snap(**cs) -> trading.SnapLite:
    return trading.SnapLite(
        received_at=datetime(2026, 6, 25, 8, 56, tzinfo=KST),
        payload={"cycle_summary": cs})


def test_buy_count_uses_n_buy_placed():
    # 선물 인사이클 체결 1 + 주식 체결대기 1 → 발주 완료 매수 2 (06-25 케이스)
    assert trading._summarize_cycle(_snap(n_bought=1, n_buy_placed=2, n_sold=0)) == "2건 매수"


def test_buy_count_fallback_to_n_bought_for_old_snapshot():
    # 구버전 스냅샷(n_buy_placed 부재) → n_bought 폴백(기존 동작 불변)
    assert trading._summarize_cycle(_snap(n_bought=1, n_sold=0)) == "1건 매수"


def test_buy_and_sell_both_shown():
    assert trading._summarize_cycle(_snap(n_bought=2, n_buy_placed=2, n_sold=3)) == "2건 매수 · 3건 매도"


def test_zero_when_none():
    assert trading._summarize_cycle(_snap(n_bought=0, n_sold=0)) == "0건"
