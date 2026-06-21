"""LS증권 선물 브로커 — 국내선물(Phase D). 해외선물 메서드는 Phase F에서 추가.

LsBroker와 동일 인증/HTTP(_LsAuth 상속), 선물 TR(/futureoption/*)만 매핑.
자격증명은 load_ls_futures(별도 선물계좌). docs/ls-api/domestic-futures-research.md 정본.
"""
from __future__ import annotations
import logging
from .ls_broker import _LsAuth, normalize_ls_order_resp, canonical_odno
from .secrets_store import load_ls_futures

log = logging.getLogger("localapp.ls_futures_broker")


class LsFuturesBroker(_LsAuth):
    def __init__(self):
        creds = load_ls_futures()
        if not creds:
            raise RuntimeError("LS 선물 자격증명이 없습니다. setup에서 등록하세요.")
        super().__init__(creds)

    @property
    def domestic_configured(self) -> bool:
        return True

    @property
    def overseas_configured(self) -> bool:
        return False   # Phase F에서 해외선물 분기

    def index_futures_master(self) -> list[dict]:
        """t8432 지수선물 마스터 — shcode/expcode/hname. resolver가 1일 캐시."""
        body = self._post("/futureoption/market-data", "t8432", {"t8432InBlock": {"gubun": "0"}})
        return body.get("t8432OutBlock") or []
