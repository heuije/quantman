"""gui_format 순수 표시 로직 — 주문 상태 한글화·진입/청산 판정·주문 예정 창.

Tkinter 미의존(순수 함수)이라 위젯 없이 검증한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import gui_format as gf


def test_status_ko():
    assert gf.order_status_ko("filled") == "체결"
    assert gf.order_status_ko("submitted") == "접수"
    assert gf.order_status_ko("partial") == "부분체결"
    assert gf.order_status_ko("cancelled") == "취소"
    assert gf.order_status_ko("rejected") == "거부"
    assert gf.order_status_ko("timeout") == "시간초과"
    # 미지 상태는 은폐 대신 원문 유지(디버깅 가능).
    assert gf.order_status_ko("weird") == "weird"
    assert gf.order_status_ko("") == ""


def test_kind_from_field_wins_over_reason():
    # 신규 기록 — 구조적 kind가 1차 진실원천(사유와 무관).
    assert gf.order_kind_ko({"kind": "진입", "reason": "당일청산"}) == "진입"
    assert gf.order_kind_ko({"kind": "청산", "reason": "매수신호"}) == "청산"


def test_kind_fallback_by_reason():
    # 구버전 기록 — kind 없음 → 사유 키워드로 폴백.
    assert gf.order_kind_ko({"reason": "매수신호"}) == "진입"
    assert gf.order_kind_ko({"reason": "숏진입"}) == "진입"
    assert gf.order_kind_ko({"reason": "당일청산"}) == "청산"
    assert gf.order_kind_ko({"reason": "보유기간"}) == "청산"
    assert gf.order_kind_ko({"reason": "익절"}) == "청산"
    assert gf.order_kind_ko({"reason": "손절 (intraday)"}) == "청산"
    assert gf.order_kind_ko({"reason": "전략 청산(삭제)"}) == "청산"
    # 사유 없음/불명 → 진입 기본.
    assert gf.order_kind_ko({}) == "진입"


def test_order_window():
    assert gf.order_window("next_open", "futures") == ("시가", 8, 55)
    assert gf.order_window("next_open", "stock") == ("시가", 8, 55)
    assert gf.order_window("close", "futures") == ("종가", 15, 40)
    assert gf.order_window("close", "stock") == ("종가", 15, 25)
    assert gf.order_window("typical", "futures") == ("종가", 15, 40)
    # 미지 fill → 시가창 기본(다음날 시가).
    assert gf.order_window("", "stock") == ("시가", 8, 55)
