"""flow_deriv_kr 피드 단위 테스트 — 네트워크·로그인 없음(fake post 주입).

검증: 순수 파서(콤마·음수·역순정렬·정정 keep-last)·윈도우 원자성(한 소스 실패 시 전체
저장 없음)·멱등 merge·비활성 게이트·fail-loud(스키마 드리프트·조용한 전부-0)·
심볼 정합(data_fetcher.MACRO_FLOW_DERIV_SYMBOLS ↔ 피드 SYMBOLS — 리터럴 드리프트 차단)·
카테고리 등재(수급 — 누락 시 챗 카탈로그 '개별종목' 오분류)."""

from __future__ import annotations

import pandas as pd
import pytest

from quant_core import data_fetcher as df_mod
from quant_core.data.feeds import flow_deriv_kr as fd


def _fut_row(dd: str, a07: str, a12: str) -> dict:
    return {"TRD_DD": dd, "A07": a07, "A08": "1,000", "A09": "-2,000",
            "A12": a12, "AMT_OR_QTY": "0"}


def _etf_row(dd: str, v21: str, v24: str) -> dict:
    return {"TRD_DD": dd, "NUM_ITM_VAL21": v21, "NUM_ITM_VAL22": "1,000",
            "NUM_ITM_VAL23": "-2,000", "NUM_ITM_VAL24": v24, "NUM_ITM_VAL25": "0"}


def _make_post(fut_by_isu: dict, etf_rows, fail: set | None = None):
    """payload를 보고 소스별 canned 응답을 주는 fake post. fail 집합의 소스는 None(네트워크 실패)."""
    fail = fail or set()

    def post(payload):
        if payload["bld"].endswith("13102"):
            isu = payload["isuCd"]
            if isu in fail:
                return None
            return {"output": fut_by_isu.get(isu, [])}
        if "etf" in fail:
            return None
        return {"output": etf_rows}
    return post


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """로그인 env + 임시 DATA_DIR + 스로틀 0 — 전 테스트 공용."""
    monkeypatch.setenv("KRX_ID", "test-id")
    monkeypatch.setenv("KRX_PW", "test-pw")
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)
    monkeypatch.setattr(fd, "_THROTTLE_S", 0)
    monkeypatch.setattr(fd, "_RETRY_SLEEP_S", 0)
    return tmp_path


def test_parse_rows_comma_negative_sort_dedup():
    rows = [_fut_row("2026/07/03", "-404,451,200,000", "34,076,187,500"),
            _fut_row("2026/07/02", "1,000", "-2,500"),
            _fut_row("2026/07/03", "-404,451,200,001", "34,076,187,501")]   # 같은 날 정정 → 마지막
    df = fd.parse_rows(rows, fd._FUT_COLS)
    assert list(df.index) == [pd.Timestamp("2026-07-02"), pd.Timestamp("2026-07-03")]  # 역순→정렬
    assert df.loc["2026-07-03", "기관"] == -404451200001.0
    assert df.loc["2026-07-03", "외국인"] == 34076187501.0
    assert df.loc["2026-07-02", "외국인"] == -2500.0


def test_fetch_range_saves_six_symbols_and_values(env):
    fut = {"KR___FUK2I": [_fut_row("2026/07/02", "-100", "200"), _fut_row("2026/07/03", "-110", "210")],
           "KR___FUKQI": [_fut_row("2026/07/03", "50", "-60")]}
    etf = [_etf_row("2026/07/03", "300", "-400")]
    res = fd.fetch_range("20260702", "20260703", post=_make_post(fut, etf))
    assert res["ok"] is True
    assert set(res["saved"]) == set(fd.SYMBOLS)
    k200_f = df_mod._load_existing("코스피200선물외국인순매수")
    assert len(k200_f) == 2 and k200_f["Close"].iloc[-1] == 210.0
    assert df_mod._load_existing("코스닥150선물기관순매수")["Close"].iloc[0] == 50.0
    assert df_mod._load_existing("KRETF외국인순매수")["Close"].iloc[0] == -400.0
    assert df_mod._load_existing("KRETF기관순매수")["Close"].iloc[0] == 300.0


def test_fetch_range_atomic_when_one_source_fails(env):
    """ETF 실패 → 선물이 성공했어도 아무것도 저장하지 않는다(전체 재시도가 멱등 복구)."""
    fut = {"KR___FUK2I": [_fut_row("2026/07/03", "1", "2")],
           "KR___FUKQI": [_fut_row("2026/07/03", "3", "4")]}
    res = fd.fetch_range("20260703", "20260703", post=_make_post(fut, [], fail={"etf"}))
    assert res["ok"] is False and res["fail"] == "KRETF"
    assert res["saved"] == {}
    assert not (env / "코스피200선물외국인순매수.parquet").exists()


