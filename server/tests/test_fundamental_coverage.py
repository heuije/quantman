"""GET /admin/fundamental-coverage — KR 펀더멘털 백필 진척 경량 집계 회귀.

coverage()는 core가 파일시스템 스캔으로 산출, 라우터는 total·coverage_pct를 덧붙인다.

    cd server && pytest tests/test_fundamental_coverage.py -v
"""
import pytest

admin = pytest.importorskip("app.routers.admin")


def test_fundamental_coverage_shape(monkeypatch):
    """라우터가 core coverage 집계에 total_managed·coverage_pct를 더해 반환."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import fundamental_kr

    monkeypatch.setattr(data_fetcher, "load_managed_kr_codes", lambda: ["A", "B", "C", "D"])
    monkeypatch.setattr(fundamental_kr, "coverage",
                        lambda codes: {"have_fundamentals": 3, "empty_marked": 1,
                                       "written_last_24h": 2,
                                       "newest_mtime": 1_700_000_000.0,
                                       "oldest_mtime": 1_600_000_000.0})

    out = admin.fundamental_coverage(user=None)
    assert out["total_managed"] == 4
    assert out["have_fundamentals"] == 3
    assert out["coverage_pct"] == 75.0
    assert out["empty_marked"] == 1
    assert out["written_last_24h"] == 2
    assert out["newest"] is not None and out["oldest"] is not None


def test_fundamental_coverage_empty_universe(monkeypatch):
    """관리 종목이 0이면 coverage_pct=None(0 나눗셈 방지)."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import fundamental_kr

    monkeypatch.setattr(data_fetcher, "load_managed_kr_codes", lambda: [])
    monkeypatch.setattr(fundamental_kr, "coverage",
                        lambda codes: {"have_fundamentals": 0, "empty_marked": 0,
                                       "written_last_24h": 0, "newest_mtime": None,
                                       "oldest_mtime": None})

    out = admin.fundamental_coverage(user=None)
    assert out["total_managed"] == 0
    assert out["coverage_pct"] is None
    assert out["newest"] is None


def test_clear_fundamental_markers(monkeypatch):
    """POST /admin/clear-fundamental-markers가 core clear_markers 호출 + removed 반환."""
    from quant_core.data.feeds import fundamental_kr
    monkeypatch.setattr(fundamental_kr, "clear_markers", lambda: 42)
    out = admin.clear_fundamental_markers(user=None)
    assert out["ok"] is True and out["removed"] == 42
