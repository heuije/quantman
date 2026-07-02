"""futures_roll.build_continuous — 만기물 패널→연속물 stitch 검증(합성 패널·손계산).

패널: 근월 C1(202001, D1~D4·만기 D4) + 차월 C2(202002, D1~D6). 롤 방식별 전환일이
서로 다르게 설계됨 — oi_cross(D3)≠volume_cross(D4)≠at_expiry(D5)≠days_before_2(D2).
"""
import numpy as np
import pandas as pd
import pytest

from quant_core.data.futures_roll import build_continuous

D = pd.bdate_range("2020-01-01", periods=6)   # D1..D6


def _bars(contract, dates, closes, vols, ois):
    return pd.DataFrame({
        "contract": contract, "Open": closes,
        "High": [c + 1 for c in closes], "Low": [c - 1 for c in closes],
        "Close": closes, "Settle": closes, "Volume": vols, "OI": ois,
    }, index=dates)


def _panel():
    c1 = _bars("202001", D[:4], [100, 101, 102, 103], [1000, 900, 800, 700], [5000, 4000, 3000, 0])
    c2 = _bars("202002", D[:6], [110, 111, 112, 113, 114, 115],
               [100, 200, 750, 800, 900, 900], [1000, 2000, 3500, 4000, 5000, 5000])
    return pd.concat([c1, c2]).sort_index()


def test_at_expiry_holds_front_through_expiry():
    out = build_continuous(_panel(), "at_expiry", "none")
    assert list(out.index) == list(D)
    assert list(out["Close"]) == [100, 101, 102, 103, 114, 115]      # C1 D1-D4, C2 D5-D6
    assert list(out["Volume"]) == [1000, 900, 800, 700, 900, 900]
    assert list(out["High"]) == [101, 102, 103, 104, 115, 116]       # OHLC 전달
    assert list(out["Low"]) == [99, 100, 101, 102, 113, 114]


def test_oi_cross_rolls_when_next_oi_overtakes():
    out = build_continuous(_panel(), "oi_cross", "none")
    assert list(out["Close"]) == [100, 101, 112, 113, 114, 115]      # 전환 D3
    assert list(out["Volume"]) == [1000, 900, 750, 800, 900, 900]


def test_volume_cross_rolls_when_next_volume_overtakes():
    out = build_continuous(_panel(), "volume_cross", "none")
    assert list(out["Close"]) == [100, 101, 102, 113, 114, 115]      # 전환 D4


def test_days_before_rolls_n_trading_days_before_expiry():
    out = build_continuous(_panel(), "days_before_2", "none")
    assert list(out["Close"]) == [100, 111, 112, 113, 114, 115]      # 전환 D2 (만기 D4의 2영업일 전)


def test_none_preserves_raw_roll_gap():
    out = build_continuous(_panel(), "at_expiry", "none")
    # D4→D5 = 103→114 (실제 계약전환 베이시스 갭 그대로)
    assert out["Close"].iloc[4] / out["Close"].iloc[3] == pytest.approx(114 / 103)


def test_back_adjust_removes_gap_additively():
    out = build_continuous(_panel(), "at_expiry", "back_adjust")
    # 베이시스 = C2(D4)-C1(D4) = 113-103 = +10 → 과거 세그먼트에 +10
    assert list(out["Close"]) == [110, 111, 112, 113, 114, 115]
    # High도 동일 오프셋
    assert list(out["High"]) == [111, 112, 113, 114, 115, 116]


def test_ratio_preserves_returns_exactly():
    out = build_continuous(_panel(), "at_expiry", "ratio")
    # 롤일 D4 조정가 = C2(D4) 실제가 113.0 (factor 113/103 × C1 103)
    assert out["Close"].iloc[3] == pytest.approx(113.0)
    # 세그먼트 내 수익률 = 원본 근월 수익률, 경계 수익률 = 차월 실제 수익률
    exp = [101 / 100 - 1, 102 / 101 - 1, 103 / 102 - 1, 114 / 113 - 1, 115 / 114 - 1]
    assert np.allclose(out["Close"].pct_change().iloc[1:].to_numpy(), exp)
    assert (out["Close"] > 0).all()                                  # 비율=양수 보존


def test_close_falls_back_to_settle_when_missing():
    d = pd.bdate_range("2020-01-01", periods=3)
    c = pd.DataFrame({"contract": "202001", "Open": [np.nan] * 3, "High": [np.nan] * 3,
                      "Low": [np.nan] * 3, "Close": [100.0, np.nan, 102.0],
                      "Settle": [100.0, 101.0, 102.0], "Volume": [10, 0, 10], "OI": [5, 5, 5]}, index=d)
    out = build_continuous(c, "at_expiry", "none")
    assert list(out["Close"]) == [100, 101, 102]                     # D2 Settle 폴백


def test_single_contract_passthrough():
    c1 = _bars("202001", D[:4], [100, 101, 102, 103], [10, 10, 10, 10], [5, 5, 5, 5])
    out = build_continuous(c1, "at_expiry", "none")
    assert list(out["Close"]) == [100, 101, 102, 103]


def test_empty_panel_returns_empty():
    assert build_continuous(pd.DataFrame()).empty
    assert build_continuous(None).empty


def test_far_months_beyond_live_front_ignored():
    """살아있는 최근월물(마지막날 존재) 뒤의 원월물로 조기 롤하지 않는다.

    실데이터에서 잡힌 버그: 윈도우 끝=만기 오인으로 days_before가 최근월물서 조기 롤,
    ratio 앵커가 미사용 원월물에 잡혀 최근 조정계수 오염. C2가 live front, C3는 never-active.
    """
    c1 = _bars("202003", D[:3], [100, 101, 102], [1000, 900, 800], [5000, 4000, 0])
    c2 = _bars("202006", D[:6], [200, 201, 202, 203, 204, 205], [50, 60, 70, 900, 900, 900],
               [100, 200, 300, 4000, 5000, 5000])
    c3 = _bars("202009", D[:6], [300, 301, 302, 303, 304, 305], [1] * 6, [10] * 6)
    panel = pd.concat([c1, c2, c3]).sort_index()

    out = build_continuous(panel, "at_expiry", "none")
    assert list(out["Close"]) == [100, 101, 102, 203, 204, 205]      # C3로 안 넘어감
    assert out["Close"].iloc[-1] == 205                              # 최근 = live front(C2)

    # days_before_5: 최근월물(C2) 만기 미도래 → 조기 롤 없음(버그#1 회귀 가드)
    assert build_continuous(panel, "days_before_5", "none")["Close"].iloc[-1] == 205
    # ratio 앵커 = 마지막 active(C2), 원월물 오염 없음(버그#2 회귀 가드)
    r = build_continuous(panel, "at_expiry", "ratio")
    assert r["Close"].iloc[-1] == 205
    assert r["Close"].iloc[2] == pytest.approx(202.0)                # C1 D3 → C2(D3) 202로 스케일


def test_days_before_larger_than_segment_clamps():
    # N이 근월 생존일수보다 크면 세그먼트 붕괴 없이 근월 첫날 다음으로 클램프(크래시 없음)
    out = build_continuous(_panel(), "days_before_9", "none")
    assert len(out) == 6
    assert out["Close"].iloc[0] == 100                              # 최소 첫날은 근월