def test_fetch_range_idempotent(env):
    fut = {"KR___FUK2I": [_fut_row("2026/07/03", "1", "2")],
           "KR___FUKQI": [_fut_row("2026/07/03", "3", "4")]}
    etf = [_etf_row("2026/07/03", "5", "6")]
    post = _make_post(fut, etf)
    fd.fetch_range("20260703", "20260703", post=post)
    fd.fetch_range("20260703", "20260703", post=post)          # 재수집 — 행수 동일(멱등)
    assert len(df_mod._load_existing("코스피200선물기관순매수")) == 1


def test_inactive_without_login(monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    res = fd.fetch_range("20260701", "20260703", post=lambda p: {"output": []})
    assert res.get("inactive") is True and res["ok"] is False and res["saved"] == {}


def test_holiday_empty_window_is_ok(env):
    """휴장 구간(빈 output)은 실패가 아니다 — ok=True·저장 0(증분 cron이 주말에 헛돌지 않게)."""
    res = fd.fetch_range("20260704", "20260705",
                         post=_make_post({"KR___FUK2I": [], "KR___FUKQI": []}, []))
    assert res["ok"] is True and not any(res["saved"].values())


def test_schema_drift_fails_loud(env):
    """행은 오는데 매핑 컬럼 키(A12)가 전 행에 없으면 실패 — 반쪽 데이터 조용 적재 금지."""
    bad = [{"TRD_DD": "2026/07/03", "A07": "1", "AMT_OR_QTY": "0"}]
    res = fd.fetch_range("20260703", "20260703", post=_make_post({"KR___FUK2I": bad}, []))
    assert res["ok"] is False and res["fail"] == "코스피200선물"


def test_empty_string_era_keeps_window_and_skips_investor(env):
    """키는 있고 값이 ''인 시대(KQ150 상장 초기 외국인 미집계 실측)는 실패가 아니다 —
    기관은 저장·외국인은 그 날 자연 제외(실패 처리하면 백필 커서가 경계에서 영구 정체)."""
    era = [{"TRD_DD": f"2015/12/{d:02d}", "A07": f"{d}00", "A08": "0", "A09": "1", "A12": "",
            "AMT_OR_QTY": "0"} for d in range(1, 8)]
    fut = {"KR___FUK2I": [_fut_row("2015/12/01", "1", "2")], "KR___FUKQI": era}
    res = fd.fetch_range("20151201", "20151207", post=_make_post(fut, [_etf_row("2015/12/01", "3", "4")]))
    assert res["ok"] is True
    assert res["saved"]["코스닥150선물기관순매수"] == 7
    assert res["saved"]["코스닥150선물외국인순매수"] == 0       # ''=값 없음 — 가짜 0 적재 금지
    assert not (env / "코스닥150선물외국인순매수.parquet").exists()


def test_all_zero_window_fails_loud(env):
    """≥5행 전부 정확히 0 = isuCd 형식 드리프트의 조용한 무데이터(prodId 함정 부류) — 실패."""
    zeros = [_fut_row(f"2026/06/{d:02d}", "0", "0") for d in range(22, 27)]
    res = fd.fetch_range("20260622", "20260626", post=_make_post({"KR___FUK2I": zeros}, []))
    assert res["ok"] is False and res["fail"] == "코스피200선물"


def test_window_over_limit_raises(env):
    with pytest.raises(ValueError):
        fd.fetch_range("20200101", "20260101", post=lambda p: {"output": []})


def test_symbols_locked_to_data_fetcher():
    """data_fetcher.MACRO_FLOW_DERIV_SYMBOLS(리터럴) ↔ 피드 SYMBOLS 정합 — 한쪽만 고치는
    드리프트를 CI가 차단(cot_cftc 가드와 동형)."""
    derived = {p + inv + "순매수"
               for p in ("코스피200선물", "코스닥150선물", "KRETF") for inv in ("외국인", "기관")}
    assert set(fd.SYMBOLS) == derived
    assert set(df_mod.MACRO_FLOW_DERIV_SYMBOLS) == derived


def test_symbols_categorized_as_supgup():
    """6심볼 전부 SYMBOL_CATEGORY '수급' 등재 — 누락 시 챗 카탈로그가 '개별종목'으로 오분류."""
    for s in fd.SYMBOLS:
        assert df_mod.symbol_category(s) == "수급", s
