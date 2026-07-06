"""추정실적 서빙 — 데이터엔진 estimate_kr 피드 → 웹 KrEarnings({years(E), 한글 rows}) 어댑터.

옛 krdata 로컬 wcomp 파싱·별도 json 캐시를 제거하고 estimate_kr 피드 SSOT로 단일화(§5.5 잔재).
어댑터는 프레임 컬럼을 한글 행 키로 매핑하고 추정 연도에 '(E)' 접미만 붙인다(무네트워크).
"""
import pandas as pd

from app import krdata


def _frame():
    """estimate_kr.get 반환 프레임 모사 — 확정 1년 + 추정 2년(E)."""
    idx = pd.Index(["2024/12", "2025/12", "2026/12"], name="fiscal")
    return pd.DataFrame({
        "rev": [3008709.0, 3200000.0, 3500000.0],
        "op": [329724.0, 400000.0, 500000.0],
        "ni": [340000.0, 420000.0, 520000.0],
        "controlling_ni": [335000.0, 410000.0, 510000.0],
        "per": [10.5, 9.0, 7.2], "pbr": [1.4, 1.3, 1.2], "roe": [9.5, 11.0, 12.5],
        "eps": [5000.0, 6200.0, 7700.0],
        "op_margin": [11.0, 12.5, 14.3], "net_margin": [11.3, 13.1, 14.9],
        "is_estimate": [False, True, True],
    }, index=idx)


def test_earnings_adapts_feed_to_web_shape(monkeypatch):
    import quant_core.data.feeds.estimate_kr as est
    monkeypatch.setattr(est, "get", lambda c: _frame())
    krdata._earnings_cached.cache_clear()
    out = krdata.earnings("005930")
    assert out["years"] == ["2024/12", "2025/12(E)", "2026/12(E)"]     # 추정 연도 (E) 접미
    assert out["rows"]["매출액"] == [3008709.0, 3200000.0, 3500000.0]
    assert out["rows"]["영업이익"] == [329724.0, 400000.0, 500000.0]
    assert out["rows"]["지배주주"] == [335000.0, 410000.0, 510000.0]    # controlling_ni → 지배주주
    assert out["rows"]["영업이익률"] == [11.0, 12.5, 14.3]
    assert out["rows"]["당기순이익률"] == [11.3, 13.1, 14.9]
    # 웹 KrEarnings가 소비하는 한글 행 키 전부(매출/영업/순이익·마진·멀티플·수익성·EPS).
    assert set(out["rows"]) == {"매출액", "영업이익", "당기순이익", "지배주주",
                                "PER", "PBR", "ROE", "EPS", "영업이익률", "당기순이익률"}


def test_earnings_empty_when_no_feed(monkeypatch):
    import quant_core.data.feeds.estimate_kr as est
    monkeypatch.setattr(est, "get", lambda c: pd.DataFrame())
    krdata._earnings_cached.cache_clear()
    assert krdata.earnings("999999") == {"years": [], "rows": {}}     # 미커버 → 빈(가짜 0 금지)
