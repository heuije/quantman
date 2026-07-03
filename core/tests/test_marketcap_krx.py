"""static.market_cap 피드(KRX sto 시총·거래대금) 단위 테스트 — 네트워크 없음(fake fetch).

핵심 검증: ①에러 페이로드(OutBlock 부재)와 휴장 빈응답의 구분 — 미신청 서비스에서 커서가
데이터 없이 침묵 전진하는 거짓 완료의 부류 차단 ②ISU_CD 12자 ISIN/6자 단축 양쪽 해석
③윈도우 부분 실패 시 ok=False(부분 전진 금지) ④종목별 parquet 멱등 merge.
"""

from __future__ import annotations

import pandas as pd

from quant_core import data_fetcher as df_mod
from quant_core.data.feeds import marketcap_krx as mk


def _row(isu, mktcap="1000", trdval="500", shrs="10"):
    return {"ISU_CD": isu, "MKTCAP": mktcap, "ACC_TRDVAL": trdval, "LIST_SHRS": shrs}


def test_extract_rows_handles_isin_and_short_codes():
    out = mk.extract_rows([_row("KR7005930003"), _row("000660"),
                           _row("US0378331005"),          # 비KR ISIN → skip(오귀속 금지)
                           _row("005380", mktcap="-", trdval="-", shrs="-")])  # 전결측 → 제외
    assert set(out) == {"005930", "000660"}
    assert out["005930"] == (1000.0, 500.0, 10.0)


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mk, "MARKETCAP_DIR", tmp_path / "marketcap")
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)
    monkeypatch.setattr(mk, "is_active", lambda: True)


def test_error_payload_fails_but_holiday_ok(monkeypatch, tmp_path):
    """에러(None)=실패(커서 유지) / 휴장([])=정상 진행 — 침묵 전진 부류 차단의 핵심."""
    _setup(monkeypatch, tmp_path)

    res = mk.fetch_range("20240102", "20240103", fetch=lambda svc, bd: None)
    assert res["ok"] is False and res["fail"] >= 1          # 미신청/네트워크 → 커서 유지

    res2 = mk.fetch_range("20240106", "20240107", fetch=lambda svc, bd: [])  # 주말+휴장
    assert res2["ok"] is True and res2["stocks"] == 0       # 휴장은 정상(커서 전진 가능)


def test_partial_day_failure_blocks_advance(monkeypatch, tmp_path):
    """윈도우 중 하루라도 실패면 ok=False — 중간 구멍(부분 전진) 방지, 전체 재시도(멱등)."""
    _setup(monkeypatch, tmp_path)

    def fetch(svc, bd):
        if bd == "20240103":
            return None                                     # 하루 실패
        return [_row("KR7005930003")]

    res = mk.fetch_range("20240102", "20240104", fetch=fetch)
    assert res["ok"] is False
    assert mk.load_marketcap("005930").empty                # 부분 저장 없음


def test_fetch_range_saves_and_idempotent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    fetch = lambda svc, bd: [_row("KR7005930003", mktcap="2000")] if svc == mk._SVC_STK \
        else [_row("KR7035720002", mktcap="300")]           # KOSPI·KOSDAQ 병합

    res = mk.fetch_range("20240102", "20240103", fetch=fetch)
    assert res["ok"] is True and res["stocks"] == 2

    d = mk.load_marketcap("005930")
    assert list(d.columns) == ["market_cap", "trade_value", "shares_listed"]
    assert len(d) == 2 and d["market_cap"].iloc[0] == 2000.0
    assert d.index.name == "as_of"

    mk.fetch_range("20240102", "20240103", fetch=fetch)     # 재수집 — 멱등(중복 없음)
    assert len(mk.load_marketcap("005930")) == 2
    assert len(mk.load_marketcap("035720")) == 2
