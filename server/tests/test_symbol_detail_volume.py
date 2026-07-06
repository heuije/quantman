"""symbol_detail 원시 OHLCV 볼륨 우선 서빙 — 데이터엔진 dataset 우선 + FDR 폴백 (무네트워크·monkeypatch).

HOME summary 지배 병목이던 라이브 FDR 재크롤(_raw_ohlcv 종목·_raw_bench 벤치마크, 직렬 ~4s·
range별 재fetch·재배포 warmup)을 데이터엔진 볼륨 read로 대체. 백필과 동일 FDR 소스라 출력동일 —
여기선 ① 볼륨 우선 ② 미커버 FDR 폴백 ③ 벤치마크 심볼→볼륨명 매핑을 계약으로 고정한다.
"""
import pandas as pd

from app.routers import market


def _ohlcv_df(n=600, end="2026-07-06"):
    idx = pd.bdate_range(end=end, periods=n)
    base = pd.Series(range(n), index=idx, dtype=float) + 1000.0
    return pd.DataFrame({"Open": base, "High": base + 5, "Low": base - 5,
                         "Close": base + 1, "Volume": 1_000_000.0}, index=idx)


def _boom(*a, **k):
    raise AssertionError("FDR 라이브 호출됨 — 볼륨 우선이어야 함")


def test_raw_ohlcv_volume_first(monkeypatch):
    """볼륨에 있으면 FDR을 부르지 않고 볼륨 프레임을 반환(원칙6·warmup 근절)."""
    import quant_core.dataset as qc
    import FinanceDataReader as fdr
    df = _ohlcv_df()
    monkeypatch.setattr(qc, "load_dataset_for",
                        lambda keys, with_indicators=True: {list(keys)[0]: df})
    monkeypatch.setattr(fdr, "DataReader", _boom)
    market._raw_ohlcv.cache_clear()
    out = market._raw_ohlcv("005930", "2026-07-07", 2400)
    assert not out.empty and list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert out["Close"].iloc[-1] == df["Close"].iloc[-1]
    # (today - fetch_days) 이후만 슬라이스 — FDR 창과 동일
    assert out.index.min() >= pd.Timestamp(pd.Timestamp("2026-07-07").date() - pd.Timedelta(days=2400))


def test_raw_ohlcv_fdr_fallback_when_uncovered(monkeypatch):
    """볼륨 미커버(신규상장·롱테일·US 미수집)면 FDR 라이브 폴백."""
    import quant_core.dataset as qc
    import FinanceDataReader as fdr
    fdf = _ohlcv_df(n=50)
    monkeypatch.setattr(qc, "load_dataset_for", lambda keys, with_indicators=True: {})
    monkeypatch.setattr(fdr, "DataReader", lambda sym, start: fdf)
    market._raw_ohlcv.cache_clear()
    out = market._raw_ohlcv("999999", "2026-07-07", 500)
    assert out["Close"].iloc[-1] == fdf["Close"].iloc[-1]


def test_raw_bench_volume_mapping(monkeypatch):
    """벤치마크 FDR 심볼(^KS11)→볼륨명(코스피지수) 매핑 후 볼륨 우선.

    FDR ^KS11/^KQ11(캐럿형)은 최근 NaN이라 국내 벤치마크가 깨져 있었다 — 볼륨 실값으로 복구."""
    import quant_core.dataset as qc
    import FinanceDataReader as fdr
    captured = {}

    def fake_load(keys, with_indicators=True):
        captured["keys"] = list(keys)
        return {"코스피지수": _ohlcv_df(n=100)}

    monkeypatch.setattr(qc, "load_dataset_for", fake_load)
    monkeypatch.setattr(fdr, "DataReader", _boom)
    market._raw_bench.cache_clear()
    out = market._raw_bench("^KS11", "2026-07-07", 2400)
    assert captured["keys"] == ["코스피지수"]
    assert not out.empty and "Close" in out.columns


def test_bench_volume_map_covers_all_benchmarks():
    """_benchmark_for가 내보내는 4개 지수가 모두 볼륨명 매핑을 가진다(챗·표시 커버리지 드리프트 차단)."""
    assert market._BENCH_VOLUME == {
        "^KS11": "코스피지수", "^KQ11": "코스닥지수",
        "^IXIC": "나스닥지수", "^GSPC": "S&P500"}


def test_kosdaq_index_in_data_engine():
    """코스닥지수(^KQ11)가 데이터엔진 매크로 수집 심볼로 정식 편입됐다(수집→볼륨→서빙 사슬)."""
    from quant_core import data_fetcher as dfm
    assert dfm.YFINANCE_SYMBOLS.get("코스닥지수") == "^KQ11"
    assert dfm.SYMBOL_CATEGORY.get("코스닥지수") == "지수"
    assert "코스닥지수" in dfm.data_type_symbols()["ohlcv.futures"]
