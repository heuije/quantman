"""심볼(한글 상품명) → 라이브 계약코드 해석 — 순수 resolver 단위검증(네트워크 없음).

M2: 백테스트/전략의 연속물 키("코스피200선물"·"금선물")를 라이브 주문용 특정 만기
계약코드(국내 A01606·해외 globex GCM26)로 변환. 마스터 텍스트는 인자(다운로드는 호출부).

    cd platform/core && python -m pytest tests/test_futures_contract.py -q
"""
from __future__ import annotations

from datetime import date

from quant_core.futures_contract import (
    OVERSEAS_ROOTS,
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
    assert resolve_contract("코스피200선물", date(2026, 6, 7), domestic_master=_DOM) == "A01606"


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


# ── front_contract — (계약코드, 만기일) 동시 해석 (M6 진입 시 ledger 기록용) ──────
def test_front_contract_domestic_code_and_expiry():
    # A01606(6월물) → 만기 = 2026-06 2번째 목요일 = 6/11
    assert front_contract("코스피200선물", date(2026, 6, 7),
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
