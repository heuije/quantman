"""LS증권 선물 브로커 — 국내선물(Phase D). 해외선물 메서드는 Phase F에서 추가.

LsBroker와 동일 인증/HTTP(_LsAuth 상속), 선물 TR(/futureoption/*)만 매핑.
자격증명은 load_ls_futures(별도 선물계좌). docs/ls-api/domestic-futures-research.md 정본.
"""
from __future__ import annotations
import logging
from .ls_broker import _LsAuth   # 주문/체결 메서드(normalize·canonical_odno)는 D5/D6서 추가 import
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

    def _acct_summary_raw(self) -> dict:
        return self._post("/futureoption/accno", "CFOAQ50600",
                          {"CFOAQ50600InBlock1": {"RecCnt": 1, "BalEvalTp": "1",
                                                  "FutsPrcEvalTp": "1", "LqtQtyQryTp": "1"}})

    def _positions_raw(self) -> dict:
        return self._post("/futureoption/accno", "t0441",
                          {"t0441InBlock": {"cts_expcode": "", "cts_medocd": ""}})

    def account_snapshot(self) -> dict:
        """국내선물 잔고 — {account, positions}. 2-TR 중 실패는 raise(라우터가 fetch_failed).
        ⚠ 필드명(EvalDpsamtTotamt/MnyOrdAbleAmt/jqty/medosu 등) research 기반 — Phase D-C 실측 확정."""
        summary = (self._acct_summary_raw().get("CFOAQ50600OutBlock2") or {})
        account = {
            "equity": int(float(summary.get("EvalDpsamtTotamt") or 0)),       # 추정예탁자산(킬스위치)
            "order_cash": int(float(summary.get("MnyOrdAbleAmt") or 0)),      # 현금주문가능(사이징)
            "margin_total": int(float(summary.get("CsgnMgnTotamt") or 0)),
            "eval_pnl": int(float(summary.get("FutsEvalPnlAmt") or 0)),
            "currency": "KRW",
        }
        positions = []
        for it in (self._positions_raw().get("t0441OutBlock1") or []):
            qty = int(float(it.get("jqty") or 0))
            if qty == 0:
                continue
            positions.append({
                "symbol": str(it.get("expcode", "")).strip(),
                "side": "long" if str(it.get("medosu") or "") == "매수" else "short",
                "qty": qty,
                "avg_price": float(it.get("pamt") or 0),
                "eval_price": float(it.get("price") or 0),
                "eval_pnl": float(it.get("dtsunik1") or 0),
                "market": "DOMESTIC", "currency": "KRW", "asset_class": "futures",
            })
        return {"account": account, "positions": positions}
