"""미확정(세션 미마감) 봉 가드 — yfinance 장중 봉이 dataset에 저장되지 않는다.

2026-07-21 인시던트 회귀 고정: US 정규장이 열려 있는 동안 fetch되면 yfinance가 형성 중인
당일 봉(미완성 Close·저거래량)을 반환하는데, 그게 저장되면 증분 fetch가 마지막 봉을 재조회하지
않아 영구 동결됐다(^GSPC 07-20=7495.04 미확정이 실계좌 #27 방향을 롱→숏 역전). 종전의 clock
기반 `now.hour<20` 스킵은 주말/휴장 갭에 뚫렸다. 이 테스트는 데이터 기준(16:00 ET 마감
워터마크) 드롭이 그 부류를 저장 단계에서 막음을 고정한다.

    cd platform && pytest tests/test_provisional_bar_guard.py -v
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core import data_fetcher as dfh  # noqa: E402


def _ohlcv(idx, closes=None, tz=None):
    """OHLCV 프레임. closes 지정 시 Close에 반영. tz 지정 시 tz-aware index(yfinance 모사)."""
    index = pd.to_datetime(idx)
    if tz is not None:
        index = index.tz_localize(tz)
    n = len(idx)
    cl = closes if closes is not None else [1.0] * n
    return pd.DataFrame(
        {"Open": cl, "High": cl, "Low": cl, "Close": cl, "Volume": [1] * n},
        index=index)


# ── _drop_provisional_tail 단위 ────────────────────────────────────────────

def test_drops_provisional_us_bar(monkeypatch):
    """US 심볼: 워터마크(마지막 마감일)보다 뒤인 trailing 봉을 드롭."""
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 17))
    df = _ohlcv(["2026-07-16", "2026-07-17", "2026-07-20"])   # 07-20 = 장중(미마감)
    out = dfh._drop_provisional_tail(df, "^GSPC")
    assert list(out.index.date) == [date(2026, 7, 16), date(2026, 7, 17)]


def test_keeps_final_us_bar(monkeypatch):
    """US 심볼: 세션 마감 후(워터마크가 그 날까지 전진)면 그 봉을 유지."""
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 20))
    df = _ohlcv(["2026-07-16", "2026-07-17", "2026-07-20"])
    out = dfh._drop_provisional_tail(df, "^GSPC")
    assert list(out.index.date) == [date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 20)]


def test_unguarded_markets_never_dropped(monkeypatch):
    """세션 캘린더가 없는 시장(JP/HK/UK/EU)엔 워터마크 미적용 — 신선한 봉 오드롭 방지."""
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 17))
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 17))
    df = _ohlcv(["2026-07-16", "2026-07-17", "2026-07-20"])
    for t in ("^N225", "^HSI", "^FTSE", "^GDAXI", "^STOXX50E"):
        out = dfh._drop_provisional_tail(df, t)
        assert len(out) == 3, f"{t} 오드롭"


def test_kr_uses_its_own_calendar_not_us(monkeypatch):
    """KR 지수는 **KR 캘린더**로 가드 — US 워터마크를 쓰면 신선한 KR 봉을 오드롭한다.

    16:00 KST 상황: KR은 이미 마감(07-21 봉 최종)인데 US 07-21 세션은 미마감.
    US 워터마크(07-20)를 쓰면 신선한 07-21 KR 봉이 잘못 드롭된다.
    """
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 21))  # KR 마감
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 20))  # US 미마감
    df = _ohlcv(["2026-07-20", "2026-07-21"])
    for t in ("^KS11", "^KQ11", "261220"):
        out = dfh._drop_provisional_tail(df, t)
        assert len(out) == 2, f"{t}: KR 마감 후 신선한 07-21 봉이 유지돼야(US 워터마크 오적용 금지)"


def test_kr_drops_provisional_during_kr_session(monkeypatch):
    """KR 장중(마감 전)엔 형성 중인 KR 당일 봉을 드롭한다 — US와 같은 부류 방어."""
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 20))  # 07-21 장중
    df = _ohlcv(["2026-07-20", "2026-07-21"])
    for t in ("^KS11", "^KQ11", "261220"):
        out = dfh._drop_provisional_tail(df, t)
        assert list(out.index.date) == [date(2026, 7, 20)], f"{t}: 장중 미확정 봉 드롭 실패"


def test_calendar_failure_is_conservative(monkeypatch):
    """캘린더 로드 실패(예외)면 드롭하지 않는다 — fetch를 깨지 않는 보수적 무동작."""
    def _boom():
        raise RuntimeError("calendar unavailable")
    monkeypatch.setattr(dfh, "_last_closed_kr_date", _boom)
    df = _ohlcv(["2026-07-20", "2026-07-21"])
    assert len(dfh._drop_provisional_tail(df, "^KS11")) == 2


def test_no_watermark_no_drop(monkeypatch):
    """워터마크 None(tz 불가)이면 보수적으로 전량 유지(과소 드롭 없음)."""
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: None)
    df = _ohlcv(["2026-07-16", "2026-07-20"])
    assert len(dfh._drop_provisional_tail(df, "^GSPC")) == 2


# ── _yf_history 통합 (yfinance 모킹) ───────────────────────────────────────

class _FakeTicker:
    def __init__(self, df):
        self._df = df

    def history(self, *a, **k):
        return self._df


def test_yf_history_drops_provisional(monkeypatch):
    """실제 fetch 경로: 장중 캡처된 07-20 미확정 봉이 저장 전에 드롭된다(인시던트 재현)."""
    # yfinance는 tz-aware(America/New_York) index를 준다 — _yf_history가 tz_localize(None) 처리.
    fake = _ohlcv(["2026-07-16", "2026-07-17", "2026-07-20"],
                  closes=[7533.77, 7457.69, 7495.04],   # 07-20 미확정(실제 최종 7443.28)
                  tz="America/New_York")
    monkeypatch.setattr(dfh.yf, "Ticker", lambda t: _FakeTicker(fake))
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 17))  # 07-20 장중

    out = dfh._yf_history("^GSPC", "2026-01-01")
    assert list(out.index.date) == [date(2026, 7, 16), date(2026, 7, 17)]
    assert 7495.04 not in out["Close"].values, "미확정 07-20 봉이 저장돼선 안 된다"


def test_yf_history_negative_control_without_guard(monkeypatch):
    """부정 대조 — 드롭 가드를 무력화하면 미확정 봉이 그대로 통과(가드가 실제 원인임을 증명)."""
    fake = _ohlcv(["2026-07-17", "2026-07-20"], closes=[7457.69, 7495.04],
                  tz="America/New_York")
    monkeypatch.setattr(dfh.yf, "Ticker", lambda t: _FakeTicker(fake))
    monkeypatch.setattr(dfh, "_drop_provisional_tail", lambda df, t: df)  # 가드 OFF

    out = dfh._yf_history("^GSPC", "2026-01-01")
    assert 7495.04 in out["Close"].values, "가드 없으면 미확정 봉이 통과해야(대조 성립)"


# ── fetch_yfinance end-to-end: 미확정 봉은 저장 안 되고, 마감 후 최종값이 저장 ──────

def test_fetch_yfinance_incident_end_to_end(tmp_path, monkeypatch):
    """장중 fetch는 미확정 07-20을 저장하지 않고(원장 07-17 유지),
    마감 후 fetch는 최종 07-20(7443.28)을 저장한다 — #27 방향 역전을 원천 차단."""
    def fake_path(sym):
        return tmp_path / f"{sym}.parquet"
    monkeypatch.setattr(dfh, "_parquet_path", fake_path)
    monkeypatch.setattr(dfh, "mark_data_dirty", lambda: None)
    # 기존 원장 = 금요일(07-17)까지
    _ohlcv(["2026-07-16", "2026-07-17"], closes=[7533.77, 7457.69]).to_parquet(fake_path("S&P500"))

    # (1) 월요일 07-20 장중 — yfinance가 미확정 07-20(7495.04) 반환
    prov = _ohlcv(["2026-07-20"], closes=[7495.04], tz="America/New_York")
    monkeypatch.setattr(dfh.yf, "Ticker", lambda t: _FakeTicker(prov))
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 17))  # 장중=마감 전
    out1 = dfh.fetch_yfinance("S&P500", "^GSPC")
    assert out1.index[-1].date() == date(2026, 7, 17), "장중엔 미확정 07-20을 저장하면 안 된다"
    assert 7495.04 not in out1["Close"].values

    # (2) 마감 후 — yfinance가 최종 07-20(7443.28) 반환, 워터마크가 07-20으로 전진
    final = _ohlcv(["2026-07-20"], closes=[7443.28], tz="America/New_York")
    monkeypatch.setattr(dfh.yf, "Ticker", lambda t: _FakeTicker(final))
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 20))
    out2 = dfh.fetch_yfinance("S&P500", "^GSPC")
    assert out2.index[-1].date() == date(2026, 7, 20), "마감 후엔 최종 07-20을 저장"
    assert out2.loc["2026-07-20", "Close"] == 7443.28

    # 신호 부호 검증: 07-20 최종(-0.19%)은 음수 → #27 롱(역전 아님)
    pct = 7443.28 / 7457.69 - 1
    assert pct < 0, "최종 데이터의 07-20 전일대비는 음수(→롱)여야 한다"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ── 같은 부류 나머지 진입점 (2026-07-21 전수 스캔) ──────────────────────────────
