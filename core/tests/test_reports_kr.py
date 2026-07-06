"""reports_kr 피드 — 파싱·종목명해결·nid dedup·ingest·목표가/투자의견·컨센서스 패널 (무네트워크).

한경 consensus_kr 은퇴 → 이 피드가 리포트 목록 AND 컨센서스 지표의 유일 소스.

    cd core && pytest tests/test_reports_kr.py -v
"""
import math

import pandas as pd
import pytest

from quant_core.data.feeds import reports_kr as rk


# 네이버 크로스종목 목록 구조: [종목명, 제목(company_read?nid=N), 증권사, PDF, 작성일(YY.MM.DD), 조회수]
_HTML = """
<table class="type_1">
 <tr><th>종목명</th><th>제목</th><th>증권사</th><th>첨부</th><th>작성일</th><th>조회수</th></tr>
 <tr>
   <td>삼성전자</td>
   <td><a href="company_read.naver?nid=101&page=1">4Q Preview 실적 서프라이즈</a></td>
   <td>메리츠증권</td>
   <td><a href="https://stock.pstatic.net/stock-research/company/1/20260706_company_1.pdf"><img></a></td>
   <td>26.07.06</td><td>3129</td>
 </tr>
 <tr>
   <td>듣보종목명</td>
   <td><a href="company_read.naver?nid=102&page=1">이름 매칭 안 되는 리포트</a></td>
   <td>한화투자증권</td>
   <td></td>
   <td>26.07.06</td><td>2000</td>
 </tr>
 <tr>
   <td>SK하이닉스</td>
   <td><a href="company_read.naver?nid=103&page=1">메모리 반등</a></td>
   <td>NH투자증권</td>
   <td></td>
   <td>26.07.05</td><td>4100</td>
 </tr>
 <tr>
   <td>삼성전자</td>
   <td><a href="company_read.naver?nid=101&page=2">같은 nid 중복 행</a></td>
   <td>메리츠증권</td>
   <td></td>
   <td>26.07.06</td><td>10</td>
 </tr>
</table>
"""

_N2C = {"삼성전자": "005930", "SK하이닉스": "000660"}


def test_parse_list_page_resolves_dedups_and_skips_unmatched():
    pytest.importorskip("bs4")
    rows = rk._parse_list_page(_HTML, _N2C)
    by_nid = {r["nid"]: r for r in rows}
    # 102: 종목명 미해결 → skip. 101 중복 → 1건 dedup.
    assert set(by_nid) == {101, 103}, "종목명 미해결(102)·중복 nid(101 두 번)는 걸러져야"
    r1 = by_nid[101]
    assert r1["code"] == "005930" and r1["broker"] == "메리츠증권"
    assert r1["as_of"] == "2026-07-06", "YY.MM.DD → ISO 정규화"
    assert r1["title"] == "4Q Preview 실적 서프라이즈"
    assert r1["url"].endswith("_1.pdf"), "PDF 직접 링크 우선"
    r3 = by_nid[103]
    assert r3["code"] == "000660"
    assert "company_read.naver?nid=103" in r3["url"], "PDF 없으면 상세 페이지 URL 폴백"


def test_name_to_code_from_ticker_db(monkeypatch):
    """종목명→코드 맵은 ticker_db의 .KS(KR) 항목에서 6자리 코드만 취한다."""
    monkeypatch.setattr(rk, "load_db", lambda: [
        {"t": "005930.KS", "k": "삼성전자", "e": "", "x": "KOSPI"},
        {"t": "AAPL", "k": "", "e": "Apple", "x": "NASDAQ"},      # US → 제외
        {"t": "000660.KS", "k": "SK하이닉스", "e": "", "x": "KOSPI"},
    ])
    m = rk._name_to_code()
    assert m == {"삼성전자": "005930", "SK하이닉스": "000660"}


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(rk, "_reports_path", lambda c: tmp_path / f"{c}.parquet")
    monkeypatch.setattr(rk, "_panel_path", lambda c: tmp_path / f"{c}_panel.parquet")


