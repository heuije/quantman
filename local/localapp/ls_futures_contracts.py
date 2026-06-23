"""데이터셋 심볼(한글)↔LS 선물 계약코드(A0169000). t8467 마스터 1일 캐시.

KIS ContractResolver 대칭이나 마스터 소스가 KIS 정적파일이 아니라 LS API(t8467)다.
근월물 선택은 hname의 YYMM 파싱(roll lead 적용). BrokerRouter에 resolve/dataset_for_code 주입.
"""
from __future__ import annotations

import datetime
import re

import quant_core as qc
from quant_core.futures_contract import instrument_spec, roll_lead_days, OVERSEAS_ROOTS

_KOSPI200 = "코스피200선물"

# CME 월물코드 → 월 (ADM23 = BscGdsCd + 월물코드 + 연2자리)
# ⚠ o3101 필드명(Symbol/BscGdsCd)·LS BscGdsCd가 CME globex root와 동일한지(금 GC 등)·
#   월물코드 규칙은 research 기반 — 모의 실측 확정(불일치 시 resolve None→발주 skip 안전, 거래 불가).
#   resolve_expiry는 overseas (None,None) 유지(만기 backstop 후속).
_CME_MONTHS = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


def _pick_front_overseas(master, root, today):
    """o3101 마스터에서 BscGdsCd==root 활성계약 중 근월물 Symbol. 미일치 → None.
    마스터가 활성계약만 수록한다는 전제로 '현재월 이후 최근월'을 근월물로 선택(정밀 만기 불요)."""
    cur = (today.year, today.month)
    cands = []
    for row in master or []:
        if str(row.get("BscGdsCd") or "") != root:
            continue
        sym = str(row.get("Symbol") or "").strip()
        if not sym.startswith(root) or len(sym) < len(root) + 3:
            continue
        tail = sym[len(root):]                 # 월물코드 + YY
        mo = _CME_MONTHS.get(tail[0])
        try:
            yr = 2000 + int(tail[1:3])
        except ValueError:
            continue
        if mo is None:
            continue
        if (yr, mo) >= cur:                    # 만기경과 제외(마스터=활성계약)
            cands.append(((yr, mo), sym))
    cands.sort()
    return cands[0][1] if cands else None
# t8467 hname은 YYMM 4자리("F 2609" = 2026-09) — 2026-06-22 모의 실측 확정(G-DF9 해소).
# 과거 가정 YYYYMM("F 202406")은 t8467 미일치 → 전 계약 제외 → resolve None이던 결함.
_HNAME_YM = re.compile(r"(\d{2})(\d{2})")   # YYMM (예 "F 2609" → 26·09)


def _parse_ym(hname: str) -> tuple[int, int] | None:
    """hname "F 2609" → (2026, 9). 미매치 → None."""
    m = _HNAME_YM.search(hname or "")
    return (2000 + int(m.group(1)), int(m.group(2))) if m else None


def _second_thursday(y: int, m: int) -> datetime.date:
    """KOSPI200 최종거래일 = 그 달 2번째 목요일(순수함수·로컬 복제 — core 결합 회피)."""
    d = datetime.date(y, m, 1)
    first_thu = d + datetime.timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + datetime.timedelta(days=7)


def _pick_front_kospi200(master: list[dict], today: datetime.date) -> str | None:
    """t8467 마스터에서 KOSPI200 근월물 shcode. 스프레드(SP)·만기경과 제외, roll lead 반영.
    아웃라이트 KOSPI200 선물 shcode="A01…"(예 A0169000=F2609), 스프레드="D01…"(F SP …·SP로 제외)."""
    lead = roll_lead_days(instrument_spec(_KOSPI200).default_roll)
    cands = []
    for row in master:
        h = str(row.get("hname") or "")
        sh = str(row.get("shcode") or "")
        if "SP" in h or not sh.startswith("A01"):   # 스프레드·비KOSPI200 정규선물 제외
            continue
        ym = _parse_ym(h)
        if not ym:
            continue
        # 만기 ≈ 2번째 목요일. lead 전이면 다음 월물로 롤.
        exp = _second_thursday(*ym)
        if exp - datetime.timedelta(days=lead) >= today:
            cands.append((exp, sh))
    cands.sort()
    return cands[0][1] if cands else None


class LsContractResolver:
    """심볼→LS 계약코드(A0169000). 마스터 1일 캐시(선물 브로커 토큰으로 t8467 fetch)."""

    def __init__(self, futures_broker):
        self.broker = futures_broker
        self._master: list[dict] | None = None
        self._fetched: datetime.date | None = None
        self._ov_master: list[dict] | None = None
        self._ov_fetched: datetime.date | None = None

    def _ensure(self, today: datetime.date) -> None:
        if self._fetched == today:
            return
        try:
            self._master = self.broker.index_futures_master()
        except Exception:   # 다운로드 실패는 None → resolve None → 발주 skip(추측 발주 금지)
            # ⚠ 실패 시 당일 재시도 없음(_fetched=today 고정). 09:00 순간 API 장애면 하루 발주 불가.
            #   안전쪽=현 동작(오발주 0). Phase D+: 실패 시 _fetched 미설정으로 재시도 허용 검토.
            self._master = None
        self._fetched = today

    def _ensure_overseas(self, today: datetime.date) -> None:
        if self._ov_fetched == today:
            return
        try:
            self._ov_master = self.broker.overseas_futures_master()
        except Exception:   # 다운로드 실패 → None → resolve None → 발주 skip(추측발주 금지)
            self._ov_master = None
        self._ov_fetched = today

    def resolve(self, symbol: str) -> str | None:
        if not qc.is_futures(symbol):
            return symbol               # 주식은 심볼 그대로
        today = datetime.date.today()
        if symbol == _KOSPI200:
            self._ensure(today)         # 기존 도메스틱 t8432
            return _pick_front_kospi200(self._master, today) if self._master else None
        root = OVERSEAS_ROOTS.get(symbol)
        if root:
            self._ensure_overseas(today)
            return _pick_front_overseas(self._ov_master, root, today) if self._ov_master is not None else None
        return None                     # 미등록 선물

    def resolve_expiry(self, symbol: str):
        """(계약코드, 만기일). 만기 자동청산 ledger 기록용. 미해석 → (None, None)."""
        code = self.resolve(symbol)
        if not code or symbol != _KOSPI200:
            return None, None
        ym = _parse_ym(next(
            (r["hname"] for r in (self._master or []) if r.get("shcode") == code), ""))
        if not ym:
            return code, None
        return code, _second_thursday(*ym)

    @staticmethod
    def dataset_for_code_static(code: str) -> str | None:
        """LS 계약코드 → 데이터셋 심볼(역매핑). 국내선물 A01… → 코스피200선물. 주식/미등록 → None."""
        if code and code.startswith("A01"):
            return _KOSPI200
        for sym, root in OVERSEAS_ROOTS.items():
            if code and code.startswith(root) and len(code) >= len(root) + 3 \
                    and code[len(root)] in _CME_MONTHS:
                return sym
        return None

    def dataset_for_code(self, code: str) -> str | None:
        return self.dataset_for_code_static(code)
