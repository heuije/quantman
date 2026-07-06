"""국채금리 서빙 — 데이터엔진 bonds 피드 볼륨 → 표시 조립(최신 커브·bp 변동·무네트워크)."""
import pandas as pd

from app import bonds


def _curve():
    idx = pd.to_datetime(["2026-06-30", "2026-07-01"])
    return pd.DataFrame({"3M": [3.85, 3.87], "2Y": [4.10, 4.17], "10Y": [4.40, 4.48]}, index=idx)


def test_country_assembles_from_volume(monkeypatch):
    import quant_core.data.feeds.bonds as feed
    monkeypatch.setattr(feed, "get", lambda cc, **k: _curve())
    out = bonds.country("US")
    assert out["country"] == "US" and out["name"] == "미국"
    assert out["asof"] == "2026-07-01"
    assert out["series"][-1]["10Y"] == 4.48
    assert out["latest"]["10Y"]["yield"] == 4.48
    assert out["latest"]["10Y"]["chg_bp"] == round((4.48 - 4.40) * 100, 1)   # +8.0bp
    assert out["latest"]["2Y"]["chg_bp"] == round((4.17 - 4.10) * 100, 1)    # +7.0bp


def test_country_unknown_code():
    out = bonds.country("XX")
    assert out["series"] == [] and out["latest"] == {}


def test_country_empty_when_no_volume(monkeypatch):
    import quant_core.data.feeds.bonds as feed
    monkeypatch.setattr(feed, "get", lambda cc, **k: None)
    out = bonds.country("US")
    assert out["series"] == [] and out["maturities"] == feed.maturities("US")   # 만기 라벨은 유지
