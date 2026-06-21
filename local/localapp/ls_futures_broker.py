"""LS증권 선물 브로커 — 국내선물(Phase D). 해외선물 메서드는 Phase F에서 추가.

LsBroker와 동일 인증/HTTP(_LsAuth 상속), 선물 TR(/futureoption/*)만 매핑.
자격증명은 load_ls_futures(별도 선물계좌). docs/ls-api/domestic-futures-research.md 정본.
"""
from __future__ import annotations
import logging
from .ls_broker import _LsAuth, normalize_ls_order_resp
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

    def _quote_raw(self, symbol: str) -> dict:
        return self._post("/futureoption/market-data", "t2101",
                          {"t2101InBlock": {"focode": symbol}})

    def price(self, symbol: str) -> float:
        return float((self._quote_raw(symbol).get("t2101OutBlock") or {}).get("price") or 0)

    def today_open(self, symbol: str) -> float:
        """catch-up 시초가. 없으면(개장전·오류) 0.0 → caller가 skip 결정."""
        try:
            v = (self._quote_raw(symbol).get("t2101OutBlock") or {}).get("open")
            return float(v) if v not in (None, "", 0, "0") else 0.0
        except Exception:
            return 0.0

    def _submit(self, symbol, qty, side, ord_ptn, unit_price):
        bns = "2" if side == "buy" else "1"          # 롱숏 net via BnsTpCode (국내선물 진입/청산 별도코드 없음)
        prc = float(unit_price) if ord_ptn == "00" else 0   # double 포인트 — int 절삭 금지
        resp = self._post("/futureoption/order", "CFOAT00100",
                          {"CFOAT00100InBlock1": {
                              "FnoIsuNo": symbol, "BnsTpCode": bns,
                              "FnoOrdprcPtnCode": ord_ptn, "FnoOrdPrc": prc, "OrdQty": qty}},
                          is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="OrdNo")

    def buy(self, symbol, qty): return self._submit(symbol, qty, "buy", "03", 0)
    def sell(self, symbol, qty): return self._submit(symbol, qty, "sell", "03", 0)
    def buy_limit(self, symbol, qty, limit_price): return self._submit(symbol, qty, "buy", "00", float(limit_price))
    def sell_limit(self, symbol, qty, limit_price): return self._submit(symbol, qty, "sell", "00", float(limit_price))

    def buy_resv_limit(self, *a, **k):
        raise NotImplementedError("국내선물 예약주문 미지원")

    def sell_resv_limit(self, *a, **k):
        raise NotImplementedError("국내선물 예약주문 미지원")
