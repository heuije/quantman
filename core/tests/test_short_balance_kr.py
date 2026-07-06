"""short_balance_kr 피드 — 일별 전종목 공매도 잔고 수집(key-safe 비활성·컬럼정규화·merge·차단 커서유지·휴장 skip).

pykrx 호출은 fetch 주입으로 대체(실 로그인·네트워크 없음). KRX_ID/PW 미설정 시 no-op 보장이 핵심.
throttle/재시도 sleep은 monkeypatch로 제거해 빠르게 돈다.

    cd core && pytest tests/test_short_balance_kr.py -v
"""
import pandas as pd
import pytest

from quant_core.data.feeds import short_balance_kr as sb


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sb.time, "sleep", lambda *a, **k: None)


def _baldf(mapping: dict):
    """{code: (bal_qty, bal_amt, bal_ratio)} → get_shorting_balance_by_ticker 반환형 df.

    실제 pykrx 컬럼(공매도잔고·상장주식수·공매도금액·시가총액·비중)을 그대로 흉내 —
    substring 매칭(_pick_col)이 정확히 우리 3컬럼만 골라내는지 계약 고정.
    """
    if not mapping:
        return pd.DataFrame()
    return pd.DataFrame(
        {"공매도잔고": [v[0] for v in mapping.values()],
         "상장주식수": [1_000_000 for _ in mapping],
         "공매도금액": [v[1] for v in mapping.values()],
         "시가총액":   [9_999_999 for _ in mapping],
         "비중":       [v[2] for v in mapping.values()]},
        index=list(mapping.keys()))


# ── key-safe: 로그인 없으면 비활성(fetch 0회) ─────────────────────────────────

def test_fetch_range_inactive_without_login(monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return pd.DataFrame()

    r = sb.fetch_range("20240101", "20240110", fetch=spy)
    assert r["inactive"] is True and r["ok"] is False and r["stocks"] == 0
    assert calls["n"] == 0, "로그인 없으면 pykrx 헛호출 0"


# ── 정규화 + 종목별 저장(시장 2콜/일 → per_code 병합·우리 3컬럼만 추출) ────────

def test_fetch_range_normalizes_and_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(sb, "_short_path", lambda c: tmp_path / f"{c}.parquet")

    data = {
        "KOSPI": {"005930": (1000.0, 5_000_000.0, 0.5), "000660": (2000.0, 8_000_000.0, 1.2)},
        "KOSDAQ": {"247540": (30.0, 90_000.0, 0.1)},
    }

    def fake(bas_dd, market):
        return _baldf(data.get(market, {}))

    r = sb.fetch_range("20240101", "20240103", fetch=fake)
    assert r["ok"] is True and r["stocks"] == 3
    df = pd.read_parquet(tmp_path / "005930.parquet")
    assert list(df.columns) == ["bal_qty", "bal_amt", "bal_ratio"]   # 우리 3컬럼만(상장주식수·시총 제외)
    assert df["bal_qty"].iloc[-1] == 1000.0
    assert df["bal_amt"].iloc[-1] == 5_000_000.0
    assert df["bal_ratio"].iloc[-1] == 0.5
    kq = pd.read_parquet(tmp_path / "247540.parquet")
    assert kq["bal_ratio"].iloc[-1] == 0.1                            # KOSDAQ도 수집


# ── 차단(None) → 부분 전진 금지·커서 유지 ────────────────────────────────────

def test_fetch_range_blocked_no_partial_write(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(sb, "_short_path", lambda c: tmp_path / f"{c}.parquet")

    def blocked(bas_dd, market):
        return None                                                  # 봇차단/네트워크

    r = sb.fetch_range("20240101", "20240103", fetch=blocked)
    assert r["ok"] is False and r["fail"] == 1 and r["stocks"] == 0
    assert not (tmp_path / "005930.parquet").exists()                # 부분 write 없음(윈도우 재시도)


# ── 휴장(Length mismatch) → 빈 df로 접힘·차단 아님(ok) ────────────────────────

def test_holiday_length_mismatch_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(sb, "_short_path", lambda c: tmp_path / f"{c}.parquet")

    def holiday(bas_dd, market):
        raise ValueError("Length mismatch: Expected 0 elements, new values have ...")

    r = sb.fetch_range("20240101", "20240103", fetch=holiday)
    assert r["ok"] is True and r["stocks"] == 0                       # 휴장 skip·차단 오판 아님


# ── load 라운드트립 + 미커버 빈 DF ───────────────────────────────────────────

def test_load_short_balance_roundtrip(monkeypatch, tmp_path):
    from quant_core.parquet_io import write_parquet_atomic
    monkeypatch.setattr(sb, "_short_path", lambda c: tmp_path / f"{c}.parquet")
    df = pd.DataFrame({"bal_qty": [10.0], "bal_amt": [20.0], "bal_ratio": [0.3]},
                      index=pd.to_datetime(["2026-07-03"]))
    write_parquet_atomic(df, tmp_path / "005930.parquet")
    got = sb.load_short_balance("005930")
    assert not got.empty and got["bal_qty"].iloc[0] == 10.0
    assert sb.load_short_balance("999999").empty                     # 미커버 → 빈 DF


def test_pick_col_substring():
    df = _baldf({"005930": (1.0, 2.0, 0.3)})
    assert sb._pick_col(df, ("공매도잔고",)) == "공매도잔고"
    assert sb._pick_col(df, ("공매도금액",)) == "공매도금액"
    assert sb._pick_col(df, ("비중",)) == "비중"
    assert sb._pick_col(df, ("없는컬럼",)) is None
