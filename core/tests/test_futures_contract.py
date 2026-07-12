"""심볼(한글 상품명) → 라이브 계약코드 해석 — 순수 resolver 단위검증(네트워크 없음).

M2: 백테스트/전략의 연속물 키("코스피200선물"·"금선물")를 라이브 주문용 특정 만기
계약코드(국내 A01606·해외 globex GCM26)로 변환. 마스터 텍스트는 인자(다운로드는 호출부).

    cd platform/core && python -m pytest tests/test_futures_contract.py -q
"""
from __future__ import annotations

from datetime import date

from quant_core.futures_contract import (
    OVERSEAS_ROOTS,
    dataset_for_contract,
    front_contract,
    futures_market,
    parse_front_month_domestic,
    parse_front_month_overseas,
    resolve_contract,
)

# ── 국내 fo_idx_code.mst 표본 (cp949 decode 후, server 테스트와 동일 형식) ──────────
_DOM = "\n".join([
    "1A01606   KR4A01660005F 202606                  00000.0012001     KOSPI200",
    "1A01609   KR4A01690002F 202609                  00000.0022001     KOSPI200",
    "1A01612   KR4A016C0004F 202612                  00000.0032001     KOSPI200",
    "BA05606   KR4A05660001미니F 202606              00000.0012001     KOSPI200",   # 미니 제외
    "2A02606   KR4A02660003C 202606  300.0           00000.0012001     KOSPI200",   # 옵션 제외
])

# ── 해외 ffcode.mst 표본 (실제 컬럼 형식: code·flags·name-YYYYMM·CME·root·…·승수·…) ──
_OV = "\n".join([
    "GCM26       YYYN       Gold-202606            CME       GC        006    1   -1    0.1    10    100    10    1000NN53",
    "GCQ26       YYYN       Gold-202608            CME       GC        006    1   -1    0.1    10    100    10    1000NN53",
    "MGCM26      YYYN       Micro Gold-202606      CME       MGC       006    1   -1    0.1    10    10     10    1000NN53",  # 마이크로 제외(root)
    "GCM26-Q26   YYYN       Gold-2606-2608         CME       GCGC      006    1   -1    0.1    10    100    10    1000YY53",  # 스프레드 제외(code-)
    "GCTQ26      YYYN       (TAS)Gold-202608       CME       GCT       006    1   -1    0.1    10    100    10    1011NN53",  # TAS 제외(root)
    "CLN26       YYYN       Crude Oil-202607       CME       CL        007    2   -2    0.01   10    1000   10    10011NN51",
    "CLQ26       YYYN       Crude Oil-202608       CME       CL        007    2   -2    0.01   10    1000   10    10011NN51",
    "BTCM26      NNNN       Bitcoin Futures-202606    CME       BTC       001    0    0    5      25    5      10    0.211NN50",
])


# ── 국내 ──────────────────────────────────────────────────────────────────────
def test_domestic_front_before_expiry():
    assert parse_front_month_domestic(_DOM, date(2026, 6, 7)) == "A01606"


def test_domestic_front_rolls_after_expiry():
    # 6/11 만기 경과 → 9월물
    assert parse_front_month_domestic(_DOM, date(2026, 6, 12)) == "A01609"


def test_domestic_excludes_mini_and_options():
    code = parse_front_month_domestic(_DOM, date(2026, 6, 7))
    assert code == "A01606" and not code.startswith(("A05", "A02"))


def test_domestic_none_when_all_expired():
    assert parse_front_month_domestic(_DOM, date(2027, 1, 1)) is None


# ── 해외 ──────────────────────────────────────────────────────────────────────
def test_overseas_front_nearest():
    assert parse_front_month_overseas(_OV, date(2026, 6, 7), "GC", 100.0) == "GCM26"


def test_overseas_rolls_to_next_month():
    # today_ym 202607 > 202606 → GCM26 제외, GCQ26(202608)
    assert parse_front_month_overseas(_OV, date(2026, 7, 1), "GC", 100.0) == "GCQ26"


def test_overseas_excludes_micro_spread_tas():
    # GC root 정확일치라 MGC(마이크로)·GCGC(스프레드)·GCT(TAS) 모두 제외, 풀계약만
    code = parse_front_month_overseas(_OV, date(2026, 6, 7), "GC", 100.0)
    assert code == "GCM26"


def test_overseas_crude_oil():
    assert parse_front_month_overseas(_OV, date(2026, 6, 7), "CL", 1000.0) == "CLN26"


def test_overseas_multiplier_validation_rejects_wrong_mult():
    # GC인데 승수 999 기대 → 일치 라인 없음 → None (parse 오류 안전장치)
    assert parse_front_month_overseas(_OV, date(2026, 6, 7), "GC", 999.0) is None


def test_overseas_unknown_root_none():
    assert parse_front_month_overseas(_OV, date(2026, 6, 7), "ZZ", 1.0) is None


# ── dispatch + market ─────────────────────────────────────────────────────────
def test_resolve_equity_passthrough():
    # 주식은 심볼이 곧 거래코드 — 무변환
    assert resolve_contract("005930", date(2026, 6, 7)) == "005930"
    assert resolve_contract("AAPL", date(2026, 6, 7)) == "AAPL"


