"""estimate_kr feed — FnGuide getSnpFinancial(JSON) 파싱·forward 뷰 단위 테스트(네트워크 무의존)."""
from __future__ import annotations

import pandas as pd

from quant_core.data.feeds import estimate_kr


def _hdr(yymm, ep, cd):
    return {"YYMM": yymm, "EP_CHK": ep, "CD": cd, "NO_TYP": "IFRS", "CONSOL_TYPE": "연결"}


def _row(seq, name, vals):
    d = {"SEQ": seq, "NAME": name, "UNIT": None, "DIGIT": 0, "FORMULA": "", "LVL": 1}
    for i, v in enumerate(vals, 1):
        d[f"VAL{i}"] = None if v is None else str(v)
    return d


# getSnpFinancial dataset 구조 픽스처 — 확정 2년 + 추정 2년(E). 단위: 손익 억원·EPS 원·PER 배.
# 의도적 함정: '영업이익'(발표기준 아님·forward None)·'당기순이익(비지배)'가 정확일치로 걸러지는지 검증.
_HEADER = [
    _hdr("2023/12", " ", "VAL1"),
    _hdr("2024/12", " ", "VAL2"),
    _hdr("2025/12", "E", "VAL3"),
    _hdr("2026/12", "E", "VAL4"),
]
_DATA = [
    _row(1, "매출액", [2589355, 3008709, 3200000, 3500000]),
    _row(2, "영업이익", [65670, 329724, None, None]),              # 함정: forward 비어있음 → op에 쓰면 안 됨
    _row(3, "영업이익(발표기준)", [65670, 329724, 400000, 500000]),   # op(정답: forward 존재)
    _row(4, "당기순이익", [154871, 340000, 420000, 520000]),        # ni
    _row(5, "  당기순이익(지배)", [150000, 335000, 410000, 510000]),  # controlling_ni(앞 공백=LVL2)
    _row(6, "  당기순이익(비지배)", [4871, 5000, 10000, 10000]),      # 함정: 매핑 안 됨
    _row(7, "EPS", [2131, 5000, 6200, 7700]),
    _row(8, "PER", [36.83, 10.50, 9.00, 7.20]),
    _row(9, "PBR", [1.50, 1.40, 1.30, 1.20]),
    _row(10, "ROE", [4.10, 9.50, 11.00, 12.50]),
]
_PAYLOAD = {"dataset": {"header": _HEADER, "data": _DATA}}


def test_parse_years_and_estimate_flags():
    p = estimate_kr.parse_financial(_PAYLOAD)
    assert p["years"] == ["2023/12", "2024/12", "2025/12", "2026/12"]
    assert p["is_estimate"] == [False, False, True, True]           # EP_CHK 'E' → 추정 플래그


def test_parse_metrics_maxrow_and_computed_margin():
    m = estimate_kr.parse_financial(_PAYLOAD)["metrics"]
    assert m["rev"] == [2589355.0, 3008709.0, 3200000.0, 3500000.0]
    assert m["eps"] == [2131.0, 5000.0, 6200.0, 7700.0]
    assert m["per"][1] == 10.50
    # op = 값이 더 많은 행 채택 → '영업이익(발표기준)'(4값·forward 포함), 함정 '영업이익'(2값)은 버림.
    assert m["op"] == [65670.0, 329724.0, 400000.0, 500000.0]
    # 정확일치: '당기순이익(지배)'→controlling_ni, '당기순이익(비지배)'는 매핑 없음(무시).
    assert m["controlling_ni"] == [150000.0, 335000.0, 410000.0, 510000.0]
    assert m["ni"] == [154871.0, 340000.0, 420000.0, 520000.0]
    # op_margin·net_margin은 매출액 대비 계산(발표기준 op·total ni 기준·1소수점 반올림).
    assert m["op_margin"] == [2.5, 11.0, 12.5, 14.3]
    assert m["net_margin"] == [6.0, 11.3, 13.1, 14.9]


def test_to_frame_shape():
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    assert list(df.index) == ["2023/12", "2024/12", "2025/12", "2026/12"]
    assert "is_estimate" in df.columns
    assert df.loc["2026/12", "is_estimate"] is True or bool(df.loc["2026/12", "is_estimate"])
    assert {"rev", "op", "ni", "eps", "per", "pbr", "roe", "op_margin"}.issubset(df.columns)


