"""GlobalMarket 지수·원자재 서빙 — 데이터엔진 dataset 우선 + FDR 폴백 (무네트워크·monkeypatch)."""
import pandas as pd

from app import globalmarket as gm


def _close_df(vals):
    idx = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"])
    return pd.DataFrame({"Close": vals}, index=idx)


def test_indices_dataset_first_and_fallback(monkeypatch):
    import quant_core.dataset as qc
    # 볼륨엔 S&P500만 수집됨 → 나머지는 라이브 FDR 폴백
    monkeypatch.setattr(qc, "load_dataset_for",
                        lambda names, with_indicators=True: {"S&P500": _close_df([100.0, 101.0, 102.0])})
    monkeypatch.setattr(gm, "_fetch_close", lambda sym, days: _close_df([10.0, 11.0, 12.0])["Close"])
    gm._indices.cache_clear()
    out = gm.indices()
    by = {it["name"]: it for it in out["items"]}
    assert by["S&P 500"]["last"] == 102.0          # dataset(볼륨)
    assert by["S&P 500"]["symbol"] == "US500"      # FDR 심볼 보존(프론트 symbolDetail 링크)
    assert by["나스닥"]["last"] == 12.0             # 미수집 → FDR 폴백
    assert all(it["last"] is not None for it in out["items"])
    assert len(out["items"]) == len(gm.INDICES) and len(out["series"]) >= 1


def test_commodities_dataset_first_and_fallback(monkeypatch):
    import quant_core.dataset as qc
    monkeypatch.setattr(qc, "load_dataset_for",
                        lambda names, with_indicators=True: {"원유선물": _close_df([60.0, 61.0, 62.5])})
    monkeypatch.setattr(gm, "_fetch_close", lambda sym, days: _close_df([1.0, 1.1, 1.2])["Close"])
    gm._commodities.cache_clear()
    out = gm.commodities()
    by = {it["name"]: it for it in out["items"]}
    assert by["WTI 원유"]["last"] == 62.5 and by["WTI 원유"]["symbol"] == "CL=F"
    assert by["WTI 원유"]["change"] == round(62.5 - 61.0, 4)
    assert by["금"]["last"] == 1.2                  # 미수집 → FDR 폴백
    assert all(it["last"] is not None for it in out["items"])
    assert len(out["items"]) == len(gm.COMMODITIES)


def test_commodities_empty_when_no_data(monkeypatch):
    import quant_core.dataset as qc
    monkeypatch.setattr(qc, "load_dataset_for", lambda names, with_indicators=True: {})
    monkeypatch.setattr(gm, "_fetch_close", lambda sym, days: None)   # 폴백도 실패
    gm._commodities.cache_clear()
    out = gm.commodities()
    assert all(it["last"] is None for it in out["items"])            # 가짜 0 금지·항목은 유지
