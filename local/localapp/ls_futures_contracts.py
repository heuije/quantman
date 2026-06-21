"""데이터셋 심볼(한글)↔LS 선물 계약코드(101V6000). t8432 마스터 1일 캐시.

KIS ContractResolver 대칭이나 마스터 소스가 KIS 정적파일이 아니라 LS API(t8432)다.
근월물 선택은 hname의 YYYYMM 파싱(roll lead 적용). BrokerRouter에 resolve/dataset_for_code 주입.
"""
from __future__ import annotations

import datetime
import re

import quant_core as qc
from quant_core.futures_contract import instrument_spec, roll_lead_days

_KOSPI200 = "코스피200선물"
_HNAME_YM = re.compile(r"(\d{4})(\d{2})")   # "F 202406" → (2024, 06)


def _second_thursday(y: int, m: int) -> datetime.date:
    """KOSPI200 최종거래일 = 그 달 2번째 목요일(순수함수·로컬 복제 — core 결합 회피)."""
    d = datetime.date(y, m, 1)
    first_thu = d + datetime.timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + datetime.timedelta(days=7)


def _pick_front_kospi200(master: list[dict], today: datetime.date) -> str | None:
    """t8432 마스터에서 KOSPI200 근월물 shcode. 스프레드(SP)·만기경과 제외, roll lead 반영."""
    lead = roll_lead_days(instrument_spec(_KOSPI200).default_roll)
    cands = []
    for row in master:
        h = str(row.get("hname") or "")
        sh = str(row.get("shcode") or "")
        if "SP" in h or not sh.startswith("101"):   # 스프레드·비KOSPI200 정규선물 제외
            continue
        m = _HNAME_YM.search(h)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        # 만기 ≈ 2번째 목요일. lead 전이면 다음 월물로 롤.
        exp = _second_thursday(y, mo)
        if exp - datetime.timedelta(days=lead) >= today:
            cands.append((exp, sh))
    cands.sort()
    return cands[0][1] if cands else None


class LsContractResolver:
    """심볼→LS 계약코드(101V6000). 마스터 1일 캐시(선물 브로커 토큰으로 t8432 fetch)."""

    def __init__(self, futures_broker):
        self.broker = futures_broker
        self._master: list[dict] | None = None
        self._fetched: datetime.date | None = None

    def _ensure(self, today: datetime.date) -> None:
        if self._fetched == today:
            return
        try:
            self._master = self.broker.index_futures_master()
        except Exception:   # 다운로드 실패는 None → resolve None → 발주 skip(추측 발주 금지)
            self._master = None
        self._fetched = today

    def resolve(self, symbol: str) -> str | None:
        if not qc.is_futures(symbol):
            return symbol               # 주식은 심볼 그대로
        today = datetime.date.today()
        self._ensure(today)
        if not self._master:
            return None
        if symbol == _KOSPI200:
            return _pick_front_kospi200(self._master, today)
        return None                     # 국내선물=KOSPI200 only(Phase D)

    def resolve_expiry(self, symbol: str):
        """(계약코드, 만기일). 만기 자동청산 ledger 기록용. 미해석 → (None, None)."""
        code = self.resolve(symbol)
        if not code or symbol != _KOSPI200:
            return None, None
        m = _HNAME_YM.search(next(
            (r["hname"] for r in (self._master or []) if r.get("shcode") == code), "") or "")
        if not m:
            return code, None
        return code, _second_thursday(int(m.group(1)), int(m.group(2)))

    @staticmethod
    def dataset_for_code_static(code: str) -> str | None:
        """LS 계약코드 → 데이터셋 심볼(역매핑). 국내선물 101… → 코스피200선물. 주식/미등록 → None."""
        if code and code.startswith("101"):
            return _KOSPI200
        return None

    def dataset_for_code(self, code: str) -> str | None:
        return self.dataset_for_code_static(code)
