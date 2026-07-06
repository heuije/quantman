"""공매도 서빙 — short_balance_kr 볼륨 우선 + 라이브 KRX(MDC) 폴백 (무네트워크·monkeypatch).

옛 매 요청 KRX MDC OTP→CSV 라이브 크롤(in-memory lru·재배포마다 콜드)을 데이터엔진 피드 볼륨
서빙으로 대체. 여기선 ① 피드 우선(라이브 미호출) ② 볼륨 미커버 시 라이브 폴백 ③ 웹 계약
(newest-first·date/bal_qty/bal_amt/bal_ratio)을 고정한다.
"""
import pandas as pd

from app import krdata


def _feed_df():
    # index=거래일(오래된→최신), 볼륨 저장형(bal_qty/bal_amt/bal_ratio)
    return pd.DataFrame({"bal_qty": [100.0, 110.0], "bal_amt": [5_000_000.0, 5_500_000.0],
                         "bal_ratio": [0.50, 0.55]},
                        index=pd.to_datetime(["2026-07-02", "2026-07-03"]))


def _boom(*a, **k):
    raise AssertionError("라이브 KRX 크롤됨 — 피드 우선이어야 함")


def test_shorting_feed_first(monkeypatch):
    from quant_core.data.feeds import short_balance_kr
    monkeypatch.setattr(short_balance_kr, "load_short_balance", lambda code: _feed_df())
    monkeypatch.setattr(krdata, "_shorting", _boom)          # 라이브 불리면 실패 → 피드만 쓰였음 증명
    krdata._shorting_from_feed.cache_clear()
    out = krdata.shorting("005930")
    assert out and out[0]["date"] == "2026-07-03"            # newest-first(웹 shorting[0]=최신)
    assert out[0]["bal_qty"] == 110 and out[0]["bal_ratio"] == 0.55
    assert set(out[0]) == {"date", "bal_qty", "bal_amt", "bal_ratio"}


def test_shorting_fallback_to_live(monkeypatch):
    from quant_core.data.feeds import short_balance_kr
    monkeypatch.setattr(short_balance_kr, "load_short_balance", lambda code: pd.DataFrame())  # 볼륨 미커버
    monkeypatch.setattr(krdata, "_shorting",
                        lambda code, day: [{"date": "2026/07/03", "bal_qty": 9,
                                            "bal_amt": 1, "bal_ratio": 0.1}])
    krdata._shorting_from_feed.cache_clear()
    out = krdata.shorting("999999")
    assert out and out[0]["bal_qty"] == 9                    # 라이브 폴백(미커버·신규상장)


def test_shorting_empty_when_neither(monkeypatch):
    from quant_core.data.feeds import short_balance_kr
    monkeypatch.setattr(short_balance_kr, "load_short_balance", lambda code: pd.DataFrame())
    monkeypatch.setattr(krdata, "_shorting", lambda code, day: [])   # 폴백도 빈결과
    krdata._shorting_from_feed.cache_clear()
    assert krdata.shorting("999999") == []                  # 가짜 데이터 금지·빈 결과