def test_resolve_domestic_futures():
    # 만기(6/11)에서 충분히 떨어진 날 → 근월물 A01606
    assert resolve_contract("코스피200선물", date(2026, 6, 1), domestic_master=_DOM) == "A01606"


def test_resolve_domestic_rolls_within_lead():
    """진입 resolver는 만기 lead(5일) 안이면 차월물로 롤 — 만기 임박 계약 진입 회피.

    백스톱(_expiry_close_reason)이 보유분을 만기 5일 전 청산하는 것과 대칭. 이게 없으면
    '사자마자 백스톱이 롤청산'하는 만기임박 진입이 발생(라이브 sharp-edge)."""
    # 6/11 만기 — 6/7·6/10·6/11(만기당일) 모두 lead(5일) 안이라 9월물로 롤
    assert resolve_contract("코스피200선물", date(2026, 6, 7), domestic_master=_DOM) == "A01609"
    assert resolve_contract("코스피200선물", date(2026, 6, 10), domestic_master=_DOM) == "A01609"
    assert resolve_contract("코스피200선물", date(2026, 6, 11), domestic_master=_DOM) == "A01609"
    # front_contract도 같은 계약 + 그 만기(9/10) 반환(주문 라우팅·원장 기록 정합)
    code, exp = front_contract("코스피200선물", date(2026, 6, 10), domestic_master=_DOM)
    assert code == "A01609" and exp == date(2026, 9, 10)


def test_parse_front_domestic_lead_param():
    """lead_days 파라미터 직접 검증 — lead=0은 만기당일까지 유지(하위호환)."""
    assert parse_front_month_domestic(_DOM, date(2026, 6, 7)) == "A01606"          # 기본 lead=0
    assert parse_front_month_domestic(_DOM, date(2026, 6, 7), 5) == "A01609"        # lead=5 → 롤
    assert parse_front_month_domestic(_DOM, date(2026, 6, 11)) == "A01606"          # 만기당일·lead0 → 유지


def test_resolve_overseas_futures():
    assert resolve_contract("금선물", date(2026, 6, 7), overseas_master=_OV) == "GCM26"
    assert resolve_contract("원유선물", date(2026, 6, 7), overseas_master=_OV) == "CLN26"
    assert resolve_contract("비트코인선물", date(2026, 6, 7), overseas_master=_OV) == "BTCM26"


def test_resolve_futures_without_master_none():
    assert resolve_contract("코스피200선물", date(2026, 6, 7)) is None
    assert resolve_contract("금선물", date(2026, 6, 7)) is None


def test_overseas_roots_cover_catalog():
    # 카탈로그 6종 CME 선물이 모두 root맵에 있어야(코스피200=국내 제외)
    assert set(OVERSEAS_ROOTS) == {
        "원유선물", "천연가스선물", "금선물", "은선물(COMEX)", "나스닥선물", "비트코인선물"}


def test_futures_market_routing():
    assert futures_market("코스피200선물") == "KRX"
    assert futures_market("금선물") == "CME"
    assert futures_market("원유선물") == "CME"
    assert futures_market("005930") == ""      # 주식은 선물 market 아님


# ── dataset_for_contract — 계약코드 → 데이터셋 심볼 역매핑 (스냅샷 정규화·reconcile, M7) ──
def test_dataset_for_contract_overseas_roots():
    assert dataset_for_contract("GCM26") == "금선물"        # GC+M+26
    assert dataset_for_contract("CLN26") == "원유선물"      # CL+N+26
    assert dataset_for_contract("BTCM26") == "비트코인선물"  # BTC+M+26
    assert dataset_for_contract("NQZ25") == "나스닥선물"     # NQ+Z+25


def test_dataset_for_contract_domestic():
    assert dataset_for_contract("A01606") == "코스피200선물"
    assert dataset_for_contract("A01609") == "코스피200선물"


def test_dataset_for_contract_roundtrip_with_resolve():
    # resolve_contract의 역 — 같은 마스터로 정방향 해석한 코드를 되돌리면 원 심볼
    assert dataset_for_contract(
        resolve_contract("금선물", date(2026, 6, 7), overseas_master=_OV)) == "금선물"
    assert dataset_for_contract(
        resolve_contract("코스피200선물", date(2026, 6, 7), domestic_master=_DOM)) == "코스피200선물"


def test_dataset_for_contract_unknown_none():
    assert dataset_for_contract("") is None
    assert dataset_for_contract("005930") is None     # 주식코드는 선물 계약 아님
    assert dataset_for_contract("ZZZ99") is None       # 미등록 root


# ── front_contract — (계약코드, 만기일) 동시 해석 (M6 진입 시 ledger 기록용) ──────
def test_front_contract_domestic_code_and_expiry():
    # A01606(6월물) → 만기 = 2026-06 2번째 목요일 = 6/11. (6/1은 만기 lead 5일 밖 → 근월물 유지)
    assert front_contract("코스피200선물", date(2026, 6, 1),
                          domestic_master=_DOM) == ("A01606", date(2026, 6, 11))


