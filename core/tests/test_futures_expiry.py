"""선물 만기 캘린더 — 순수 날짜 계산 단위검증 (M6 만기 자동청산 기반).

각 상품의 거래소 최종거래일 규칙을 인코딩한다(spec = 공개 CME/KRX 규칙):
  kospi200_2nd_thu : 그 달 2번째 목요일
  cme_gc / cme_si  : 인도월의 3번째 마지막 영업일 (COMEX 금속)
  cme_cl           : 인도월 *전월* 25일의 3영업일 전 (NYMEX 원유; 25일이 비영업일이면
                     그 직전 영업일 기준) — 이름월=인도월이라 만기는 전월에 온다
  cme_ng           : 인도월 1일의 3영업일 전 (NYMEX 천연가스)
  cme_nq           : 인도월 3번째 금요일 (CME 주가지수)
  cme_btc          : 인도월 마지막 금요일 (CME 암호화폐)

영업일은 주말만 제외하는 보수적 근사 — 거래소 휴장일은 모델링하지 않는다(피드 없음).
만기 자동청산은 roll_lead_days 마진(≥5일)만큼 *앞서* 청산하므로, 미모델 휴장일(최대
1~2일 오차)이 있어도 항상 실제 만기 전에 닫힌다. 이 마진이 근사의 안전 가드다.

    cd platform/core && python -m pytest tests/test_futures_expiry.py -q
"""
from __future__ import annotations

from datetime import date

from quant_core.futures_expiry import last_trading_date, roll_lead_days


# ── KOSPI200 — 2번째 목요일 ────────────────────────────────────────────────────
def test_kospi200_second_thursday():
    assert last_trading_date("kospi200_2nd_thu", 2026, 6) == date(2026, 6, 11)
    assert last_trading_date("kospi200_2nd_thu", 2026, 9) == date(2026, 9, 10)
    assert last_trading_date("kospi200_2nd_thu", 2026, 12) == date(2026, 12, 10)


# ── COMEX 금속(GC/SI) — 인도월 3번째 마지막 영업일 ──────────────────────────────
def test_comex_metals_third_last_business_day():
    assert last_trading_date("cme_gc", 2026, 6) == date(2026, 6, 26)
    assert last_trading_date("cme_si", 2026, 7) == date(2026, 7, 29)


# ── NYMEX 원유(CL) — 전월 25일의 3영업일 전 ────────────────────────────────────
def test_nymex_crude_prior_month_rule():
    # CLN26(인도월 7월) → 전월 6월 25일(목, 영업일)의 3영업일 전 = 6/22
    assert last_trading_date("cme_cl", 2026, 7) == date(2026, 6, 22)
    # CLM26(인도월 6월) → 전월 5월 25일(월)의 3영업일 전 = 5/20
    assert last_trading_date("cme_cl", 2026, 6) == date(2026, 5, 20)


def test_nymex_crude_january_year_wrap():
    # 1월물 → 전월 = 전년 12월 (연도 wrap)
    exp = last_trading_date("cme_cl", 2026, 1)
    assert exp == date(2025, 12, 22)


# ── NYMEX 천연가스(NG) — 인도월 1일의 3영업일 전 ───────────────────────────────
def test_nymex_natgas_before_first():
    assert last_trading_date("cme_ng", 2026, 6) == date(2026, 5, 27)


# ── CME 주가지수(NQ) — 3번째 금요일 ─────────────────────────────────────────────
def test_cme_index_third_friday():
    assert last_trading_date("cme_nq", 2026, 6) == date(2026, 6, 19)


# ── CME 암호화폐(BTC) — 마지막 금요일 ──────────────────────────────────────────
def test_cme_crypto_last_friday():
    assert last_trading_date("cme_btc", 2026, 6) == date(2026, 6, 26)


# ── equity / 미등록 규칙 → None (만기 없음) ────────────────────────────────────
def test_equity_and_unknown_rule_none():
    assert last_trading_date("", 2026, 6) is None
    assert last_trading_date("cme_unknown", 2026, 6) is None


# ── roll_lead_days — default_roll 파싱 ─────────────────────────────────────────
def test_roll_lead_days_parses_days_before():
    assert roll_lead_days("days_before:5") == 5
    assert roll_lead_days("days_before:3") == 3


def test_roll_lead_days_defaults_for_non_days_rules():
    # volume_cross/oi_cross는 일수 기반이 아님 → 보수적 기본 5일
    assert roll_lead_days("volume_cross") == 5
    assert roll_lead_days("oi_cross") == 5
    assert roll_lead_days("") == 5
    assert roll_lead_days("days_before:bad") == 5
