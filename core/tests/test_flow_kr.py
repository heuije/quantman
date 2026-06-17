"""flow_kr 피드 — key-safe 비활성·컬럼 정규화·transient/genuine 구분·merge dedup (P1, 무네트워크).

pykrx 호출은 monkeypatch로 대체(실 로그인·네트워크 없음). KRX_ID/PW 미설정 시 no-op 보장이 핵심.

    cd core && pytest tests/test_flow_kr.py -v
"""
import pandas as pd
import pytest

from quant_core.data.feeds import flow_kr as fk


# ── key-safe: 로그인 없으면 비활성 ─────────────────────────────────────────────

def test_fetch_inactive_without_login(monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    calls = {"n": 0}

    def spy(*a):
        calls["n"] += 1
        return pd.DataFrame(), True
    monkeypatch.setattr(fk, "fetch_one", spy)

    r = fk.fetch(["005930"], "20140101", "20240101")
    assert r["inactive"] is True and r["symbols"] == 0
    assert calls["n"] == 0, "로그인 없으면 pykrx 헛호출 0"


# ── fetch_one: pykrx 출력 정규화 ───────────────────────────────────────────────

def test_fetch_one_normalizes_columns(monkeypatch):
    pytest.importorskip("pykrx")
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    fake = pd.DataFrame({"기관합계": [100, -50], "외국인": [-20, 30], "개인": [5, 5]}, index=idx)
    monkeypatch.setattr("pykrx.stock.get_market_trading_value_by_date", lambda s, e, c: fake)

    df, ok = fk.fetch_one("005930", "20240101", "20240131")
    assert ok is True
    assert list(df.columns) == ["inst_net_buy", "foreign_net_buy"]
    assert df.index.name == "as_of"
    assert df.loc["2024-01-02", "inst_net_buy"] == 100
    assert df.loc["2024-01-03", "foreign_net_buy"] == 30


def test_fetch_one_login_error_returns_not_ok(monkeypatch):
    pytest.importorskip("pykrx")
    def boom(s, e, c):
        raise RuntimeError("KRX 로그인 실패")
    monkeypatch.setattr("pykrx.stock.get_market_trading_value_by_date", boom)

    df, ok = fk.fetch_one("005930", "20240101", "20240131")
    assert ok is False and df.empty, "로그인/네트워크 오류=transient(ok=False) → 호출자 마커 금지"


def test_fetch_one_schema_change_yields_empty(monkeypatch):
    pytest.importorskip("pykrx")
    fake = pd.DataFrame({"전체": [1]}, index=pd.to_datetime(["2024-01-02"]))
    monkeypatch.setattr("pykrx.stock.get_market_trading_value_by_date", lambda s, e, c: fake)

    df, ok = fk.fetch_one("005930", "20240101", "20240131")
    assert ok is True and df.empty, "기관/외국인 컬럼 미발견=스키마변동 → 빈결과(가짜 채움 금지)"


# ── fetch: merge dedup + freshness skip ────────────────────────────────────────

def test_fetch_merge_dedup_and_freshness(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    monkeypatch.setattr(fk, "_flow_path", lambda c: tmp_path / f"{c}.parquet")
    monkeypatch.setattr(fk, "_marker_path", lambda c: tmp_path / f"{c}.empty")

    def win(rows):
        return pd.DataFrame(
            {"inst_net_buy": [r[1] for r in rows], "foreign_net_buy": [r[2] for r in rows]},
            index=pd.Index(pd.to_datetime([r[0] for r in rows]), name="as_of"))

    # 1차 적재
    monkeypatch.setattr(fk, "fetch_one",
                        lambda c, s, e: (win([("2024-01-02", 100, 1), ("2024-01-03", 200, 2)]), True))
    r1 = fk.fetch(["005930"], "20240101", "20240103", fresh_days=2)
    assert r1["ok"] == 1 and not r1["inactive"]
    assert len(pd.read_parquet(tmp_path / "005930.parquet")) == 2

    # 즉시 재실행 → 신선도 skip
    r2 = fk.fetch(["005930"], "20240101", "20240103", fresh_days=2)
    assert r2["symbols"] == 0, "fresh_days 이내 재시도 skip"

    # 겹치는 새 윈도우 → merge + dedup(최신 우선)
    monkeypatch.setattr(fk, "fetch_one",
                        lambda c, s, e: (win([("2024-01-03", 999, 9), ("2024-01-04", 300, 3)]), True))
    fk.fetch(["005930"], "20240101", "20240104", fresh_days=0)   # fresh_days=0 → 강제 refetch
    merged = pd.read_parquet(tmp_path / "005930.parquet")
    assert list(merged.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert merged.loc["2024-01-03", "inst_net_buy"] == 999, "겹치는 날짜는 새 윈도우 값(keep last)"


def test_provides_match_spec_contract():
    from quant_core.data import spec
    assert list(fk._COL_MAP.keys()) == spec.get("flow.kr_investor").provides