#
# `_yf_history` 말고도 parquet에 봉을 쓰는 경로가 3개 더 있었고 전부 미가드였다:
#   ① `_fdr_history`(fetch_fdr)        — 스킵 펜스조차 없음 → **영구 동결**(^GSPC와 동일)
#   ② `fetch_korean_stocks`            — 펜스는 있으나 갭이 생기면 우회 → **영구 동결**
#   ③ `_fetch_overseas_batched`        — end= 없는 yf.download. 자가치유형(1~3거래일)
# ①②는 `fetch_all`을 통해 ^GSPC와 **같은 부팅/cron 트리거**를 탄다.


def test_fdr_history_drops_provisional(monkeypatch):
    """① FDR: KRX 장중 봉이 저장 전에 드롭된다(FDR_SYMBOLS는 6자리 숫자 → KR 라우팅)."""
    fake = _ohlcv(["2026-07-17", "2026-07-20", "2026-07-21"])
    monkeypatch.setattr(dfh.fdr, "DataReader", lambda t, s: fake)
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 20))  # 07-21 장중

    out = dfh._fdr_history("261220", "2026-01-01")
    assert list(out.index.date) == [date(2026, 7, 17), date(2026, 7, 20)]


def test_fdr_history_negative_control(monkeypatch):
    """부정 대조 — 가드를 끄면 미확정 봉이 통과한다."""
    fake = _ohlcv(["2026-07-20", "2026-07-21"])
    monkeypatch.setattr(dfh.fdr, "DataReader", lambda t, s: fake)
    monkeypatch.setattr(dfh, "_drop_provisional_tail", lambda df, t: df)

    out = dfh._fdr_history("261220", "2026-01-01")
    assert list(out.index.date)[-1] == date(2026, 7, 21)


