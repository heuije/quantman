"""LS 선물 계약 resolver — t8467 마스터 파싱→근월물 코드·역매핑.
형식(shcode A01…·hname YYMM "F 2606"·스프레드 D01…/SP)은 2026-06-22 모의 실측 확정."""
from __future__ import annotations
import sys, datetime
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

_MASTER = [
    {"shcode": "A0166000", "expcode": "KR4A01660005", "hname": "F 2606"},      # 2026-06 (실측 형식)
    {"shcode": "A0169000", "expcode": "KR4A01690002", "hname": "F 2609"},      # 2026-09
    {"shcode": "D01696CS", "expcode": "KR4D016000", "hname": "F SP 09-2612"},  # 스프레드(SP) 제외
]

def test_resolve_picks_front_month():
    from localapp.ls_futures_contracts import _pick_front_kospi200
    assert _pick_front_kospi200(_MASTER, datetime.date(2026, 5, 1)) == "A0166000"

def test_resolve_skips_spread_and_expired():
    from localapp.ls_futures_contracts import _pick_front_kospi200
    assert _pick_front_kospi200(_MASTER, datetime.date(2026, 7, 1)) == "A0169000"

def test_dataset_for_code_reverse():
    from localapp.ls_futures_contracts import LsContractResolver
    assert LsContractResolver.dataset_for_code_static("A0166000") == "코스피200선물"
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
    assert r.dataset_for_code("A0166000") == "코스피200선물"
    assert r.dataset_for_code("ZZZ99") is None


def test_resolve_overseas_unknown_root_none():
    r = lfc.LsContractResolver(_MasterBroker([{"Symbol": "ESM26", "BscGdsCd": "ES", "ExchCd": "CME"}]))
    assert r.resolve("원유선물") is None


# ── 미니 코스피200선물(A05) 일반화 ─────────────────────────────────────────────
# 정규(A01)와 미니(A05)가 같은 t8467 마스터에 동시 수록된다는 전제(2026-06 실측 형식).
# 정규 LS 해석은 byte-identical로 보존되고, 미니가 옆에 ADD 된다.
_MASTER_BOTH = [
    {"shcode": "A0166000", "expcode": "KR4A01660005", "hname": "F 2606"},      # 정규 2026-06
    {"shcode": "A0169000", "expcode": "KR4A01690002", "hname": "F 2609"},      # 정규 2026-09
    {"shcode": "A0566000", "expcode": "KR4A05660003", "hname": "미니 F 2606"},   # 미니 2026-06
    {"shcode": "A0569000", "expcode": "KR4A05690000", "hname": "미니 F 2609"},   # 미니 2026-09
    {"shcode": "D01696CS", "expcode": "KR4D016000", "hname": "F SP 09-2612"},   # 스프레드(SP) 제외
]


class _DomesticMasterBroker:
    """resolver용 더블 — index_futures_master(국내 t8467)만 제공."""
    def __init__(self, rows): self._rows = rows
    def index_futures_master(self): return self._rows


def test_resolve_regular_unchanged_with_mini_present():
    """정규 해석은 미니가 같은 마스터에 있어도 A01 근월물로 byte-identical 보존(INVARIANT)."""
    r = lfc.LsContractResolver(_DomesticMasterBroker(_MASTER_BOTH))
    code = r.resolve("코스피200선물")
    assert code is not None and code.startswith("A01")


def test_resolve_mini_picks_a05():
    """미니 해석은 A05 근월물 shcode를 고른다(스프레드·정규 A01 제외)."""
    r = lfc.LsContractResolver(_DomesticMasterBroker(_MASTER_BOTH))
    code = r.resolve("미니코스피200선물")
    assert code is not None and code.startswith("A05")


def test_pick_front_filters_prefix_per_product():
    """_pick_front_kospi200이 상품별 prefix(A01 정규·A05 미니)로 필터한다(SP 제외)."""
    today = datetime.date(2026, 7, 1)   # 2606 만기경과 → 2609 근월물
    assert lfc._pick_front_kospi200(_MASTER_BOTH, today) == "A0169000"
    assert lfc._pick_front_kospi200(_MASTER_BOTH, today, "미니코스피200선물") == "A0569000"


def test_dataset_for_code_splits_a01_a05():
    """역매핑: A01…→정규·A05…→미니. I-2(A05 라이브 포지션이 None으로 ledger 누수)를 닫는다."""
    assert lfc.LsContractResolver.dataset_for_code_static("A0166000") == "코스피200선물"
    assert lfc.LsContractResolver.dataset_for_code_static("A0566000") == "미니코스피200선물"


def test_resolve_expiry_mini_accepted():
    """resolve_expiry가 미니도 받아 같은 2번째 목요일 만기를 반환한다(정규와 동일 규칙)."""
    r = lfc.LsContractResolver(_DomesticMasterBroker(_MASTER_BOTH))
    code, exp = r.resolve_expiry("미니코스피200선물")
    assert code is not None and code.startswith("A05")
    assert exp is not None and exp.weekday() == 3   # 목요일
