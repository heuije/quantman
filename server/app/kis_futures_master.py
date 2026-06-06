"""KIS 지수선물 종목 마스터 → KOSPI200 정규선물 최근월물 자동 해석.

KIS가 공개하는 선물옵션 종목 마스터(fo_idx_code.mst.zip, *키 불요*)에서 KOSPI200 정규선물의
최근월물(만기 미경과 중 가장 가까운) 단축코드를 구한다. 분기 롤(3·6·9·12월 2번째 목요일 만기)이
와도 매 호출마다 재해석하므로 수동 갱신·env 종목코드 불필요.

마스터 라인 예(cp949): '1A01606   KR4A01660005F 202606  ... KOSPI200'
  [0]='1'(정규선물; 'B'=미니 제외) · [1:7]=단축코드(A01606) · 상품명 'F 202606'(만기 YYYYMM).
KIS는 과거 '101S06'식 코드를 'A...'식으로 이미 마이그레이션 — 현 마스터·API 모두 A-코드(6자리).
⚠ A-코드가 FHKIF03020100 FID_INPUT_ISCD로 동작하는지는 실 KIS 호출 시점에 확정(첫 cron 로그).
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, timedelta

import requests

log = logging.getLogger("app.kis_data")

_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/fo_idx_code.mst.zip"


def _second_thursday(y: int, m: int) -> date:
    """선물 만기일 = 그 달 2번째 목요일(KOSPI200 선물 최종거래일)."""
    first = date(y, m, 1)
    first_thu = first + timedelta(days=(3 - first.weekday()) % 7)   # 목요일=3
    return first_thu + timedelta(days=7)


def parse_front_month(master_text: str, today: date) -> str | None:
    """마스터 텍스트 → KOSPI200 정규선물 최근월물 단축코드(만기≥today 중 최근). 없으면 None.

    순수함수(네트워크 없음) — 단위검증 대상. 정규선물('1' 시작)만, 미니('B')·옵션 제외.
    """
    best: tuple[date, str] | None = None
    for line in master_text.splitlines():
        if "KOSPI200" not in line or not line.startswith("1"):
            continue
        m = re.search(r"F (\d{6})", line)           # 상품명 'F 202606'
        if not m:
            continue
        code = line[1:7].strip()                     # 단축코드 A01606
        ym = m.group(1)
        exp = _second_thursday(int(ym[:4]), int(ym[4:6]))
        if exp >= today and (best is None or exp < best[0]):
            best = (exp, code)
    return best[1] if best else None


def resolve_kospi200_front_month() -> str | None:
    """KIS 공개 마스터 다운로드 → KOSPI200 정규선물 최근월물 단축코드. 실패 시 None(호출부 skip)."""
    try:
        r = requests.get(_MASTER_URL, timeout=15)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        txt = z.read(z.namelist()[0]).decode("cp949", errors="replace")
    except Exception as e:                            # noqa: BLE001 — 외부 다운로드 실패는 skip
        log.warning("KOSPI200 선물 최근월물 마스터 해석 실패: %s", e)
        return None
    code = parse_front_month(txt, date.today())
    if code:
        log.info("KOSPI200 선물 최근월물 자동해석: %s", code)
    return code
