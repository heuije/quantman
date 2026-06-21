"""LS 선물 계약 resolver — t8432 마스터 파싱→근월물 코드·역매핑."""
from __future__ import annotations
import sys, datetime
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

_MASTER = [
    {"shcode": "101V6000", "expcode": "KR4101V60002", "hname": "F 202406"},
    {"shcode": "101V9000", "expcode": "KR4101V90009", "hname": "F 202409"},
    {"shcode": "401V6V9SP", "expcode": "KR4401", "hname": "F SP 06-09"},  # 스프레드 제외
]

def test_resolve_picks_front_month():
    from localapp.ls_futures_contracts import _pick_front_kospi200
    assert _pick_front_kospi200(_MASTER, datetime.date(2024, 5, 1)) == "101V6000"

def test_resolve_skips_spread_and_expired():
    from localapp.ls_futures_contracts import _pick_front_kospi200
    assert _pick_front_kospi200(_MASTER, datetime.date(2024, 7, 1)) == "101V9000"

def test_dataset_for_code_reverse():
    from localapp.ls_futures_contracts import LsContractResolver
    assert LsContractResolver.dataset_for_code_static("101V6000") == "코스피200선물"
    assert LsContractResolver.dataset_for_code_static("005930") is None
