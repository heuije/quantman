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


# ── F5: 해외선물 resolver ─────────────────────────────────────────────────────
from localapp import ls_futures_contracts as lfc


class _MasterBroker:
    """resolver용 더블 — overseas_futures_master만 제공."""
    def __init__(self, rows): self._rows = rows
    def overseas_futures_master(self): return self._rows


def test_resolve_overseas_front_month():
    rows = [
        {"Symbol": "CLK26", "BscGdsCd": "CL", "ExchCd": "CME", "CtrtPrAmt": "1000"},
        {"Symbol": "CLM26", "BscGdsCd": "CL", "ExchCd": "CME", "CtrtPrAmt": "1000"},
        {"Symbol": "CLZ30", "BscGdsCd": "CL", "ExchCd": "CME", "CtrtPrAmt": "1000"},
    ]
    r = lfc.LsContractResolver(_MasterBroker(rows))
    code = r.resolve("원유선물")
    assert code is not None and code.startswith("CL")


def test_dataset_for_code_overseas():
    r = lfc.LsContractResolver(_MasterBroker([]))
    assert r.dataset_for_code("CLM26") == "원유선물"
    assert r.dataset_for_code("GCM26") == "금선물"
    assert r.dataset_for_code("101V6000") == "코스피200선물"
    assert r.dataset_for_code("ZZZ99") is None


def test_resolve_overseas_unknown_root_none():
    r = lfc.LsContractResolver(_MasterBroker([{"Symbol": "ESM26", "BscGdsCd": "ES", "ExchCd": "CME"}]))
    assert r.resolve("원유선물") is None