def test_forward_view_growth_and_fwd_pe():
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    fv = estimate_kr.forward_view(df, last_price=80000.0)
    # 최근 확정=2024/12, 가장 가까운 추정=2025/12
    assert fv["fiscal_actual"] == "2024/12"
    assert fv["fiscal_forward"] == "2025/12"
    assert fv["rev_growth"] == round((3200000 - 3008709) / 3008709 * 100, 1)
    assert fv["op_growth"] == round((400000 - 329724) / 329724 * 100, 1)   # 발표기준 op 성장률
    assert fv["eps_forward"] == 6200.0
    assert fv["forward_pe"] == round(80000.0 / 6200.0, 2)                  # 현재가/추정EPS
    assert fv["op_margin_forward"] == 12.50


def test_forward_view_no_price_omits_fwd_pe():
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    fv = estimate_kr.forward_view(df, last_price=None)
    assert "forward_pe" not in fv            # 가격 없으면 생략(가짜 0 금지)
    assert fv["eps_forward"] == 6200.0


def test_annual_view_multiyear_table():
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    av = estimate_kr.annual_view(df, max_years=6)
    assert av["years"] == ["2023/12", "2024/12", "2025/12", "2026/12"]
    assert av["is_estimate"] == [False, False, True, True]
    assert av["rev"] == [2589355.0, 3008709.0, 3200000.0, 3500000.0]
    assert av["op"] == [65670.0, 329724.0, 400000.0, 500000.0]
    assert av["eps"][-1] == 7700.0


def test_annual_view_empty():
    assert estimate_kr.annual_view(pd.DataFrame()) == {}


def test_estimate_block_composes(monkeypatch):
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    monkeypatch.setattr(estimate_kr, "get", lambda code: df)   # on-demand 심볼뷰 위임
    blk = estimate_kr.estimate_block("005930", last_price=80000.0)
    assert blk["source"] == "FnGuide 컨센서스"
    assert blk["forward"]["forward_pe"] == round(80000.0 / 6200.0, 2)
    assert blk["annual"]["years"][-1] == "2026/12"


def test_estimate_block_empty_when_no_data(monkeypatch):
    monkeypatch.setattr(estimate_kr, "get", lambda code: None)
    assert estimate_kr.estimate_block("000000", last_price=100.0) == {}


def test_get_uses_fresh_cache(monkeypatch, tmp_path):
    """신선 저장본이 있으면 fetch 없이 캐시 반환(on-demand 심볼뷰)."""
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    p = tmp_path / "005930.parquet"
    df.to_parquet(p)
    monkeypatch.setattr(estimate_kr, "_path", lambda code: p)
    called = {"n": 0}
    monkeypatch.setattr(estimate_kr, "refresh", lambda c: called.__setitem__("n", called["n"] + 1) or pd.DataFrame())
    got = estimate_kr.get("005930")
    assert got is not None and not got.empty
    assert called["n"] == 0                     # 신선 캐시 → fetch 건너뜀


def test_get_fetches_when_missing(monkeypatch, tmp_path):
    """저장본 없으면 fetch(refresh)로 즉시 수집."""
    monkeypatch.setattr(estimate_kr, "_path", lambda code: tmp_path / "999999.parquet")
    df = estimate_kr.to_frame(estimate_kr.parse_financial(_PAYLOAD))
    monkeypatch.setattr(estimate_kr, "refresh", lambda c: df)
    got = estimate_kr.get("999999")
    assert got is not None and not got.empty


def test_parse_empty_when_no_data():
    empty = {"years": [], "is_estimate": [], "metrics": {}}
    assert estimate_kr.parse_financial({}) == empty
    assert estimate_kr.parse_financial(None) == empty
    # 실제 무데이터 응답: header YYMM 전부 null → 빈 구조(가짜 연도 금지).
    null_hdr = {"dataset": {"header": [_hdr(None, None, "VAL1"), _hdr(None, None, "VAL2")], "data": []}}
    assert estimate_kr.parse_financial(null_hdr) == empty
    assert estimate_kr.to_frame(empty).empty
    assert estimate_kr.forward_view(estimate_kr.to_frame(empty)) == {}
