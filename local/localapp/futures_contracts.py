"""데이터셋 심볼(한글 상품명) → 라이브 계약코드 해석 (로컬앱 측 — 마스터 다운로드+캐시).

순수 해석 로직은 `quant_core.futures_contract`(단일 진실원천)에 있고, 여기선 KIS 공개 마스터
(fo_idx_code.mst=국내 지수선물·ffcode.mst=해외 CME, *키 불요*)를 받아 그 순수함수에 넘긴다.
마스터는 만기 롤이 잦지 않아 **하루 1회** 다운로드 캐시(프로세스 생존 중). BrokerRouter가
resolve를 주입받아 주문 직전 호출한다.
"""
from __future__ import annotations

import datetime
import io
import logging
import zipfile

import requests

import quant_core as qc
from quant_core.futures_contract import front_contract, resolve_contract

log = logging.getLogger("localapp.futures_contracts")

_DOM_URL = "https://new.real.download.dws.co.kr/common/master/fo_idx_code.mst.zip"
_OV_URL = "https://new.real.download.dws.co.kr/common/master/ffcode.mst.zip"


class ContractResolver:
    """심볼 → 라이브 계약코드. 마스터는 하루 1회 다운로드 캐시. 주식은 심볼 그대로 통과."""

    def __init__(self):
        self._dom: str | None = None
        self._ov: str | None = None
        self._fetched: datetime.date | None = None

    def _download(self, url: str) -> str | None:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            return z.read(z.namelist()[0]).decode("cp949", errors="replace")
        except Exception as e:                       # noqa: BLE001 — 외부 다운로드 실패는 skip(None→발주 skip)
            log.warning("선물 마스터 다운로드 실패 %s: %s", url, e)
            return None

    def _ensure(self, today: datetime.date) -> None:
        if self._fetched == today:
            return
        self._dom = self._download(_DOM_URL)
        self._ov = self._download(_OV_URL)
        self._fetched = today

    def resolve(self, symbol: str) -> str | None:
        """선물=근월물 계약코드, 주식=심볼 그대로. 마스터 미수신 시 None(호출부 발주 skip)."""
        if not qc.is_futures(symbol):
            return symbol
        today = datetime.date.today()
        self._ensure(today)
        return resolve_contract(symbol, today, domestic_master=self._dom, overseas_master=self._ov)

    def resolve_expiry(self, symbol: str):
        """선물 → (계약코드, 만기일date). 진입 시 ledger 기록용(M6 만기 자동청산).

        resolve와 같은 마스터 캐시를 재사용(추가 다운로드 없음). 비선물·마스터 미수신·미해석
        시 (None, None) → 호출부(BrokerRouter→Trader)는 만기 미기록(백스톱 비활성). 순수 해석은
        quant_core.front_contract(단일 진실원천)."""
        if not qc.is_futures(symbol):
            return None, None
        today = datetime.date.today()
        self._ensure(today)
        r = front_contract(symbol, today, domestic_master=self._dom, overseas_master=self._ov)
        return r if r is not None else (None, None)