def test_fetch_fdr_would_freeze_forever_without_guard(tmp_path, monkeypatch):
    """① end-to-end — `fetch_fdr`엔 스킵 펜스가 없어 한 번 오염되면 재조회가 사라진다.

    장중 저장 → 다음 호출의 `start = 마지막봉+1`이 미래라 FDR이 빈 응답 → `return existing`
    → 그 값이 **영원히** 남는다. 가드가 그 첫 저장을 막는지 확인한다.
    """
    monkeypatch.setattr(dfh, "_parquet_path", lambda s: tmp_path / f"{s}.parquet")
    monkeypatch.setattr(dfh, "mark_data_dirty", lambda: None)
    _ohlcv(["2026-07-17", "2026-07-20"], closes=[100.0, 101.0]).to_parquet(
        tmp_path / "코스피200선물ETF.parquet")

    # (1) 07-21 장중 — FDR이 형성 중 봉(99.0)을 준다
    monkeypatch.setattr(dfh.fdr, "DataReader",
                        lambda t, s: _ohlcv(["2026-07-21"], closes=[99.0]))
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 20))
    out1 = dfh.fetch_fdr("코스피200선물ETF", "261220")
    assert out1.index[-1].date() == date(2026, 7, 20), "장중 미확정 봉을 저장하면 안 된다"
    assert 99.0 not in out1["Close"].values

    # (2) 15:40 마감 후 — 최종값(103.0)이 저장된다
    monkeypatch.setattr(dfh.fdr, "DataReader",
                        lambda t, s: _ohlcv(["2026-07-21"], closes=[103.0]))
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 21))
    out2 = dfh.fetch_fdr("코스피200선물ETF", "261220")
    assert out2.loc["2026-07-21", "Close"] == 103.0


