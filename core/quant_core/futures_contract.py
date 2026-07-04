"""심볼(데이터셋 한글 상품명) → 라이브 거래 계약코드 해석 — 자동매매 #4 배선 기반(M2).

백테스트/전략은 **연속물 키**(한글 상품명 "코스피200선물"·"금선물")로 신호를 만들지만,
라이브 주문은 **특정 만기 계약코드**(국내 A01606·해외 globex GCM26)로 발주해야 한다.
이 모듈이 그 변환(front-month 해석)과 market 분류를 한다. **순수함수** — 네트워크 없음
(마스터 텍스트를 인자로 받음; 다운로드/캐시는 호출부 = 로컬앱 Trader).

마스터(KIS 공개·키불요):
  국내 `fo_idx_code.mst` — 정규('1' 시작·단축 A01)·미니('B' 시작·단축 A05)·옵션('2', C/P) 수록.
    root_char로 상품별 라인 선택(옵션은 항상 제외), 만기=2번째 목요일. 단축코드 line[1:7].
    라인예 정규 `1A01606 ... F 202606 ... KOSPI200`, 미니 `BA05606 ... 미니F 202606 ... KOSPI200`.
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
from .futures_expiry import last_trading_date, roll_lead_days

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

# 국내 선물(KRX) 상품별 식별자 — (마스터 라인 첫 글자, 단축코드 prefix). 새 국내선물은 여기 한 줄.
# 정규 코스피200선물 = 마스터 '1' 시작·단축 A01 / 미니 = 'B' 시작·단축 A05. fo_idx_code.mst가
# 두 상품을 같은 파일에 담으므로 root_char(첫 글자)로 라인을 갈라 상품별 근월물을 해석한다.
_DOMESTIC_SPEC: dict[str, tuple[str, str]] = {
    "코스피200선물":     ("1", "A01"),
    "미니코스피200선물": ("B", "A05"),
}
_DOMESTIC = tuple(_DOMESTIC_SPEC)

# 국내 선물 KRX 표준 숫자형 상품코드 prefix — 브로커 **잔고**가 쓰는 코드공간.
# 주문/마스터의 단축코드(A01606)와 달리 LS 잔고(t0441 expcode)는 KRX 상품코드형
# "101T9000"(8자)으로 포지션을 보고한다. 이 형태를 역매핑 못 해 reconcile이 자기
# 포지션을 "외부 매도"로 오판·원장 삭제한 것이 2026-07 원장↔브로커 분기 인시던트의
# 방아쇠였다. 상품코드 지식은 여기 한 곳 — 새 국내선물(코스닥150 등)은 여기 한 줄.
_DOMESTIC_KRX_PREFIX: dict[str, str] = {
    "코스피200선물":     "101",
    "미니코스피200선물": "105",
}


def _second_thursday(y: int, m: int) -> date:
    """KOSPI200 선물 최종거래일 = 그 달 2번째 목요일. (futures_expiry 단일출처 위임)"""
    return last_trading_date("kospi200_2nd_thu", y, m)


def _front_domestic(master_text: str, today: date,
                    lead_days: int = 0, root_char: str = "1") -> tuple[date, str] | None:
    """fo_idx_code.mst → KOSPI200 선물 근월물 (만기일, 단축코드). 없으면 None.

    root_char로 상품을 선택: 정규('1' 시작, 단축 A01)·미니('B' 시작, 단축 A05). 옵션('2', C/P)은
    항상 제외. 기본 root_char="1"이라 기존 호출부(정규)는 byte-identical. 만기=2번째 목요일. 순수함수.

    lead_days>0이면 **만기 lead일 전부터 차월물로 롤** — 진입 시 만기 임박 계약을 피한다.
    백스톱(_expiry_close_reason)이 보유분을 만기 lead일 전 청산하는 것과 대칭: 진입도
    같은 lead를 존중해야 "사자마자 백스톱이 롤청산"하는 만기임박 진입을 막는다. 기본 0은
    하위호환(만기 당일까지 유지) — 호출부(resolve_contract/front_contract)가 실제 lead 주입.
    """
    cutoff = today + timedelta(days=lead_days)
    best: tuple[date, str] | None = None
    for line in master_text.splitlines():
        if "KOSPI200" not in line or not line.startswith(root_char):
            continue
        m = re.search(r"F (\d{6})", line)            # 상품명 'F 202606'
        if not m:
            continue
        code = line[1:7].strip()                     # 단축코드 A01606
        ym = m.group(1)
        exp = _second_thursday(int(ym[:4]), int(ym[4:6]))
        if exp >= cutoff and (best is None or exp < best[0]):
            best = (exp, code)
    return best


def parse_front_month_domestic(master_text: str, today: date,
                               lead_days: int = 0, root_char: str = "1") -> str | None:
    """fo_idx_code.mst → KOSPI200 선물 근월물 단축코드(만기≥today+lead 중 최근). 없으면 None.

    root_char로 상품 선택(정규 '1'·미니 'B'). 기본 '1'이라 기존 호출부는 정규 그대로(byte-identical)."""
    r = _front_domestic(master_text, today, lead_days, root_char)
    return r[1] if r else None


def _front_overseas(master_text: str, today: date,
                    root: str, multiplier: float,
                    lead_days: int = 0) -> tuple[int, str] | None:
    """ffcode.mst → 해당 root 풀계약 근월물 (인도월 YYYYMM, globex 코드). 없으면 None.

    필터: exchange=CME ∧ root 정확일치(마이크로/미니/스프레드 root 자동 배제) ∧ 승수 교차검증
          ∧ code에 '-' 없음(스프레드 GCM26-N26 제외). name의 -YYYYMM으로 근월 선택. 순수함수.

    lead_days>0이면 today+lead의 인도월 기준으로 근월 선택 — 진입 시 만기 임박월 회피
    (국내와 대칭, 백스톱 lead 존중). 인도월 근사라 정밀 만기일은 백스톱이 별도 처리.
    """
    cutoff = today + timedelta(days=lead_days)
    today_ym = cutoff.year * 100 + cutoff.month
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
    return best


def parse_front_month_overseas(master_text: str, today: date,
                               root: str, multiplier: float,
                               lead_days: int = 0) -> str | None:
    """ffcode.mst → 해당 root 풀계약의 근월물 globex 코드(인도월≥today+lead 중 최소). 없으면 None."""
    r = _front_overseas(master_text, today, root, multiplier, lead_days)
    return r[1] if r else None


def dataset_for_contract(code: str) -> str | None:
    """라이브 계약코드 → 데이터셋 심볼(한글 상품명). resolve_contract의 역(reconcile/스냅샷용).

    라이브 잔고는 선물을 계약코드(국내 A01606·해외 globex GCM26)로 키잉하나 ledger·reconcile은
    데이터셋 심볼("코스피200선물"·"금선물")로 키잉한다. BrokerRouter가 스냅샷 병합 시 이 역매핑으로
    정규화해 기존 (symbol,side) 매칭 로직을 그대로 쓴다. **roll 독립**(root 파싱 — 만기/마스터 불요).

    해외: globex = root + 월코드(1) + YY(2) → root = code[:-3] → OVERSEAS_ROOTS 역.
    국내: 단축코드 prefix(A01 정규·A05 미니, _DOMESTIC_SPEC 역) + KRX 숫자형 상품코드
          prefix(101 정규·105 미니, _DOMESTIC_KRX_PREFIX 역 — LS 잔고 t0441 "101T9000" 형태).
    미매칭(미등록 코드) → None. 호출부는 None을 조용히 삼키지 말고 표면화해야 한다
    (silent skip이 2026-07 분기 인시던트를 수 주간 은닉했다).
    """
    if not code:
        return None
    root = code[:-3]                                   # 해외 globex root 추정
    for sym, r in OVERSEAS_ROOTS.items():
        if r == root:
            return sym
    for sym, (_root, prefix) in _DOMESTIC_SPEC.items():   # 국내 단축코드 prefix(A01 정규·A05 미니)
        if code.startswith(prefix):
            return sym
    if len(code) == 8:      # KRX 상품코드형은 8자 — 주식 6자 종목코드(005930) 오매칭 방지 가드
        for sym, prefix in _DOMESTIC_KRX_PREFIX.items():
            if code.startswith(prefix):
                return sym
    return None


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
    # 진입 롤 lead — 백스톱(_expiry_close_reason)과 동일 lead를 써서 만기 임박 계약 진입을
    # 피한다(사자마자 롤청산되는 만기임박 진입 방지). resolve_contract(주문 라우팅)와
    # front_contract(원장 기록)가 같은 lead로 같은 계약을 골라야 정합.
    lead = roll_lead_days(instrument_spec(symbol).default_roll)
    if symbol in _DOMESTIC:
        if not domestic_master:
            return None
        root_char = _DOMESTIC_SPEC[symbol][0]          # 정규 '1'·미니 'B' — 상품별 마스터 라인 선택
        return parse_front_month_domestic(domestic_master, today, lead, root_char=root_char)
    root = OVERSEAS_ROOTS.get(symbol)
    if root is not None:
        if not overseas_master:
            return None
        return parse_front_month_overseas(overseas_master, today, root,
                                          instrument_spec(symbol).multiplier, lead)
    return None                                        # 미등록 선물(방어)


def front_contract(symbol: str, today: date, *,
                   domestic_master: str | None = None,
                   overseas_master: str | None = None) -> tuple[str, date] | None:
    """선물 심볼 → (라이브 계약코드, 최종거래일). 진입 시 ledger 기록용(M6 만기 자동청산).

    resolve_contract와 같은 근월물을 고르되 만기일까지 함께 반환한다(단일 해석). 만기일은
    국내=2번째 목요일, 해외=인도월 + 상품 규칙(futures_expiry). 비선물·마스터 미수신·미해석
    시 None → 호출부(Trader)는 만기 미기록(백스톱 비활성)으로 처리. 순수함수.
    """
    if not is_futures(symbol):
        return None                                    # 주식: 만기 개념 없음
    spec = instrument_spec(symbol)
    lead = roll_lead_days(spec.default_roll)           # resolve_contract와 동일 lead(같은 계약 선택 정합)
    if symbol in _DOMESTIC:
        if not domestic_master:
            return None
        root_char = _DOMESTIC_SPEC[symbol][0]          # 정규 '1'·미니 'B' — 상품별 마스터 라인 선택
        r = _front_domestic(domestic_master, today, lead, root_char=root_char)
        if r is None:
            return None
        exp, code = r
        return code, exp
    root = OVERSEAS_ROOTS.get(symbol)
    if root is not None:
        if not overseas_master:
            return None
        r = _front_overseas(overseas_master, today, root, spec.multiplier, lead)
        if r is None:
            return None
        ym, code = r
        exp = last_trading_date(spec.expiry_rule, ym // 100, ym % 100)
        if exp is None:
            return None                                # 규칙 미등록(방어) — 만기 불명 시 미기록
        return code, exp
    return None                                        # 미등록 선물(방어)
