"""P1-b — OpenDART 총차입금(td) 다계정 합산 검증.

OpenDART에 단일 '총차입금' 표준계정이 없어, 표준 차입금 라인(단기차입금·유동성장기부채·장기차입금·
사채)을 합산한다. 비차입금 부채(매입채무 등)는 제외 → EV·net_debt_ebitda가 *이자부부채* 기준이 되게.
KR feed에 td가 없어 ev_ebitda·net_debt가 NaN이던 공급 갭(감사 발견)을 닫는다.

    cd platform/core && pytest tests/test_fundamental_kr_td.py -v
"""
import pandas as pd

from quant_core.data.feeds import fundamental_kr as fk


def _fs(rows):
    return pd.DataFrame(rows)


def test_pick_sum_aggregates_interest_bearing_debt():
    """차입금·사채 다계정 합산, 비차입금(매입채무)·자본 항목 제외."""
    fs = _fs([
        {"account_id": "ifrs-full_ShorttermBorrowings", "account_nm": "단기차입금", "thstrm_amount": "1,000"},
        {"account_id": "x", "account_nm": "유동성장기부채", "thstrm_amount": "500"},
        {"account_id": "x", "account_nm": "장기차입금", "thstrm_amount": "2,000"},
        {"account_id": "x", "account_nm": "사채", "thstrm_amount": "1,500"},
        {"account_id": "x", "account_nm": "매입채무", "thstrm_amount": "9,999"},          # 비차입금 → 제외
        {"account_id": "ifrs-full_Equity", "account_nm": "자본총계", "thstrm_amount": "8,888"},  # 제외
    ])
    assert fk._pick_sum(fs, fk._DEBT_IDS, fk._DEBT_NMS) == 5000.0


def test_pick_sum_none_when_no_debt():
    """차입금 계정이 하나도 없으면 None — 0으로 가장하지 않음(정직)."""
    fs = _fs([{"account_id": "x", "account_nm": "매입채무", "thstrm_amount": "100"}])
    assert fk._pick_sum(fs, fk._DEBT_IDS, fk._DEBT_NMS) is None


def test_pick_sum_matches_by_account_id():
    """account_nm이 비표준이어도 표준 account_id로 매칭(KR filer account_id 변형 대비)."""
    fs = _fs([
        {"account_id": "ifrs-full_LongtermBorrowings", "account_nm": "장기차입금및사채", "thstrm_amount": "3,000"},
    ])
    assert fk._pick_sum(fs, fk._DEBT_IDS, fk._DEBT_NMS) == 3000.0
