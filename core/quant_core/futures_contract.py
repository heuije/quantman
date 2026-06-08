"""심볼(데이터셋 한글 상품명) → 라이브 거래 계약코드 해석 — 자동매매 #4 배선 기반(M2).

백테스트/전략은 **연속물 키**(한글 상품명 "코스피200선물"·"금선물")로 신호를 만들지만,
라이브 주문은 **특정 만기 계약코드**(국내 A01606·해외 globex GCM26)로 발주해야 한다.
이 모듈이 그 변환(front-month 해석)과 market 분류를 한다. **순수함수** — 네트워크 없음
(마스터 텍스트를 인자로 받음; 다운로드/캐시는 호출부 = 로컬앱 Trader).

마스터(KIS 공개·키불요):
  국내 `fo_idx_code.mst` — KOSPI200 정규선물('1' 시작, 미니'B'/옵션 제외), 만기=2번째 목요일.
    라인예 `1A01606  ... F 202606 ... KOSPI200` → 단축코드 line[1:7]=A01606.
  해외 `ffcode.mst` — CME globex. col0=코드, name에 `-YYYYMM`, exchange/root, 승수.
    근월물 = root 정확일치 + 승수 교차검증(parse 안전장치) + 스프레드(code"-")/TAS(root) 제외 후
    name의 YYYYMM이 today_ym 이상 중 최소. 풀계약만(마이크로 MGC·미니 QM·1oz/100oz 등 root로 배제).

⚠ 만기 정밀일(product별 last-trading-day)은 마스터에 없다(이름 -YYYYMM뿐). 해외 선택은 月 기준
   근사 — 에너지(CL/NG)는 이름월=인도월(전월만기)이라 부정확할 수 있으나, 만기 자동청산(M6)이
   만기 前 청산하는 안전망으로 커버한다. 정밀 만기캘린더는 M6과 공유(중복 금지).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .exec_defaults import instrument_spec, is_futures

# 데이터셋 한글 상품명 → CME globex root. 카탈로그(exec_defaults)의 6종 CME 선물.
# root는 ffcode.mst 'CME 다음 컬럼'과 정확일치해야 하며, 승수로 교차검증한다(풀계약 확정).
OVERSEAS_ROOTS: dict[str, str] = {
    "원유선물": "CL",          # Crude Oil 1000배럴
    "천연가스선물": "NG",       # Natural Gas 10000 MMBtu
    "금선물": "GC",            # Gold 100oz
    "은선물(COMEX)": "SI",     # Silver 5000oz (1SI=100oz·SIL=micro 제외)
    "나스닥선물": "NQ",         # E-mini Nasdaq100 $20/pt (MNQ=micro 제외)
    "비트코인선물": "BTC",      # Bitcoin 5coin (MBT=micro 제외)
}

# 국내 선물(KRX) — 현 카탈로그상 KOSPI200 정규선물이 유일.
_DOMESTIC = ("코스피200선물",)


def _second_thursday(y: int, m: int) -> date:
    """KOSPI200 선물 최종거래일 = 그 달 2번째 목요일."""
    first = date(y, m, 1)
    first_thu = first + timedelta(days=(3 - first.weekday()) % 7)   # 목요일=3
    return first_thu + timedelta(days=7)


def parse_front_month_domestic(master_text: str, today: date) -> str | None:
    """fo_idx_code.mst → KOSPI200 정규선물 최근월물 단축코드(만기≥today 중 최근). 없으면 None.

    정규선물('1' 시작)만 — 미니('B')·옵션('2', C/P) 제외. 만기=2번째 목요일. 순수함수.
    """
    best: tuple[date, str] | None = None
    for line in master_text.splitlines():
        if "KOSPI200" not in line or not line.startswith("1"):
            continue
        m = re.search(r"F (\d{6})", line)            # 상품명 'F 202606'
        if not m:
            continue
        code = line[1:7].strip()                     # 단축코드 A01606
        ym = m.group(1)
        exp = _second_thursday(int(ym[:4]), int(ym[4:6]))
        if exp >= today and (best is None or exp < best[0]):
            best = (exp, code)
    return best[1] if best else None


def parse_front_month_overseas(master_text: str, today: date,
                               root: str, multiplier: float) -> str | None:
    """ffcode.mst → 해당 root 풀계약의 근월물 globex 코드(만기月≥today 중 최소). 없으면 None.

    필터: exchange=CME ∧ root 정확일치(마이크로/미니/스프레드 root 자동 배제) ∧ 승수 교차검증
          ∧ code에 '-' 없음(스프레드 GCM26-N26 제외). name의 -YYYYMM으로 근월 선택. 순수함수.
    """
    today_ym = today.year * 100 + today.month
    want_mult = str(int(multiplier))
    best: tuple[int, str] | None = None
    for line in master_text.splitlines():
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 6:
            continue
        code = cols[0]
        if "-" in code:                               # 스프레드 제외
            continue
        if "CME" not in cols:
            continue
        ci = cols.index("CME")
        if ci + 1 >= len(cols) or cols[ci + 1] != root:   # root 정확일치
            continue
        if want_mult not in cols[ci + 2:]:            # 승수 교차검증(parse 안전장치)
            continue
        m = re.search(r"-(\d{6})\b", line)            # name의 -YYYYMM (스프레드 4자리는 비매칭)
        if not m:
            continue
        ym = int(m.group(1))
        if ym < today_ym:
            continue
        if best is None or ym < best[0]:
            best = (ym, code)
    return best[1] if best else None


def futures_market(symbol: str) -> str:
    """선물 심볼 → 거래 market. 'KRX'(국내)·'CME'(해외)·''(선물 아님/미등록)."""
    if symbol in _DOMESTIC:
        return "KRX"
    if symbol in OVERSEAS_ROOTS:
        return "CME"
    return ""


def resolve_contract(symbol: str, today: date, *,
                     domestic_master: str | None = None,
                     overseas_master: str | None = None) -> str | None:
    """심볼 → 라이브 거래 계약코드. 주식은 심볼 그대로(곧 거래코드), 선물은 근월물 해석.

    마스터 미제공(다운로드 실패 등) 시 None → 호출부(Trader)는 발주 skip해야 한다(추측 발주 금지).
    """
    if not is_futures(symbol):
        return symbol                                  # 주식·ETF·지수: 심볼이 곧 거래코드
    if symbol in _DOMESTIC:
        return parse_front_month_domestic(domestic_master, today) if domestic_master else None
    root = OVERSEAS_ROOTS.get(symbol)
    if root is not None:
        if not overseas_master:
            return None
        return parse_front_month_overseas(overseas_master, today, root,
                                          instrument_spec(symbol).multiplier)
    return None                                        # 미등록 선물(방어)
