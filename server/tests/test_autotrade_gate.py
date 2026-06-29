from app.autotrade_caps_api import asset_class_for_symbol, capability_matrix


def test_asset_class_kr_stock():
    assert asset_class_for_symbol("005930", "KOSPI") == "kr_equity"


def test_asset_class_us_stock():
    assert asset_class_for_symbol("AAPL", "NAS") == "us_equity"


def test_asset_class_kospi200_futures():
    assert asset_class_for_symbol("코스피200선물", "") == "kr_futures"


def test_asset_class_mini_kospi200_futures():
    # 미니도 정규와 같은 kr_futures(currency KRW + asset_class futures) — 게이트 무변경으로 개방
    assert asset_class_for_symbol("미니코스피200선물", "") == "kr_futures"


def test_asset_class_jp_hk_unsupported():
    # 일본(TSE)/홍콩(HKS)은 자동매매 미지원 — None
    assert asset_class_for_symbol("7203", "TSE") is None
    assert asset_class_for_symbol("0700", "HKS") is None
    assert asset_class_for_symbol("TSLA", "UNKNOWN") is None


def test_capability_matrix_shape():
    m = capability_matrix()
    assert m["kis"]["paper"]["kr_futures"]["status"] == "ok"
    assert m["kis"]["paper"]["us_futures"]["status"] == "blocked"
    assert m["ls"]["paper"]["us_equity"]["status"] == "blocked"


# ── G5 capability 게이트(_assert_live_tradable) ──────────────────────────────────
import pytest
from fastapi import HTTPException
from app.routers.strategies import _assert_live_tradable


def _defn(symbols, broker, direction="long"):
    return {"universe": {"kind": "single", "symbols": symbols},
            "position": {"direction": direction, "entry": {"mode": "scheduled"}},
            "simulation": {}}


def test_kospi200_futures_paper_allowed_when_bound(monkeypatch):
    _assert_live_tradable("paper", _defn(["코스피200선물"], "kis"), account_broker="kis")


def test_mini_kospi200_futures_paper_allowed_when_bound(monkeypatch):
    # 미니도 kr_futures라 KIS·LS 모의 모두 게이트 통과(코드 변경 없이 개방)
    _assert_live_tradable("paper", _defn(["미니코스피200선물"], "kis"), account_broker="kis")
    _assert_live_tradable("paper", _defn(["미니코스피200선물"], "ls"), account_broker="ls")


def test_overseas_futures_blocked_phase1(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _defn(["원유선물"], "ls"), account_broker="ls")
    assert e.value.status_code == 422
    assert "준비 중" in e.value.detail


def test_ls_us_stock_paper_blocked(monkeypatch):
    # 게이트는 KIS 마스터에서 심볼→market을 얻어 자산군 판정. 단위 테스트엔 마스터가 없으니
    # AAPL→NAS를 주입(없으면 market="" → asset_class None → 잘못된 분기).
    monkeypatch.setattr("app.kis_master_cache.get_master_list",
                        lambda: [{"symbol": "AAPL", "market": "NAS", "name": "Apple"}])
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _defn(["AAPL"], "ls"), account_broker="ls")
    assert e.value.status_code == 422
    assert "모의" in e.value.detail


def test_unsupported_market_blocked(monkeypatch):
    monkeypatch.setattr("app.kis_master_cache.get_master_list",
                        lambda: [{"symbol": "7203", "market": "TSE", "name": "Toyota"}])
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _defn(["7203"], "kis"), account_broker="kis")
    assert e.value.status_code == 422
    assert "미지원" in e.value.detail


def test_unverified_live_requires_ack(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("live", _defn(["코스피200선물"], "kis"),
                              account_broker="kis", ack_unverified=False)
    assert e.value.status_code == 422
    assert "검증" in e.value.detail
    _assert_live_tradable("live", _defn(["코스피200선물"], "kis"),
                          account_broker="kis", ack_unverified=True)


def test_jp_hk_excluded_and_autotrade_hint(monkeypatch):
    from app.routers import backtest
    idx = {"005930": {"rows": 100, "has_ohlc": True},        # KR 주식
           "코스피200선물": {"rows": 100, "has_ohlc": True},   # KR 선물
           "원유선물": {"rows": 100, "has_ohlc": True},        # 해외선물(us_futures)
           "7203": {"rows": 100, "has_ohlc": True}}           # 일본(TSE) — 제외돼야
    master = [{"symbol": "005930", "market": "KOSPI", "kind": "stock", "name": "삼성"},
              {"symbol": "7203", "market": "TSE", "kind": "stock", "name": "Toyota"},
              {"symbol": "0700", "market": "HKS", "kind": "stock", "name": "Tencent"}]  # 홍콩 master-only
    monkeypatch.setattr(backtest.data_cache, "get_symbol_index", lambda: idx)
    monkeypatch.setattr(backtest.kis_master_cache, "get_master_list", lambda: master)
    monkeypatch.setattr(backtest.kis_master_cache, "get_status", lambda: {})
    payload = backtest._build_symbols_payload()
    syms = {s["symbol"]: s for s in payload["symbols"]}
    assert "7203" not in syms and "0700" not in syms
    assert not any("일본" in s["category"] or "홍콩" in s["category"] for s in payload["symbols"])
    assert syms["005930"]["autotrade_hint"] == "ok"            # KR equity
    assert syms["코스피200선물"]["autotrade_hint"] == "ok"      # KR futures
    assert syms["원유선물"]["autotrade_hint"] == "backtest_only"  # 해외선물 Phase 1 미지원
    assert syms["005930"]["autotrade_asset_class"] == "kr_equity"
    assert syms["코스피200선물"]["autotrade_asset_class"] == "kr_futures"
    assert syms["원유선물"]["autotrade_asset_class"] == "us_futures"


def test_capabilities_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/strategies/autotrade-capabilities")
    assert r.status_code == 200
    m = r.json()
    assert m["kis"]["paper"]["kr_futures"]["status"] == "ok"
    assert m["ls"]["paper"]["us_equity"]["status"] == "blocked"
