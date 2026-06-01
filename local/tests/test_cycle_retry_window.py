"""REV-① 회귀 — cycle 재시도 backoff가 US 예약 접수창을 넘기지 않는가.

예약(US) cycle은 스케줄러가 개장-20분에 시작하고, KIS 예약주문 접수창은 개장-10분에
닫힌다(약 10분=600s 윈도우). 기존엔 reserved 무관하게 (60,300,900)s backoff라, 2회
실패하면 3번째 재시도가 접수창을 넘겨 예약발주가 통째로 누락될 수 있었다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def test_reserved_backoffs_fit_reception_window():
    from localapp.runner import _cycle_backoffs
    krx = _cycle_backoffs(reserved=False)
    resv = _cycle_backoffs(reserved=True)

    assert resv and krx, "둘 다 최소 1회 이상 재시도해야"
    # 누적 backoff가 접수창(개장-20→-10분=600s)에 cycle 실행시간 여유까지 두고 들어가야.
    assert sum(resv) <= 120, f"예약 누적 backoff {sum(resv)}s — 접수창(600s) 마진 위협"
    assert sum(resv) < sum(krx), "예약 backoff는 KRX보다 짧아야(접수창 제약)"


def test_krx_backoffs_unchanged():
    """KRX는 정규장 6.5h 윈도우라 기존 (60,300,900) 유지."""
    from localapp.runner import _cycle_backoffs
    assert _cycle_backoffs(reserved=False) == (60, 300, 900)
