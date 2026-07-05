"""flow_kr 피드 — 일별 전종목 수집(key-safe 비활성·정규화·merge dedup·차단 커서유지·휴장 skip).

pykrx 호출은 fetch 주입으로 대체(실 로그인·네트워크 없음). KRX_ID/PW 미설정 시 no-op 보장이 핵심.
throttle/재시도 sleep은 monkeypatch로 제거해 빠르게 돈다.

    cd core && pytest tests/test_flow_kr.py -v
"""
import pandas as pd
import pytest

from quant_core.data.feeds import flow_kr as fk


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(fk.time, "sleep", lambda *a, **k: None)


def _tickerdf(mapping: dict):
    """{code: 순매수거래대금} → 일별전종목 함수 반환형 df(index=티커·종목명+순매수거래대금)."""
    if not mapping:
        return pd.DataFrame()
    return pd.DataFrame({"종목명": ["x"] * len(mapping),
                         "순매수거래대금": list(mapping.values())},
                        index=list(mapping.keys()))


# ── key-safe: 로그인 없으면 비활성(fetch 0회) ─────────────────────────────────

def test_fetch_range_inactive_without_login(monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return pd.DataFrame()

    r = fk.fetch_range("20240101", "20240110", fetch=spy)
    assert r["inactive"] is True and r["ok"] is False and r["stocks"] == 0
    assert calls["n"] == 0, "로그인 없으면 pykrx 헛호출 0"


# ── 정규화 + 종목별 저장(시장·투자자 4콜/일 → per_code 병합) ─────────────────

def test_fetch_range_normalizes_and_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(fk, "_flow_path", lambda c: tmp_path / f"{c}.parquet")

    data = {
        ("KOSPI", "기관합계"): {"005930": 100.0, "000660": 200.0},
        ("KOSPI", "외국인"): {"005930": -20.0, "000660": 30.0},
        ("KOSDAQ", "기관합계"): {"247540": 7.0},
        ("KOSDAQ", "외국인"): {"247540": -3.0},
    }

    def fake(fd, td, market, investor):
        return _tickerdf(data.get((market, investor), {}))

    r = fk.fetch_range("20240102", "20240102", fetch=fake)   # 2024-01-02 = 화(거래일)
    assert r["ok"] is True and r["days"] == 1 and r["stocks"] == 3

    df = pd.read_parquet(tmp_path / "005930.parquet")
    assert list(df.columns) == ["inst_net_buy", "foreign_net_buy"], "spec provides 순서"
    assert df.index.name == "as_of"
    assert df.loc["2024-01-02", "inst_net_buy"] == 100.0
    assert df.loc["2024-01-02", "foreign_net_buy"] == -20.0
    kq = pd.read_parquet(tmp_path / "247540.parquet")
    assert kq.loc["2024-01-02", "inst_net_buy"] == 7.0


# ── 차단(None): 커서 유지·아무것도 안 씀 ──────────────────────────────────────

def test_fetch_range_block_holds_window(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(fk, "_flow_path", lambda c: tmp_path / f"{c}.parquet")

    def blocked(fd, td, market, investor):
        raise RuntimeError("get_stock_ticker_isin: 'NoneType' object is not subscriptable")

    r = fk.fetch_range("20240102", "20240104", fetch=blocked)
    assert r["ok"] is False and r["fail"] == 1 and r["stocks"] == 0
    assert list(tmp_path.glob("*.parquet")) == [], "차단이면 부분 전진 금지(무기록)"


# ── 휴장일: Length mismatch(0 elements) / 빈 df → skip(fail 아님) ─────────────

def test_fetch_range_holiday_length_mismatch_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(fk, "_flow_path", lambda c: tmp_path / f"{c}.parquet")

    def holiday(fd, td, market, investor):
        raise ValueError("Length mismatch: Expected axis has 0 elements, new values have 6 elements")

    r = fk.fetch_range("20240102", "20240102", fetch=holiday)
    assert r["ok"] is True and r["fail"] == 0 and r["stocks"] == 0, "휴장은 차단 아님(커서 전진)"
    assert list(tmp_path.glob("*.parquet")) == []


def test_fetch_range_empty_df_skips(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(fk, "_flow_path", lambda c: tmp_path / f"{c}.parquet")

    r = fk.fetch_range("20240102", "20240102", fetch=lambda *a: pd.DataFrame())
    assert r["ok"] is True and r["stocks"] == 0


# ── merge dedup: 겹치는 윈도우는 최신 값 우선 ─────────────────────────────────

def test_fetch_range_merge_dedup(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(fk, "_flow_path", lambda c: tmp_path / f"{c}.parquet")

    def only_kospi_inst(mapping):
        def fake(fd, td, market, investor):
            if market == "KOSPI" and investor == "기관합계":
                return _tickerdf(mapping.get(fd, {}))
            return pd.DataFrame()
        return fake

    # 윈도우 1: 2024-01-02 inst=100
    fk.fetch_range("20240102", "20240102", fetch=only_kospi_inst({"20240102": {"005930": 100.0}}))
    # 윈도우 2: 2024-01-02(겹침) inst=999 + 2024-01-03 inst=300
    fk.fetch_range("20240102", "20240103",
                   fetch=only_kospi_inst({"20240102": {"005930": 999.0},
                                          "20240103": {"005930": 300.0}}))
    merged = pd.read_parquet(tmp_path / "005930.parquet")
    assert list(merged.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert merged.loc["2024-01-02", "inst_net_buy"] == 999.0, "겹치는 날짜는 새 윈도우 값(keep last)"
    assert merged.loc["2024-01-03", "inst_net_buy"] == 300.0


# ── spec 계약: provides == _COL_MAP 키(순서 포함) ─────────────────────────────

def test_provides_match_spec_contract():
    from quant_core.data import spec
    assert list(fk._COL_MAP.keys()) == spec.get("flow.kr_investor").provides
