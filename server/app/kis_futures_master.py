"""KIS 지수선물 종목 마스터 다운로드 → KOSPI200 정규선물 최근월물 자동 해석.

KIS가 공개하는 선물옵션 종목 마스터(fo_idx_code.mst.zip, *키 불요*)를 받아 KOSPI200 정규선물의
최근월물(만기 미경과 중 가장 가까운) 단축코드를 구한다. 분기 롤(3·6·9·12월 2번째 목요일 만기)이
와도 매 호출마다 재해석하므로 수동 갱신·env 종목코드 불필요.

⚠ **파싱 로직은 `quant_core.futures_contract`로 통합**(단일 진실원천 — 로컬앱 자동매매 #4 배선이
   같은 front-month 해석을 써야 하므로). 이 모듈은 *다운로드 wrapper*만 담당. `parse_front_month`·
   `_second_thursday`는 하위호환 재노출(import).
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date

import requests

from quant_core.futures_contract import (  # noqa: F401 — 하위호환 재노출
    _second_thursday,
    parse_front_month_domestic as parse_front_month,
)

log = logging.getLogger("app.kis_data")

_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/fo_idx_code.mst.zip"


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
