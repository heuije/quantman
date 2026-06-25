"""타임라인 '자동매매 시작'이 그날 실제 진입(체결+체결대기)을 side별로 반영하는지.

시가 진입 주문은 08:55 사이클 시점엔 09:00 개장 단일가 체결 전이라 n_bought/n_sold(인사이클
체결)에 안 잡혀 과소표시됐다(2026-06-25 발견·A안 수정): 매수='2후보 1매수', 숏 진입(매도)은
아예 비표시. 로컬이 side별 발주 완료분 n_buy_placed(매수=롱 진입)·n_sell_placed(매도=숏
진입·양방향 선물)를 기록 → 서버 _summarize_cycle가 표시. 구버전 스냅샷(부재)은
n_bought/n_sold 폴백(기존 동작 보존).

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


def test_short_entry_uses_n_sell_placed():
    # 시가 숏 진입(매도)은 09:00 체결 전이라 n_sold=0 → n_sell_placed로 표면화(이전 비표시)
    assert trading._summarize_cycle(_snap(n_sold=0, n_sell_placed=1)) == "1건 매도"


def test_long_and_short_both_shown():
    # 롱 진입(주식 매수) + 숏 진입(선물 매도) 동시 → 둘 다 표시 (양방향 선물 케이스)
    assert trading._summarize_cycle(_snap(n_buy_placed=1, n_sell_placed=1)) == "1건 매수 · 1건 매도"


def test_sell_count_fallback_to_n_sold_for_old_snapshot():
    # 구버전 스냅샷(n_sell_placed 부재) → n_sold 폴백(기존 동작 불변)
    assert trading._summarize_cycle(_snap(n_sold=2)) == "2건 매도"