def test_fetch_korean_stocks_drops_provisional(tmp_path, monkeypatch):
    """② 펜스 우회 시나리오 — 갭이 있는 종목은 fetch되는데, 그때 장중 봉이 섞여 온다.

    펜스(`last_date >= last_closed`)는 최신 종목만 막는다. 원장이 07-17에 멈춘 종목은
    펜스를 통과해 fetch되고, 응답에 형성 중 07-21 봉이 들어 있으면 종전엔 그대로 저장됐다.
    저장되면 다음 날부터 펜스에 걸려 **영구 동결**된다.
    """
    monkeypatch.setattr(dfh, "_parquet_path", lambda s: tmp_path / f"{s}.parquet")
    monkeypatch.setattr(dfh, "mark_data_dirty", lambda: None)
    _ohlcv(["2026-07-16", "2026-07-17"]).to_parquet(tmp_path / "005930.parquet")  # 갭 보유
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 20))   # 07-21 장중
    monkeypatch.setattr(dfh.fdr, "DataReader",
                        lambda c, s: _ohlcv(["2026-07-20", "2026-07-21"], closes=[70000.0, 69000.0]))

    res = dfh.fetch_korean_stocks(["005930"])
    stored = dfh._load_existing("005930")
    assert res["ok"] == 1
    assert stored.index[-1].date() == date(2026, 7, 20), "07-21 장중 봉이 저장되면 안 된다"
    assert 69000.0 not in stored["Close"].values


def test_fetch_korean_stocks_keeps_closed_session_bar(tmp_path, monkeypatch):
    """② 부정 대조 — 마감된 세션 봉은 그대로 저장돼야 한다(과잉 드롭 방지)."""
    monkeypatch.setattr(dfh, "_parquet_path", lambda s: tmp_path / f"{s}.parquet")
    monkeypatch.setattr(dfh, "mark_data_dirty", lambda: None)
    _ohlcv(["2026-07-16", "2026-07-17"]).to_parquet(tmp_path / "005930.parquet")
    monkeypatch.setattr(dfh, "_last_closed_kr_date", lambda: date(2026, 7, 21))   # 마감 후
    monkeypatch.setattr(dfh.fdr, "DataReader",
                        lambda c, s: _ohlcv(["2026-07-20", "2026-07-21"], closes=[70000.0, 69000.0]))

    dfh.fetch_korean_stocks(["005930"])
    stored = dfh._load_existing("005930")
    assert stored.index[-1].date() == date(2026, 7, 21)
    assert 69000.0 in stored["Close"].values


def test_overseas_batched_drops_provisional(tmp_path, monkeypatch):
    """③ `_yf_history`를 우회하는 두 번째 yf 진입점(end= 없는 yf.download)."""
    monkeypatch.setattr(dfh, "_parquet_path", lambda s: tmp_path / f"{s}.parquet")
    monkeypatch.setattr(dfh, "mark_data_dirty", lambda: None)
    monkeypatch.setattr(dfh, "_last_closed_us_date", lambda: date(2026, 7, 20))   # 07-21 장중
    monkeypatch.setattr(dfh.yf, "download",
                        lambda *a, **k: _ohlcv(["2026-07-20", "2026-07-21"], closes=[150.0, 149.0]))

    dfh._fetch_overseas_batched(["AAPL"], "2026-01-01", 50)
    stored = dfh._load_existing("AAPL")
    assert stored.index[-1].date() == date(2026, 7, 20), "US 장중 봉이 저장되면 안 된다"
    assert 149.0 not in stored["Close"].values
