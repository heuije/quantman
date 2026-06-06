"""KOSPI200 선물 최근월물 자동해석 — 마스터 파서 단위검증(네트워크 없음).

parse_front_month이 KIS 지수선물 마스터 라인에서 정규선물(미니·옵션 제외)의 만기 미경과 최근
월물 단축코드를 고르는지. 분기 롤(만기=2번째 목요일) 경계 동작 포함.

    cd platform/server && python -m pytest tests/test_kis_futures_master.py -q
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.kis_futures_master import _second_thursday, parse_front_month

# 실제 마스터 형식(cp949 decode 후) 표본
_MASTER = "\n".join([
    "1A01606   KR4A01660005F 202606                  00000.0012001     KOSPI200",
    "1A01609   KR4A01690002F 202609                  00000.0022001     KOSPI200",
    "1A01612   KR4A016C0004F 202612                  00000.0032001     KOSPI200",
    "BA05606   KR4A05660001미니F 202606              00000.0012001     KOSPI200",   # 미니 → 제외
    "2A02606   KR4A02660003C 202606  300.0           00000.0012001     KOSPI200",   # 옵션(C) → 제외
])


def test_second_thursday():
    assert _second_thursday(2026, 6) == date(2026, 6, 11)   # 2026-06 2번째 목요일
    assert _second_thursday(2026, 9) == date(2026, 9, 10)


def test_front_month_before_expiry():
    # 6/7 < 6/11 만기 → 6월물(A01606)이 최근월물
    assert parse_front_month(_MASTER, date(2026, 6, 7)) == "A01606"


def test_front_month_rolls_after_expiry():
    # 6/12 > 6/11 만기 → 6월물 제외, 9월물(A01609)로 자동 롤
    assert parse_front_month(_MASTER, date(2026, 6, 12)) == "A01609"


def test_front_month_excludes_mini_and_options():
    # 정규선물('1')만 — 미니('B')·옵션('2', C/P)은 안 골라야 함
    code = parse_front_month(_MASTER, date(2026, 6, 7))
    assert code == "A01606" and not code.startswith(("A05", "A02"))


def test_front_month_none_when_all_expired():
    assert parse_front_month(_MASTER, date(2027, 1, 1)) is None
