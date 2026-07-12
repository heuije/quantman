"""코스닥150선물(KQ150) 라이브 계약 해석·역매핑·만기 — core 배선 검증.

실측 기반(2026-07-12 KIS 공개 마스터 fo_idx_code.mst 다운로드):
  KQ150 라인 = root_char '3' · 단축 prefix 'A06' · 라인 키워드 'KSQ150'
  예: "3A06609   KR4A06690007코스닥150F 202609 ... KSQ150"
  (KOSPI200='1'/A01/'KOSPI200', 미니='B'/A05, 옵션='2', KQ150 스프레드='4' — root_char로 격리)

이 테스트가 잠그는 불변식:
  1. KQ150 근월물 해석(진입) — 실측 라인 형식·roll lead
  2. KQ150 역매핑(reconcile) — 단축 A06·KRX형 106, 기존 101/105와 비충돌
  3. KQ150 만기(kosdaq150_2nd_thu = 2번째 목요일, kospi200와 동일 계산)
  4. **격리**: KQ150 라인이 있든 없든 KOSPI200/미니 해석은 불변(byte-identical)

    cd platform/core && python -m pytest tests/test_kq150_futures.py -q
"""
from __future__ import annotations

from datetime import date

from quant_core.futures_contract import (
    dataset_for_contract,
    front_contract,
    futures_market,
    parse_front_month_domestic,
    resolve_contract,
)
from quant_core.futures_expiry import last_trading_date

# KOSPI200(정규·미니)·옵션만 — 격리 비교용 기준 마스터.
_DOM_KP = "\n".join([
    "1A01606   KR4A01660005F 202606                  00000.0012001     KOSPI200",
    "1A01609   KR4A01690002F 202609                  00000.0022001     KOSPI200",
    "BA05606   KR4A05660001미니F 202606              00000.0012001     KOSPI200",   # 미니
    "2A02606   KR4A02660003C 202606  300.0           00000.0012001     KOSPI200",   # 옵션
])
# 위 + KQ150 근월/차월 + KQ150 스프레드(root 4) — 실측 라인 형식.
_DOM = "\n".join([
    _DOM_KP,
    "3A06606   KR4A06660008코스닥150F 202606         00000.0013003     KSQ150",
    "3A06609   KR4A06690007코스닥150F 202609         00000.0023003     KSQ150",
    "4D0660901 KR4D06696CS7코스닥150SP 2609-2612     00000.0013003     KSQ150",     # KQ150 스프레드(root 4)
])


# ── 1. KQ150 근월물 해석(파싱: lead=0 직접 호출로 격리) ──────────────────
def test_kq150_parse_front_before_expiry():
    assert parse_front_month_domestic(_DOM, date(2026, 6, 7), root_char="3",
                                      line_keyword="KSQ150") == "A06606"


def test_kq150_parse_rolls_after_expiry():
    # 6월 2번째 목요일(6/11) 경과 후 → 9월물
    assert parse_front_month_domestic(_DOM, date(2026, 6, 12), root_char="3",
                                      line_keyword="KSQ150") == "A06609"


def test_kq150_parse_excludes_spread_and_options():
    # 스프레드(root 4)·옵션(root 2)은 root_char '3' 필터로 배제 — 근월은 항상 A06 계열
    assert parse_front_month_domestic(_DOM, date(2026, 6, 7), root_char="3",
                                      line_keyword="KSQ150").startswith("A06")


# ── 2. KQ150 통합 해석(resolve_contract: roll lead 5일 적용) ────────────
def test_kq150_resolve_front():
    # 6/1 → cutoff 6/6 < 만기 6/11 → 6월물 유지
    assert resolve_contract("코스닥150선물", date(2026, 6, 1),
                            domestic_master=_DOM) == "A06606"


def test_kq150_resolve_rolls_within_lead():
    # 6/8 → cutoff 6/13 > 만기 6/11 → roll lead로 9월물(사자마자 백스톱 롤청산 방지)
    assert resolve_contract("코스닥150선물", date(2026, 6, 8),
                            domestic_master=_DOM) == "A06609"


def test_kq150_market_is_krx():
    assert futures_market("코스닥150선물") == "KRX"


def test_kq150_front_contract_returns_expiry():
    code, exp = front_contract("코스닥150선물", date(2026, 6, 1),
                               domestic_master=_DOM)
    assert code == "A06606"
    assert exp == last_trading_date("kospi200_2nd_thu", 2026, 6)   # 2번째 목요일 동일


# ── 3. 격리: KQ150 라인 유무로 KOSPI200/미니 해석 불변(날짜 독립) ────────
def test_kospi200_resolution_identical_with_or_without_kq150():
    for d in [date(2026, 6, 1), date(2026, 6, 8), date(2026, 9, 20)]:
        assert (resolve_contract("코스피200선물", d, domestic_master=_DOM)
                == resolve_contract("코스피200선물", d, domestic_master=_DOM_KP))


def test_mini_resolution_identical_with_or_without_kq150():
    for d in [date(2026, 6, 1), date(2026, 6, 8)]:
        assert (resolve_contract("미니코스피200선물", d, domestic_master=_DOM)
                == resolve_contract("미니코스피200선물", d, domestic_master=_DOM_KP))


# ── 4. 역매핑(reconcile) — 단축 A06·KRX형 106 ──────────────────────────
def test_kq150_reverse_short_code():
    assert dataset_for_contract("A06606") == "코스닥150선물"


def test_kq150_reverse_krx_form():
    # LS 잔고 t0441 KRX 상품코드형(8자) — 106…
    assert dataset_for_contract("106T9000") == "코스닥150선물"


def test_reverse_isolation_kospi200_unbroken():
    # KQ150 추가 후에도 기존 코스피200/미니 역매핑 불변(prefix 101/105/106 distinct)
    assert dataset_for_contract("A01606") == "코스피200선물"
    assert dataset_for_contract("A05606") == "미니코스피200선물"
    assert dataset_for_contract("101T9000") == "코스피200선물"
    assert dataset_for_contract("105T9000") == "미니코스피200선물"


# ── 5. 만기 규칙 kosdaq150_2nd_thu = 2번째 목요일(kospi200와 동일 계산) ──
def test_kosdaq150_expiry_is_second_thursday():
    for y, m in [(2026, 6), (2026, 9), (2026, 12), (2027, 3)]:
        assert (last_trading_date("kosdaq150_2nd_thu", y, m)
                == last_trading_date("kospi200_2nd_thu", y, m))
