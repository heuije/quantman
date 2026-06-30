"""NASDAQ Trader 심볼 디렉터리 파서 — US 데이터 유니버스의 권위 소스.

`nasdaqlisted.txt`(NASDAQ) + `otherlisted.txt`(NYSE/AMEX/ARCA 등)를 파싱해 **보통주 + ETF만**
남기고(워런트·권리·우선주·채권성·테스트이슈 제외), yfinance/dataset 코드로 정규화한다.
ADR(American Depositary Shares)과 **LP 보통단위(MLP — ET·MPLX 등 "Common Units")는 정당한
equity라 포함**한다. SPAC 유닛(share+warrant 번들)은 name 제외 대신 데이터 자동큐레이션(빈 parquet→
/symbols 제외)에 맡긴다 — "Units" 일괄 제외는 대형 MLP를 오제외하기 때문(라이브 검증).

무료·무키(공개 디렉터리). 유니버스 빌더(server)가 KIS US master와 union 한다 — 이 모듈은 US 상장
*완전성*을, KIS master는 *거래가능성*을 담당(데이터⊇거래). 컬럼 스키마(고정):
  nasdaqlisted: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot|ETF|NextShares
  otherlisted : ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|Test Issue|NASDAQ Symbol
"""

from __future__ import annotations

import re

# 보통주가 아닌 파생/우선/채권성 상품을 Security Name으로 식별해 제외(워런트/권리/우선주/채권).
# ⚠ "Units"는 제외하지 않는다 — LP 보통단위(MLP: Energy Transfer·MPLX 등 "Common Units")가
# 오제외되기 때문(라이브 검증). SPAC 유닛은 데이터 자동큐레이션에 맡긴다. "Depositary"도 제외 안 함(ADR).
_EXCLUDE_RE = re.compile(
    r"\b(Warrants?|Rights?|Preferred|Pfd|Debentures?|Notes?)\b", re.I)

_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


def _norm(sym: str) -> str:
    """디렉터리 심볼 → yfinance/dataset 코드. 클래스주 점 표기를 대시로(BRK.B→BRK-B)."""
    return sym.strip().replace(".", "-").upper()


def _parse_block(text: str, sym_i: int, name_i: int, etf_i: int, test_i: int) -> list[dict]:
    out: list[dict] = []
    lines = text.splitlines()
    for ln in lines[1:]:                                   # 헤더 1줄 스킵
        if not ln.strip() or ln.startswith("File Creation Time"):
            continue
        p = ln.split("|")
        if len(p) <= max(sym_i, name_i, etf_i, test_i):    # 잘린/이상 라인
            continue
        sym, name = p[sym_i].strip(), p[name_i].strip()
        if not sym or p[test_i].strip() == "Y":            # 빈 심볼·테스트이슈 제외
            continue
        if re.search(r"[^A-Za-z0-9.]", sym):               # 우선주($·-)·특수표기 제외(보통주/클래스주=영숫자+점)
            continue
        if _EXCLUDE_RE.search(name):                       # 워런트/권리/우선주/채권 제외(name 보강)
            continue
        out.append({"code": _norm(sym), "name": name,
                    "kind": "etf" if p[etf_i].strip() == "Y" else "stock"})
    return out


def parse_directory(nasdaq_text: str, other_text: str) -> list[dict]:
    """두 디렉터리 텍스트 → [{code, name, kind}] (보통주+ETF, 정규화·중복제거). 순수 함수(네트워크 없음)."""
    rows = _parse_block(nasdaq_text, sym_i=0, name_i=1, etf_i=6, test_i=3)
    rows += _parse_block(other_text, sym_i=0, name_i=1, etf_i=4, test_i=6)
    seen: set[str] = set()
    dedup: list[dict] = []
    for r in rows:                                         # code 기준 중복 제거(첫 등장 우선)
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        dedup.append(r)
    return dedup


def fetch() -> list[dict]:
    """NASDAQ Trader 디렉터리 2종을 받아 parse_directory. 실패 시 예외 전파(호출자가 빌더에서 처리)."""
    import requests

    headers = {"User-Agent": "Mozilla/5.0"}
    nq = requests.get(_NASDAQ_URL, headers=headers, timeout=30)
    nq.raise_for_status()
    ot = requests.get(_OTHER_URL, headers=headers, timeout=30)
    ot.raise_for_status()
    return parse_directory(nq.text, ot.text)