def test_ingest_dedup_append_only_and_sorted(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rec = lambda nid, d, code, brk: {"nid": nid, "as_of": d, "code": code,
                                     "broker": brk, "title": f"t{nid}", "url": f"u{nid}"}
    # 1차 적재
    s1 = rk.ingest([rec(1, "2026-06-01", "005930", "A증권"),
                    rec(2, "2026-06-05", "005930", "B증권")])
    assert s1 == {"codes": 1, "new_rows": 2}
    # 2차 — nid=2 중복 + nid=3 신규 → append-only, dedup, 신규 1건만
    s2 = rk.ingest([rec(2, "2026-06-05", "005930", "B증권"),
                    rec(3, "2026-06-03", "005930", "C증권")])
    assert s2 == {"codes": 1, "new_rows": 1}
    df = pd.read_parquet(tmp_path / "005930.parquet")
    assert list(df["nid"]) == [1, 3, 2], "nid dedup 후 as_of 오름차순 정렬"
    assert len(df) == 3


def test_ingest_empty_is_noop():
    assert rk.ingest([]) == {"codes": 0, "new_rows": 0}


# ── 목표가·투자의견 상세 추출(enrich) ──────────────────────────────────────────

def test_opinion_score_robust():
    for buy in ("Buy", "StrongBuy", "Overweight", "매수", "비중확대"):
        assert rk._opinion_score(buy) == 1, buy
    for hold in ("Hold", "Neutral", "Marketperform", "중립", "유지"):
        assert rk._opinion_score(hold) == 0, hold
    for sell in ("Sell", "Reduce", "Underweight", "매도", "비중축소"):
        assert rk._opinion_score(sell) == -1, sell
    assert rk._opinion_score("") is None
    assert rk._opinion_score("없음") is None            # Not Rated → 미상(가짜 0 금지)


# 네이버 상세 페이지 헤더 구조: 목표가=.money, 투자의견=텍스트
_DETAIL = '<div class="pv_area">7.06 | 조회 3229 <em class="money">760,000</em> | 투자의견 Buy 2Q 판매</div>'
_DETAIL_NOTRATED = '<div>목표가 <em class="money">0</em> | 투자의견 없음</div>'


def test_parse_detail_target_and_opinion():
    pytest.importorskip("bs4")
    assert rk._parse_detail(_DETAIL) == (760000, 1)
    assert rk._parse_detail(_DETAIL_NOTRATED) == (None, None)   # 목표가 0·의견 없음 → None


def test_enrich_attaches_target_opinion(monkeypatch):
    monkeypatch.setattr(rk, "_fetch_detail",
                        lambda nid: {101: (760000, 1), 103: (None, None)}[nid])
    recs = [{"nid": 101, "code": "005380"}, {"nid": 103, "code": "000660"}]
    out = rk.enrich(recs)
    assert out[0]["target"] == 760000 and out[0]["opinion"] == 1
    assert out[1]["target"] is None and out[1]["opinion"] is None


# ── 컨센서스 지표 패널 (한경 consensus_kr 은퇴 → reports_kr가 유일 소스) ──────────────
# 증권사별 최신 standing(180일窓) 횡단집계 — 한 증권사 새 리포트는 그 슬롯만 갱신(덮어쓰기 없음).

def _raw(rows):
    return pd.DataFrame(rows, columns=["nid", "as_of", "code", "broker", "target", "opinion"])


def test_build_panel_per_broker_latest_no_overwrite():
    raw = _raw([
        (1, "2020-01-01", "005930", "A증권", 100.0, 1),
        (2, "2020-02-01", "005930", "B증권", 120.0, 1),
        (3, "2020-03-01", "005930", "A증권", 110.0, 0),   # A 갱신(100→110)
    ])
    p = rk._build_panel(raw)
    assert p.loc["2020-01-01", "consensus_target"] == pytest.approx(100.0)
    assert p.loc["2020-01-01", "analyst_count"] == 1
    assert math.isnan(p.loc["2020-01-01", "target_revision_pct"])
    # 2/1: A100 + B120 → 평균 110, 2사, std 10, 리비전 +10%
    assert p.loc["2020-02-01", "consensus_target"] == pytest.approx(110.0)
    assert p.loc["2020-02-01", "analyst_count"] == 2
    assert p.loc["2020-02-01", "target_dispersion"] == pytest.approx(10.0)
    assert p.loc["2020-02-01", "target_revision_pct"] == pytest.approx(10.0)
    # 3/1: A→110 갱신, B120 유지 → 115 (덮어쓰기였으면 110)
    assert p.loc["2020-03-01", "consensus_target"] == pytest.approx(115.0)
    assert p.loc["2020-03-01", "analyst_count"] == 2
    assert p.loc["2020-03-01", "consensus_opinion"] == pytest.approx(0.5)   # mean([0,1])


def test_build_panel_freshness_window_expires():
    raw = _raw([(1, "2020-01-01", "005930", "A증권", 100.0, 1)])
    p = rk._build_panel(raw)
    assert p.iloc[0]["consensus_target"] == pytest.approx(100.0)
    assert p.iloc[0]["analyst_count"] == 1
    assert p.iloc[-1]["analyst_count"] == 0                # 만료일(+180d) → 커버리지 소멸
    assert math.isnan(p.iloc[-1]["consensus_target"])


def test_build_panel_empty_when_no_target_column():
    # enrich 전(목표가 미부착) 원시 → 컨센 없음(KeyError 아님)
    assert rk._build_panel(pd.DataFrame({"nid": [1], "as_of": ["2026-01-01"], "broker": ["A"]})).empty


def test_panel_cols_match_spec_contract():
    from quant_core.data import spec
    assert list(rk.PANEL_COLS) == spec.get("estimate.consensus").provides


def test_ingest_writes_consensus_panel(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    reports = [
        {"nid": 1, "as_of": "2026-06-01", "code": "005930", "broker": "A증권",
         "title": "t1", "url": "u1", "target": 100.0, "opinion": 1},
        {"nid": 2, "as_of": "2026-06-05", "code": "005930", "broker": "B증권",
         "title": "t2", "url": "u2", "target": 120.0, "opinion": 1},
    ]
    rk.ingest(reports)
    panel = pd.read_parquet(tmp_path / "005930_panel.parquet")
    assert list(panel.columns) == list(rk.PANEL_COLS)
    assert panel["analyst_count"].max() == 2               # 2 증권사 집계


def test_rebuild_all_panels_from_existing_raw(monkeypatch, tmp_path):
    # 한경→네이버 컷오버: 저장된 원시에서 패널 재산출(재fetch 없음)
    monkeypatch.setenv("QP_CORE_DATA_DIR", str(tmp_path))
    (tmp_path / "reports").mkdir(parents=True)
    for code, tgt in [("005930", 100000.0), ("000660", 200000.0)]:
        pd.DataFrame([{"nid": 1, "as_of": "2026-06-01", "code": code, "broker": "A증권",
                       "title": "t", "url": "u", "target": tgt, "opinion": 1}]
                     ).to_parquet(tmp_path / "reports" / f"{code}.parquet")
    assert rk.rebuild_all_panels() == 2
    panel = pd.read_parquet(tmp_path / "consensus" / "005930.parquet")
    assert list(panel.columns) == list(rk.PANEL_COLS)
    assert panel["consensus_target"].dropna().iloc[0] == pytest.approx(100000.0)
