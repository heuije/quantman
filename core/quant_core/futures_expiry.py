"""선물 만기 캘린더 — 계약 인도월 → 거래소 최종거래일 (순수 날짜 계산, M6 만기 자동청산).

자동매매는 보유 선물을 만기 전 강제청산(백스톱)해야 한다(물리인도·현금정산으로 포지션이
사라지기 전에). 이 모듈은 상품별 공개 만기 규칙(expiry_rule 키 = exec_defaults 카탈로그)을
인코딩해 인도월(year, month)로부터 최종거래일을 계산한다. **순수함수** — 네트워크/IO 없음.

⚠ 영업일은 주말만 제외하는 보수적 근사 — 거래소 휴장일 캘린더 피드가 없다(PR-1 정당:
   외부 데이터 부재라는 진짜 한계). 만기 자동청산은 roll_lead_days(≥5일) 마진만큼 *앞서*
   닫으므로 미모델 휴장일(최대 1~2일 오차)이 있어도 항상 실제 만기 전에 청산된다 —
   이 마진이 근사의 안전 가드다(증상 봉합이 아니라 보수적 하한).

만기 정밀일을 마스터(ffcode.mst)에서 못 얻는 이유: 이름에 -YYYYMM(인도월)만 있고 last-
trading-date 필드가 없다. 그래서 인도월 + 상품 규칙으로 여기서 계산한다(futures_contract와 공유).
"""
from __future__ import annotations

from datetime import date, timedelta

_THU = 3
_FRI = 4


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그 달 n번째 weekday(월=0…일=6)."""
    first = date(year, month, 1)
    first_wd = first + timedelta(days=(weekday - first.weekday()) % 7)
    return first_wd + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """그 달 마지막 weekday."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _is_business(d: date) -> bool:
    return d.weekday() < 5          # 주말만 제외(휴장일 미모델 — 모듈 docstring 참조)


def _prev_business_day(d: date) -> date:
    """d가 비영업일이면 직전 영업일, 영업일이면 d 그대로."""
    while not _is_business(d):
        d -= timedelta(days=1)
    return d


def _minus_business_days(d: date, n: int) -> date:
    """d로부터 n영업일 *이전*(d 제외)."""
    while n > 0:
        d -= timedelta(days=1)
        if _is_business(d):
            n -= 1
    return d


def _nth_last_business_day(year: int, month: int, n: int) -> date:
    """그 달 n번째 마지막 영업일 (n=1=마지막)."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = _prev_business_day(nxt - timedelta(days=1))
    return _minus_business_days(last, n - 1)


def last_trading_date(expiry_rule: str, year: int, month: int) -> date | None:
    """계약 인도월(year, month) + expiry_rule → 최종거래일. equity/미등록 규칙 → None.

    expiry_rule은 exec_defaults.InstrumentSpec.expiry_rule(상품의 사실).
    """
    if expiry_rule == "kospi200_2nd_thu":
        return _nth_weekday(year, month, _THU, 2)
    if expiry_rule in ("cme_gc", "cme_si"):
        # COMEX 금속: 인도월 3번째 마지막 영업일.
        return _nth_last_business_day(year, month, 3)
    if expiry_rule == "cme_cl":
        # NYMEX 원유: 인도월 *전월* 25일의 3영업일 전(25일이 비영업일이면 직전 영업일 기준).
        py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
        ref = _prev_business_day(date(py, pm, 25))
        return _minus_business_days(ref, 3)
    if expiry_rule == "cme_ng":
        # NYMEX 천연가스: 인도월 1일의 3영업일 전.
        return _minus_business_days(date(year, month, 1), 3)
    if expiry_rule == "cme_nq":
        # CME 주가지수: 인도월 3번째 금요일.
        return _nth_weekday(year, month, _FRI, 3)
    if expiry_rule == "cme_btc":
        # CME 암호화폐: 인도월 마지막 금요일.
        return _last_weekday(year, month, _FRI)
    return None                     # equity("") / 미등록 → 만기 없음


def roll_lead_days(default_roll: str) -> int:
    """만기 며칠 전 청산할지(백스톱 lead). default_roll(카탈로그)에서 파싱.

    "days_before:N" → N. volume_cross/oi_cross 등 일수 기반이 아니거나 파싱 불가면
    보수적 기본 5일(라이브 자동매매는 차근월 거래량 데이터가 없어 일수 백스톱으로 후퇴).
    """
    if default_roll and default_roll.startswith("days_before:"):
        try:
            return int(default_roll.split(":", 1)[1])
        except ValueError:
            pass
    return 5
