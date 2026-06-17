"""consensus_kr 피드 — 파싱·의견매핑·standing 집계·ingest dedup (P1, 무네트워크).

핵심 검증: 증권사별 최신 standing(180일窓) 횡단집계 — 한 증권사 새 리포트는 그 증권사
슬롯만 갱신하고 다른 증권사 standing은 유지(덮어쓰기 없음=사용자 확정 모델).

    cd core && pytest tests/test_consensus_kr.py -v
"""
import math

import pandas as pd
import pytest

from quant_core.data.feeds import consensus_kr as ck


# ── 투자의견 매핑 ──────────────────────────────────────────────────────────────

def test_opinion_score_robust():
    for buy in ("Buy", "Strong Buy", "Trading Buy", "Outperform", "Overweight", "매수", "비중확대"):
        assert ck._opinion_score(buy) == 1, buy
    for hold in ("Hold", "Neutral", "Marketperform", "중립", "유지"):
        assert ck._opinion_score(hold) == 0, hold
    for sell in ("Sell", "Reduce", "Underperform", "Underweight", "매도", "비중축소"):
        assert ck._opinion_score(sell) == -1, sell
    assert ck._opinion_score("") is None
    assert ck._opinion_score("의견없음음") is None      # 미상 → 가짜 0 금지


def test_to_int_handles_commas_and_blanks():
    assert ck._to_int("38,000") == 38000
    assert ck._to_int("-") is None
    assert ck._to_int("") is None


# ── HTML 파싱 ──────────────────────────────────────────────────────────────────

_HTML = """
<table>
 <tr><th>작성일</th><th>제목</th><th>적정가격</th><th>투자의견</th><th>작성자</th><th>제공출처</th></tr>
 <tr>
   <td>2024-01-05</td>
   <td><a href="/analysis/downpdf?report_idx=101">삼성전자(005930) 4Q Preview</a>
       <a href="/analysis/downpdf?report_idx=101"><img></a></td>
   <td>90,000</td><td>Buy</td><td>홍길동</td><td>메리츠증권</td>
   <td></td><td></td><td></td>
 </tr>
 <tr>
   <td>2024-01-05</td>
   <td><a href="/analysis/downpdf?report_idx=102">반도체 산업 전망 2024</a></td>
   <td>-</td><td>Overweight</td><td>김분석</td><td>한화투자증권</td>
   <td></td><td></td><td></td>
 </tr>
 <tr>
   <td>2024-01-04</td>
   <td><a href="/analysis/downpdf?report_idx=103">SK하이닉스(000660) 메모리 반등</a></td>
   <td>160,000</td><td>매수</td><td>이리서치</td><td>NH투자증권</td>
   <td></td><td></td><td></td>
 </tr>
</table>
"""


def test_parse_list_page_extracts_and_skips():
    pytest.importorskip("bs4")
    rows = ck._parse_list_page(_HTML)
    by_idx = {r["report_idx"]: r for r in rows}
    # 101: 정상(코드·목표가 있음) — 같은 idx 링크 2개라도 1건 dedup
    assert set(by_idx) == {101, 103}, "산업리포트(코드 없음·idx 102)는 skip돼야"
    r1 = by_idx[101]
    assert r1["code"] == "005930" and r1["target"] == 90000.0
    assert r1["opinion"] == 1 and r1["broker"] == "메리츠증권" and r1["as_of"] == "2024-01-05"
    assert by_idx[103]["code"] == "000660" and by_idx[103]["opinion"] == 1


# ── standing 집계(핵심) ────────────────────────────────────────────────────────

def _raw(rows):
    return pd.DataFrame(rows, columns=["report_idx", "as_of", "code", "broker",
                                       "target", "opinion", "title"])


def test_build_panel_per_broker_latest_no_overwrite():
    """한 증권사 새 리포트는 그 증권사 standing만 교체, 다른 증권사는 유지(사용자 확정 모델)."""
    raw = _raw([
        (1, "2020-01-01", "005930", "A증권", 100.0, 1, "t"),
        (2, "2020-02-01", "005930", "B증권", 120.0, 1, "t"),
        (3, "2020-03-01", "005930", "A증권", 110.0, 0, "t"),   # A의 갱신(100→110)
    ])
    p = ck._build_panel(raw)

    # 1/1: A만 — target 100, 1사
    assert p.loc["2020-01-01", "consensus_target"] == pytest.approx(100.0)
    assert p.loc["2020-01-01", "analyst_count"] == 1
    assert math.isnan(p.loc["2020-01-01", "target_revision_pct"])      # 첫 컨센 → 직전 없음

    # 2/1: A100 + B120 → 평균 110, 2사, 이견 std=10, 리비전 +10%
    assert p.loc["2020-02-01", "consensus_target"] == pytest.approx(110.0)
    assert p.loc["2020-02-01", "analyst_count"] == 2
    assert p.loc["2020-02-01", "target_dispersion"] == pytest.approx(10.0)
    assert p.loc["2020-02-01", "target_revision_pct"] == pytest.approx(10.0)

    # 3/1: A가 110으로 갱신 → A110 + B120 = 115 (B 유지! 덮어쓰기였으면 110)
    assert p.loc["2020-03-01", "consensus_target"] == pytest.approx(115.0)
    assert p.loc["2020-03-01", "analyst_count"] == 2
    assert p.loc["2020-03-01", "consensus_opinion"] == pytest.approx(0.5)   # mean([0,1])


def test_build_panel_freshness_window_expires():
    """180일 초과 standing은 active에서 제외 — 커버리지 소멸 시 NaN(스테일 carry 금지)."""
    raw = _raw([(1, "2020-01-01", "005930", "A증권", 100.0, 1, "t")])
    p = ck._build_panel(raw)
    # 발표일 = 100/1사, 만료일(+180d) = NaN/0사
    assert p.iloc[0]["consensus_target"] == pytest.approx(100.0)
    assert p.iloc[0]["analyst_count"] == 1
    assert p.iloc[-1]["analyst_count"] == 0
    assert math.isnan(p.iloc[-1]["consensus_target"])


def test_build_panel_columns_match_spec_contract():
    """패널 컬럼 = estimate.consensus.provides (spec.py 계약과 일치)."""
    from quant_core.data import spec
    assert list(ck.PANEL_COLS) == spec.get("estimate.consensus").provides


# ── ingest: 원시 전건 보관 + report_idx dedup ──────────────────────────────────

def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ck, "_raw_path", lambda c: tmp_path / f"{c}_raw.parquet")
    monkeypatch.setattr(ck, "_panel_path", lambda c: tmp_path / f"{c}.parquet")


def test_ingest_dedup_and_append_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rep = lambda idx, d, t: {"report_idx": idx, "as_of": d, "code": "005930",
                             "broker": "A증권", "target": float(t), "opinion": 1, "title": "t"}

    s1 = ck.ingest([rep(1, "2020-01-01", 100), rep(2, "2020-02-01", 120)])
    assert s1["codes"] == 1 and s1["new_rows"] == 2
    assert (tmp_path / "005930.parquet").exists()        # 패널 생성

    # 재수집: idx 2(중복) + idx 3(신규) → 원시는 {1,2,3} 전건 보관(2 중복 안 쌓임)
    ck.ingest([rep(2, "2020-02-01", 120), rep(3, "2020-03-01", 130)])
    raw = pd.read_parquet(tmp_path / "005930_raw.parquet")
    assert set(raw["report_idx"]) == {1, 2, 3}
    assert len(raw) == 3
