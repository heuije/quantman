"""'자동매매 시작' 매수/매도 카운트가 *이 거래일 발주분*만 세는지 — orphan 과대표시 가드.

2026-06-26 라이브 회귀: 후보 2건인데 타임라인 '3건 매수'로 표시. 근본=n_buy_placed가
시장의 모든 매수 pending을 세서, LS 체결인지 실패로 며칠째 남은 orphan 주문(06-22 발주
18756, 7일 GC 전까지 잔존)이 당일 매수에 합산됨. _count_today_pending이 submitted_ts ≥
당일 0시 KST로 제한해 차단한다.

    cd platform/local && python -m pytest tests/test_pending_today_count.py -q
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from localapp.trader import _count_today_pending

KST = ZoneInfo("Asia/Seoul")


def _ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KST).timestamp()


def _buy(symbol, ts):
    return {"side": "buy", "symbol": symbol, "submitted_ts": ts}


def test_excludes_multiday_orphan():
    # 실제 06-26 케이스: 06-22 orphan + 당일 발주 → 당일분 1건만
    today_start = _ts(2026, 6, 26)
    pending = {
        "18756": _buy("000660", _ts(2026, 6, 22, 12, 32)),  # 다일 orphan
        "112": _buy("000660", _ts(2026, 6, 26, 8, 55)),     # 당일 발주
    }
    assert _count_today_pending(pending, "buy", "KRX", today_start) == 1


def test_missing_submitted_ts_excluded():
    today_start = _ts(2026, 6, 26)
    pending = {"x": {"side": "buy", "symbol": "000660"}}  # submitted_ts 없음 → 0 → 제외
    assert _count_today_pending(pending, "buy", "KRX", today_start) == 0


def test_side_and_market_filter():
    today_start = _ts(2026, 6, 26)
    pending = {
        "krx_buy": _buy("000660", _ts(2026, 6, 26, 9)),
        "krx_sell": {"side": "sell", "symbol": "000660", "submitted_ts": _ts(2026, 6, 26, 9)},
    }
    assert _count_today_pending(pending, "buy", "KRX", today_start) == 1
    assert _count_today_pending(pending, "sell", "KRX", today_start) == 1
    assert _count_today_pending(pending, "buy", "US", today_start) == 0  # 000660은 KRX


def test_midnight_boundary_included():
    today_start = _ts(2026, 6, 26)
    pending = {"m": _buy("000660", today_start)}  # 정확히 당일 0시 → 포함(>=)
    assert _count_today_pending(pending, "buy", "KRX", today_start) == 1