def test_front_contract_overseas_metals():
    # GCM26(인도월 6월) → COMEX 금 3번째 마지막 영업일 = 6/26
    assert front_contract("금선물", date(2026, 6, 7),
                          overseas_master=_OV) == ("GCM26", date(2026, 6, 26))


def test_front_contract_overseas_crude_prior_month():
    # CLN26(인도월 7월) → 전월 6/25의 3영업일 전 = 6/22 (이름월=인도월이라 만기는 전월)
    assert front_contract("원유선물", date(2026, 6, 7),
                          overseas_master=_OV) == ("CLN26", date(2026, 6, 22))


def test_front_contract_overseas_crypto():
    # BTCM26(인도월 6월) → 마지막 금요일 = 6/26
    assert front_contract("비트코인선물", date(2026, 6, 7),
                          overseas_master=_OV) == ("BTCM26", date(2026, 6, 26))


def test_front_contract_equity_none():
    # 주식은 만기 개념 없음 → None (호출부 = Trader가 비선물엔 호출 안 함)
    assert front_contract("005930", date(2026, 6, 7)) is None
    assert front_contract("AAPL", date(2026, 6, 7)) is None


def test_front_contract_without_master_none():
    # 마스터 미수신 → None (발주 skip과 동일 — 추측 만기 금지)
    assert front_contract("코스피200선물", date(2026, 6, 7)) is None
    assert front_contract("금선물", date(2026, 6, 7)) is None


# ── 미니 코스피200선물 (Task 3: resolver 일반화 — 정규/미니 양쪽 해석) ──────────────
# 정규('1' 시작·A01)와 미니('B' 시작·A05)가 같은 마스터에 공존. 정규 해석은 byte-identical 보존,
# 미니는 ADD ALONGSIDE — root_char로 상품별 라인 필터(정규 root="1", 미니 root="B").
_DOM2 = "\n".join([
    "1A01606   KR4A01660005F 202606                  00000.0012001     KOSPI200",
    "1A01609   KR4A01690002F 202609                  00000.0022001     KOSPI200",
    "BA05606   KR4A05660001미니F 202606              00000.0012001     KOSPI200",
    "BA05609   KR4A05690008미니F 202609              00000.0022001     KOSPI200",
    "2A02606   KR4A02660003C 202606  300.0           00000.0012001     KOSPI200",
])


def test_regular_still_resolves_a01():            # 정규 회귀
    from quant_core.futures_contract import parse_front_month_domestic
    assert parse_front_month_domestic(_DOM2, date(2026, 6, 7)) == "A01606"


def test_mini_resolves_a05():                     # 미니 근월물
    # 6/5 = 6월물 만기(6/11) 6일 전 → 진입 롤 lead(5일) 밖이라 근월물 A05606 유지(롤 안 함).
    # 6/7은 lead 안이라 9월물로 롤되므로(정규 A01609와 대칭) 근월 해석 검증엔 lead 밖 날짜를 쓴다.
    assert resolve_contract("미니코스피200선물", date(2026, 6, 5), domestic_master=_DOM2) == "A05606"


def test_regular_resolves_a01_via_resolve_contract():
    # 6/5 = lead(5일) 밖 → 정규 근월물 A01606 (resolve_contract 경유). 6/7은 lead 안이라 롤(기존
    # test_resolve_domestic_rolls_within_lead가 A01609 보장) — 그 byte-identical 불변식과 충돌 안 함.
    assert resolve_contract("코스피200선물", date(2026, 6, 5), domestic_master=_DOM2) == "A01606"


def test_dataset_for_contract_splits_regular_mini():
    assert dataset_for_contract("A01606") == "코스피200선물"
    assert dataset_for_contract("A05606") == "미니코스피200선물"


# ── dataset_for_contract — KRX 상품코드형(브로커 잔고 코드공간) ─────────────────────
# LS 잔고(t0441)는 포지션을 단축코드(A01…)가 아니라 KRX 상품코드형 8자("101T9000")로
# 보고한다. 이 형태 미인식 → 라우터 정규화 조용한 실패 → reconcile이 자기 포지션을
# "외부 매도"로 오판·원장 삭제가 2026-07 원장↔브로커 분기 인시던트다.
def test_dataset_for_contract_krx_numeric_form():
    assert dataset_for_contract("101T9000") == "코스피200선물"   # LS 가이드 t0441 실측 형식
    assert dataset_for_contract("101V6000") == "코스피200선물"
    assert dataset_for_contract("105V6000") == "미니코스피200선물"


def test_dataset_for_contract_krx_form_guards():
    assert dataset_for_contract("101000") is None      # 6자 = 주식 코드공간(오매칭 방지 가드)
    assert dataset_for_contract("201T9000") is None    # 옵션(콜) 상품코드 — 미등록 → None(호출부 표면화)
    assert dataset_for_contract("106T9000") == "코스닥150선물"   # KQ150 등록(2026-07 배선). 미등록→None 가드는 위 201(옵션)이 담당
