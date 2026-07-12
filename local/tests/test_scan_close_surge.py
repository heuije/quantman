# -*- coding: utf-8 -*-
"""브로커 종가창 스캔(scan_close_surge) 응답 파서 — KIS·LS 패리티 픽스처 검증.

전송 계층(_get_retry/_post)을 픽스처로 대체해 파싱·임계 중단·잠김 판정·배치 조립만
검증한다(실 TR 가용성·필드 실측은 릴리스 전 실측 게이트 ⓐⓑ — 설계 §6).
"""
from __future__ import annotations

import pytest

from localapp.kis_broker import KisBroker
from localapp.ls_broker import LsBroker


# ── KIS — FHPST01820000(예상체결 상승상위) + FHKST01010100(상한가) ────────────

def _kis(monkeypatch, ranking_rows, mxpr_by_code):
    b = KisBroker.__new__(KisBroker)
    b.quote_base = "https://real.example"
    calls = []

    def fake_get(path, tr, params, base=None):
        calls.append((tr, dict(params)))
        if tr == "FHPST01820000":
            assert params["fid_mkop_cls_code"] == "1"      # 장마감예상 모드 고정
            return {"output": ranking_rows}
        if tr == "FHKST01010100":
            code = params["FID_INPUT_ISCD"]
            return {"output": {"stck_mxpr": mxpr_by_code.get(code, "0")}}
        raise AssertionError(f"예상 밖 TR: {tr}")

    monkeypatch.setattr(b, "_get_retry", fake_get, raising=False)
    return b, calls


_KIS_ROWS = [
    {"stck_shrn_iscd": "123450", "hts_kor_isnm": "잠김상한", "stck_prpr": "1300",
     "prdy_ctrt": "30.00", "total_askp_rsqn": "0"},
    {"stck_shrn_iscd": "999999", "hts_kor_isnm": "파싱불가", "stck_prpr": "N/A",
     "prdy_ctrt": "29.90", "total_askp_rsqn": "10"},          # 개별 row 결함 — 제외·계속
    {"stck_shrn_iscd": "067310", "hts_kor_isnm": "준상한", "stck_prpr": "1290",
     "prdy_ctrt": "29.20", "total_askp_rsqn": "5000"},
    {"stck_shrn_iscd": "005930", "hts_kor_isnm": "임계미달", "stck_prpr": "70000",
     "prdy_ctrt": "10.00", "total_askp_rsqn": "1"},           # 임계 미달 — 여기서 중단
    {"stck_shrn_iscd": "BROKEN"},                              # 중단 이후 — 도달하면 안 됨
]


def test_kis_scan_parses_and_marks_limit_up(monkeypatch):
    b, calls = _kis(monkeypatch, _KIS_ROWS, {"123450": "1300", "067310": "1300"})
    rows = b.scan_close_surge(20.0)
    assert [r["symbol"] for r in rows] == ["123450", "067310"]
    assert rows[0]["is_limit_up"] is True                     # 예상체결 1300 ≥ 상한가 1300
    assert rows[1]["is_limit_up"] is False                    # 1290 < 1300 (준상한 — 엣지 없음)
    assert rows[0]["change_pct"] == 30.0 and rows[0]["name"] == "잠김상한"
    # 상세(상한가) 조회는 임계 통과 2종목만 — 미달·결함 row엔 호출하지 않는다.
    assert [c for c in calls if c[0] == "FHKST01010100"] == [
        ("FHKST01010100", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "123450"}),
        ("FHKST01010100", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "067310"}),
    ]


def test_kis_upper_limit_failure_is_conservative(monkeypatch):
    # 상한가 조회 실패(0) → is_limit_up=False(잠김 아님으로 제외 — 자금 안전 방향).
    b, _ = _kis(monkeypatch, _KIS_ROWS[:1], {})
    rows = b.scan_close_surge(20.0)
    assert rows[0]["is_limit_up"] is False


# ── LS — t1488(예상체결가등락율상위) + t8407(멀티현재가 배치) ──────────────────

def _ls(monkeypatch, pages, t8407_rows):
    b = LsBroker.__new__(LsBroker)
    calls = []
    pages = list(pages)

    def fake_post(path, tr_cd, body, **kw):
        calls.append((path, tr_cd, body))
        if tr_cd == "t1488":
            return pages.pop(0)
        if tr_cd == "t8407":
            return {"t8407OutBlock1": t8407_rows}
        raise AssertionError(f"예상 밖 TR: {tr_cd}")

    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    return b, calls


def test_ls_scan_batch_limit_check(monkeypatch):
    page = {"t1488OutBlock1": [
        {"shcode": "123450", "hname": "잠김상한", "price": 1300, "diff": "030.00",
         "offerrem": 0},
        {"shcode": "067310", "hname": "준상한", "price": 1290, "diff": "029.20",
         "offerrem": 4000},
        {"shcode": "005930", "hname": "임계미달", "price": 70000, "diff": "010.00",
         "offerrem": 1},
    ], "t1488OutBlock": {"idx": 0}}
    b, calls = _ls(monkeypatch, [page],
                   [{"shcode": "123450", "uplmtprice": 1300},
                    {"shcode": "067310", "uplmtprice": 1300}])
    rows = b.scan_close_surge(20.0)
    assert [r["symbol"] for r in rows] == ["123450", "067310"]
    assert rows[0]["is_limit_up"] is True and rows[1]["is_limit_up"] is False
    # t8407 배치 1콜 — 코드 연결 문자열(50종목/콜 스펙 내).
    t84 = [c for c in calls if c[1] == "t8407"]
    assert len(t84) == 1
    assert t84[0][2]["t8407InBlock"] == {"nrec": 2, "shcode": "123450067310"}
    assert t84[0][0] == "/stock/market-data"


def test_ls_scan_pagination_stops_below_threshold(monkeypatch):
    page1 = {"t1488OutBlock1": [
        {"shcode": "111110", "hname": "A", "price": 100, "diff": "030.00", "offerrem": 0},
    ], "t1488OutBlock": {"idx": 7}}
    page2 = {"t1488OutBlock1": [
        {"shcode": "222220", "hname": "B", "price": 100, "diff": "015.00", "offerrem": 0},
    ], "t1488OutBlock": {"idx": 9}}
    b, calls = _ls(monkeypatch, [page1, page2], [{"shcode": "111110", "uplmtprice": 100}])
    rows = b.scan_close_surge(20.0)
    assert [r["symbol"] for r in rows] == ["111110"]
    t1488 = [c for c in calls if c[1] == "t1488"]
    assert len(t1488) == 2                       # 2페이지째 임계 미달 → 중단(3페이지 없음)
    assert t1488[1][2]["t1488InBlock"]["idx"] == 7   # 연속조회 idx 전달
