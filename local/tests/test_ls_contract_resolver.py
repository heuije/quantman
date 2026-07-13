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


def test_dataset_for_code_krx_balance_form():
    # LS 잔고(t0441)는 KRX 상품코드형("101T9000")으로 보고 — shcode(A01…)와 다른 코드공간.
    # 미인식 → 조용한 정규화 실패 → reconcile 원장 오삭제가 2026-07 분기 인시던트
    # (core dataset_for_contract 위임으로 해소).
    from localapp.ls_futures_contracts import LsContractResolver
    assert LsContractResolver.dataset_for_code_static("101T9000") == "코스피200선물"
    assert LsContractResolver.dataset_for_code_static("101V6000") == "코스피200선물"
    assert LsContractResolver.dataset_for_code_static("105T9000") == "미니코스피200선물"
    assert LsContractResolver.dataset_for_code_static("201T9000") is None   # 옵션 — 미등록
    assert LsContractResolver.dataset_for_code_static("") is None


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
# 미니(A05)는 t8467이 아니라 파생종목마스터 t8435 gubun="MF"로 분리 제공된다(2026-07-13 모의
# 실측: shcode "A05…"·hname "MF YYMM"). index_futures_master가 t8467(A01)+t8435(MF)를 병합
# 하므로, 이 픽스처는 그 병합 결과(A01 정규 + A05 미니)를 재현해 resolver 로직을 잠근다.
# 정규 LS 해석은 byte-identical로 보존되고, 미니가 옆에 ADD 된다.
_MASTER_BOTH = [
    {"shcode": "A0166000", "expcode": "KR4A01660005", "hname": "F 2606"},      # 정규 2026-06
    {"shcode": "A0169000", "expcode": "KR4A01690002", "hname": "F 2609"},      # 정규 2026-09
    {"shcode": "A0566000", "expcode": "KR4A05660003", "hname": "MF 2606"},     # 미니 2026-06(실측 형식)
    {"shcode": "A0569000", "expcode": "KR4A05690000", "hname": "MF 2609"},     # 미니 2026-09(실측 형식)
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


# ── 코스닥150선물(A06) 일반화 — core _DOMESTIC_SPEC 파생으로 자동 지원 ──────────
# LS 코스닥150선물 마스터 = 파생종목마스터 t8435 gubun="SF"(2026-07-13 모의 실측 확정):
#   shcode "A06…"(예 A0669000)·hname "KQF YYMM"·expcode "KR4A06…". 지수선물마스터 t8467엔 없다
#   (LS 공식: t8467 gubun은 V=변동성/S=섹터/그외=코스피200 — 코스닥150 값 자체가 없음). 그래서
#   index_futures_master가 t8467(코스피200)+t8435(SF 코스닥150)를 병합한다(ls_futures_broker.py).
#   이 픽스처는 그 병합 결과 형태(A01 정규 + A06 코스닥150)를 재현해 resolver 로직을 잠근다.
#   prefix "A06"·YYMM 파싱("KQF 2609"→26·09)은 상품 무관 그대로 작동.
_MASTER_KQ = [
    {"shcode": "A0166000", "expcode": "KR4A01660005", "hname": "F 2606"},        # 정규 2026-06
    {"shcode": "A0169000", "expcode": "KR4A01690002", "hname": "F 2609"},        # 정규 2026-09
    {"shcode": "A0666000", "expcode": "KR4A06660008", "hname": "KQF 2606"},    # KQ150 2026-06(실측 형식)
    {"shcode": "A0669000", "expcode": "KR4A06690007", "hname": "KQF 2609"},    # KQ150 2026-09(실측 형식)
    {"shcode": "D06696CS", "expcode": "KR4D06696CS7", "hname": "KQF SP 09-2612"},  # 스프레드(SP) 제외
]


def test_resolve_regular_unchanged_with_kq150_present():
    """정규 해석은 KQ150이 같은 마스터에 있어도 A01 근월물로 byte-identical 보존(INVARIANT)."""
    r = lfc.LsContractResolver(_DomesticMasterBroker(_MASTER_KQ))
    code = r.resolve("코스피200선물")
    assert code is not None and code.startswith("A01")


def test_resolve_kq150_picks_a06():
    """KQ150 해석은 A06 근월물 shcode를 고른다(스프레드·정규 A01 제외)."""
    today = datetime.date(2026, 7, 1)   # 2606 만기경과 → 2609 근월물
    assert lfc._pick_front_kospi200(_MASTER_KQ, today, "코스닥150선물") == "A0669000"


def test_dataset_for_code_kq150_short_and_krx():
    """역매핑: A06…→KQ150(shcode), 106…→KQ150(KRX 잔고형·core 위임). 기존 A01/101 불변."""
    assert lfc.LsContractResolver.dataset_for_code_static("A0666000") == "코스닥150선물"
    assert lfc.LsContractResolver.dataset_for_code_static("106T9000") == "코스닥150선물"
    assert lfc.LsContractResolver.dataset_for_code_static("A0166000") == "코스피200선물"
    assert lfc.LsContractResolver.dataset_for_code_static("101T9000") == "코스피200선물"


def test_resolve_expiry_kq150_second_thursday():
    """resolve_expiry가 KQ150도 받아 2번째 목요일 만기를 반환한다."""
    r = lfc.LsContractResolver(_DomesticMasterBroker(_MASTER_KQ))
    code, exp = r.resolve_expiry("코스닥150선물")
    assert code is not None and code.startswith("A06")
    assert exp is not None and exp.weekday() == 3   # 목요일


# ── LsFuturesBroker.index_futures_master: t8467 + t8435(SF) 병합 (실 TR 배선) ──
# 실측(2026-07-13 모의): t8467은 코스피200 정규(A01)만, 코스닥150은 t8435 gubun="SF"(A06)로
# 분리 제공 → 브로커가 둘을 병합해야 resolver가 코스닥150을 본다. t8435는 additive·non-fatal.
def _broker_with_post(fake_post):
    from localapp.ls_futures_broker import LsFuturesBroker
    br = LsFuturesBroker.__new__(LsFuturesBroker)   # __init__(자격증명/네트워크) 우회
    br._post = fake_post
    return br


def test_index_master_merges_t8467_and_t8435_sf():
    """index_futures_master가 t8467(코스피200)+t8435 SF(코스닥150)를 병합한다."""
    seen = []

    def fake_post(path, tr, body):
        seen.append((tr, body.get(f"{tr}InBlock", {}).get("gubun")))
        if tr == "t8467":
            return {"t8467OutBlock": [{"shcode": "A0169000", "hname": "F 2609",
                                       "expcode": "KR4A01690002"}]}
        if tr == "t8435":
            return {"t8435OutBlock": [{"shcode": "A0669000", "hname": "KQF 2609",
                                       "expcode": "KR4A06690007"}]}
        return {}

    rows = _broker_with_post(fake_post).index_futures_master()
    shcodes = {r["shcode"] for r in rows}
    assert "A0169000" in shcodes and "A0669000" in shcodes   # 코스피200 + 코스닥150
    assert ("t8467", "") in seen and ("t8435", "SF") in seen


def test_index_master_t8435_failure_preserves_kospi200():
    """t8435(코스닥150) 조회 실패해도 예외 전파 없이 코스피200(t8467)은 보존 — 기존 유저 무영향."""
    def fake_post(path, tr, body):
        if tr == "t8467":
            return {"t8467OutBlock": [{"shcode": "A0169000", "hname": "F 2609",
                                       "expcode": "KR4A01690002"}]}
        raise RuntimeError("t8435 down")

    rows = _broker_with_post(fake_post).index_futures_master()
    assert [r["shcode"] for r in rows] == ["A0169000"]   # 코스피200 정규 보존


def test_index_master_merges_t8467_and_t8435_mf():
    """index_futures_master가 t8467(코스피200)+t8435 MF(미니 코스피200 A05)를 병합한다.

    2026-07-13 모의 실측: 미니는 t8467에 없고 파생종목마스터 t8435 gubun="MF"로 분리 제공
    (shcode "A05…"·hname "MF YYMM"). 병합 안 하면 리졸버가 미니를 못 봐 조용한 무발주."""
    seen = []

    def fake_post(path, tr, body):
        gubun = body.get(f"{tr}InBlock", {}).get("gubun")
        seen.append((tr, gubun))
        if tr == "t8467":
            return {"t8467OutBlock": [{"shcode": "A0169000", "hname": "F 2609",
                                       "expcode": "KR4A01690002"}]}
        if tr == "t8435" and gubun == "MF":
            return {"t8435OutBlock": [{"shcode": "A0569000", "hname": "MF 2609",
                                       "expcode": "KR4A05690000"}]}
        return {}

    rows = _broker_with_post(fake_post).index_futures_master()
    shcodes = {r["shcode"] for r in rows}
    assert "A0169000" in shcodes and "A0569000" in shcodes   # 코스피200 + 미니
    assert ("t8467", "") in seen and ("t8435", "MF") in seen


def test_index_master_per_product_t8435_failure_isolated():
    """t8435 한 상품(SF) 실패가 다른 상품(MF)·코스피200을 막지 않는다 — 상품별 독립 non-fatal.

    상품별 try를 하나로 합치면 SF 실패가 MF까지 건너뛰어 미니가 조용히 재차 깨진다 — 그 회귀 차단."""
    def fake_post(path, tr, body):
        gubun = body.get(f"{tr}InBlock", {}).get("gubun")
        if tr == "t8467":
            return {"t8467OutBlock": [{"shcode": "A0169000", "hname": "F 2609",
                                       "expcode": "KR4A01690002"}]}
        if tr == "t8435" and gubun == "SF":
            raise RuntimeError("t8435 SF down")
        if tr == "t8435" and gubun == "MF":
            return {"t8435OutBlock": [{"shcode": "A0569000", "hname": "MF 2609",
                                       "expcode": "KR4A05690000"}]}
        return {}

    shcodes = {r["shcode"] for r in _broker_with_post(fake_post).index_futures_master()}
    assert "A0169000" in shcodes and "A0569000" in shcodes   # 코스피200·미니 보존
    assert "A0669000" not in shcodes                          # 실패한 코스닥150만 누락
