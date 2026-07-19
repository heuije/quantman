"""선물 만기 캘린더 — 계약 인도월 → 거래소 최종거래일 (M6 만기 자동청산·월물 리졸버 공유).

자동매매는 보유 선물을 만기 전 강제청산(백스톱)해야 한다(물리인도·현금정산으로 포지션이
사라지기 전에). 이 모듈은 상품별 공개 만기 규칙(expiry_rule 키 = exec_defaults 카탈로그)을
인코딩해 인도월(year, month)로부터 최종거래일을 계산한다. 네트워크 없음 — KR 규칙만
market_calendar(로컬 세션 캐시·KIS 휴장일 이중신호 교정본)를 참조한다.

KR 지수선물(2번째 목요일): KRX 규정상 그 날이 휴장이면 최종거래일이 **직전 거래일로
앞당겨진다**. 교정 캘린더(v0.9.78+)를 참조해 이를 정확히 반영한다 — 종전 "휴장 피드
부재" 근사의 근본 해소. 캘린더 로드 실패·범위 밖(원거리 만기 조회)만 미보정 2번째
목요일로 후퇴하며, 그때도 roll_lead_days(≥5일) 마진이 종전대로 오차를 흡수한다.
낡은 캘린더의 범위 경계에 걸리면 최대 하루 이른 판정 가능 — 안전 방향(조기 롤·조기
청산)이고, 캘린더 만료 임박은 assess가 별도 경보한다.

⚠ 해외(CME) 규칙의 영업일은 주말만 제외하는 보수적 근사 유지 — CME 휴장 캘린더 피드가
   없다(PR-1 정당: 외부 데이터 부재. market_calendar의 "US"는 NYSE 세션이라 CME 파생
   휴장과 다를 수 있어 적용하지 않는다). lead 마진이 안전 가드.

만기 정밀일을 마스터(ffcode.mst)에서 못 얻는 이유: 이름에 -YYYYMM(인도월)만 있고 last-
trading-date 필드가 없다. 그래서 인도월 + 상품 규칙으로 여기서 계산한다(futures_contract와 공유).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import market_calendar

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


def _kr_session_or_prev(d: date) -> date:
    """d가 KRX 거래일이면 그대로, 휴장이면 직전 거래일(KRX 최종거래일 순연 규정).

    is_session_day는 범위 밖 날짜에 예외가 아니라 False를 준다 — 상한 10일(최장
    설·추석 연휴+주말 방어) 안에 거래일이 안 나오면 범위 밖으로 보고 CalendarError를
    던져 호출자가 미보정 근사로 후퇴하게 한다(모듈 docstring)."""
    for _ in range(10):
        if market_calendar.is_session_day("KR", d):
            return d
        d -= timedelta(days=1)
    raise market_calendar.CalendarError(f"{d} 인근 10일 내 KR 거래일 없음(범위 밖 추정)")


def last_trading_date(expiry_rule: str, year: int, month: int) -> date | None:
    """계약 인도월(year, month) + expiry_rule → 최종거래일. equity/미등록 규칙 → None.

    expiry_rule은 exec_defaults.InstrumentSpec.expiry_rule(상품의 사실).
    """
    if expiry_rule in ("kospi200_2nd_thu", "kosdaq150_2nd_thu"):
        # KRX 지수선물 공통: 결제월 2번째 목요일 — 휴장이면 직전 거래일로 앞당김.
        raw = _nth_weekday(year, month, _THU, 2)
        try:
            return _kr_session_or_prev(raw)
        except market_calendar.CalendarError:
            # 캘린더 로드 실패·범위 밖 — 미보정 근사로 후퇴(외부 데이터 한계,
            # lead ≥5일 마진이 종전대로 흡수. 모듈 docstring).
            return raw
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
