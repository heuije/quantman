"""estimate_kr feed — FnGuide highlight 파싱·forward 뷰 단위 테스트(네트워크 무의존)."""
from __future__ import annotations

import pandas as pd

from quant_core.data.feeds import estimate_kr

# FnGuide highlight_D_Y 구조 픽스처 — 확정 2년 + 추정 2년(E). 단위: 손익 억원·EPS 원·PER 배.
_HTML = """
<html><body>
<div id="highlight_D_Y"><table>
  <thead><tr>
    <th>IFRS(연결)</th><th>2023/12</th><th>2024/12</th><th>2025/12(E)</th><th>2026/12(E)</th>
  </tr></thead>
  <tbody>
    <tr><th>매출액</th><td>2,589,355</td><td>3,008,709</td><td>3,200,000</td><td>3,500,000</td></tr>
    <tr><th>영업이익</th><td>65,670</td><td>329,724</td><td>400,000</td><td>500,000</td></tr>
    <tr><th>당기순이익</th><td>154,871</td><td>340,000</td><td>420,000</td><td>520,000</td></tr>
    <tr><th>지배주주순이익</th><td>150,000</td><td>335,000</td><td>410,000</td><td>510,000</td></tr>
    <tr><th>EPS(원)</th><td>2,131</td><td>5,000</td><td>6,200</td><td>7,700</td></tr>
    <tr><th>PER</th><td>36.83</td><td>10.50</td><td>9.00</td><td>7.20</td></tr>
    <tr><th>PBR</th><td>1.50</td><td>1.40</td><td>1.30</td><td>1.20</td></tr>
    <tr><th>ROE</th><td>4.10</td><td>9.50</td><td>11.00</td><td>12.50</td></tr>
  </tbody>
</table></div>
</body></html>
"""


def test_parse_years_and_estimate_flags():
    p = estimate_kr.parse_highlight(_HTML)
    assert p["years"] == ["2023/12", "2024/12", "2025/12", "2026/12"]   # (E) 접미는 플래그로 분리
    assert p["is_estimate"] == [False, False, True, True]


def test_parse_metrics_and_margins():
    p = estimate_kr.parse_highlight(_HTML)
    m = p["metrics"]
    assert m["rev"] == [2589355.0, 3008709.0, 3200000.0, 3500000.0]
    assert m["eps"] == [2131.0, 5000.0, 6200.0, 7700.0]
    assert m["per"][1] == 10.50
    # 영업이익률 = 영업이익/매출액*100 (1회 계산 삽입)
    assert m["op_margin"][1] == round(329724 / 3008709 * 100, 1)
    assert "controlling_ni" in m


def test_to_frame_shape():
    df = estimate_kr.to_frame(estimate_kr.parse_highlight(_HTML))
    assert list(df.index) == ["2023/12", "2024/12", "2025/12", "2026/12"]
    assert "is_estimate" in df.columns
    assert df.loc["2026/12", "is_estimate"] is True or bool(df.loc["2026/12", "is_estimate"])
    assert set(["rev", "op", "ni", "eps", "per", "pbr", "roe"]).issubset(df.columns)


def test_forward_view_growth_and_fwd_pe():
    df = estimate_kr.to_frame(estimate_kr.parse_highlight(_HTML))
    fv = estimate_kr.forward_view(df, last_price=80000.0)
    # 최근 확정=2024/12, 가장 가까운 추정=2025/12
    assert fv["fiscal_actual"] == "2024/12"
    assert fv["fiscal_forward"] == "2025/12"
    assert fv["rev_growth"] == round((3200000 - 3008709) / 3008709 * 100, 1)
    assert fv["eps_forward"] == 6200.0
    assert fv["forward_pe"] == round(80000.0 / 6200.0, 2)   # 현재가/추정EPS


def test_forward_view_no_price_omits_fwd_pe():
    df = estimate_kr.to_frame(estimate_kr.parse_highlight(_HTML))
    fv = estimate_kr.forward_view(df, last_price=None)
    assert "forward_pe" not in fv            # 가격 없으면 생략(가짜 0 금지)
    assert fv["eps_forward"] == 6200.0


def test_annual_view_multiyear_table():
    df = estimate_kr.to_frame(estimate_kr.parse_highlight(_HTML))
    av = estimate_kr.annual_view(df, max_years=6)
    assert av["years"] == ["2023/12", "2024/12", "2025/12", "2026/12"]
    assert av["is_estimate"] == [False, False, True, True]
    assert av["rev"] == [2589355.0, 3008709.0, 3200000.0, 3500000.0]
    assert av["eps"][-1] == 7700.0


def test_annual_view_empty():
    assert estimate_kr.annual_view(pd.DataFrame()) == {}


def test_parse_empty_when_no_div():
    p = estimate_kr.parse_highlight("<html><body>no highlight</body></html>")
    assert p == {"years": [], "is_estimate": [], "metrics": {}}
    assert estimate_kr.to_frame(p).empty
    assert estimate_kr.forward_view(estimate_kr.to_frame(p)) == {}
