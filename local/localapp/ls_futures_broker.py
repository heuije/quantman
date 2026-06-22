"""LS증권 선물 브로커 — 국내선물(Phase D). 해외선물 메서드는 Phase F에서 추가.

LsBroker와 동일 인증/HTTP(_LsAuth 상속), 선물 TR(/futureoption/*)만 매핑.
자격증명은 load_ls_futures(별도 선물계좌). docs/ls-api/domestic-futures-research.md 정본.
"""
from __future__ import annotations
import logging
from .ls_broker import _LsAuth, normalize_ls_order_resp, canonical_odno
from .secrets_store import load_ls_futures, load_ls_overseas_futures

log = logging.getLogger("localapp.ls_futures_broker")


class LsFuturesBroker(_LsAuth):
    def __init__(self):
        dom = load_ls_futures()
        ovc = load_ls_overseas_futures()
        if not dom and not ovc:
            raise RuntimeError("LS 선물 자격증명이 없습니다. setup에서 등록하세요.")
        # 도메스틱 인증을 베이스로(_LsAuth 상속·self._post=도메스틱·Phase D 무변경).
        # 도메스틱이 없으면 해외 자격증명을 베이스로(해외전용 사용자) — domestic_configured로 게이트.
        super().__init__(dom or ovc)
        self._dom_configured = dom is not None
        # 해외선물 인증은 별도 컨텍스트(KIS _ov_token 분리 미러). 같은 appkey면 토큰캐시 공유.
        self._ov = _LsAuth(ovc) if ovc else None

    @property
    def domestic_configured(self) -> bool:
        return self._dom_configured

    @property
    def overseas_configured(self) -> bool:
        return self._ov is not None

    def index_futures_master(self) -> list[dict]:
        """t8432 지수선물 마스터 — shcode/expcode/hname. resolver가 1일 캐시."""
        body = self._post("/futureoption/market-data", "t8432", {"t8432InBlock": {"gubun": "0"}})
        return body.get("t8432OutBlock") or []

    def overseas_futures_master(self) -> list[dict]:
        """o3101 해외선물 종목마스터 — Symbol(ADM23)·BscGdsCd·CtrtPrAmt. resolver가 1일 캐시."""
        body = self._ov._post("/overseas-futureoption/market-data", "o3101", {"o3101InBlock": {"gubun": ""}})
        return body.get("o3101OutBlock") or []

    def _acct_summary_raw(self) -> dict:
        # RecCnt=int (LsApiHelper 스펙 — 해외주식 COSOQ00201 RecCnt int 확인과 동일 패턴).
        # ⚠ 국내선물 모의계좌 creds 등록 후 실측 확정(InBlock 필드 완전성 포함).
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

    def cancel(self, order_no, symbol, qty):
        resp = self._post("/futureoption/order", "CFOAT00300",
                          {"CFOAT00300InBlock1": {
                              "OrgOrdNo": int(order_no) if str(order_no).isdigit() else order_no,
                              "FnoIsuNo": symbol, "CancQty": qty}}, is_order=True)
        r = normalize_ls_order_resp(resp, ordno_field="OrdNo")
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}

    def _ccld_raw(self, chegb: str) -> dict:
        return self._post("/futureoption/accno", "t0434",
                          {"t0434InBlock": {"expcode": "", "chegb": chegb, "sortgb": "1", "cts_ordno": ""}})

    def order_status(self, order_no, symbol=None, hint=None):
        """체결 인지 — t0434 chegb='0'(전체)로 filled/cancelled 포함 조회(lesson #3).
        ⚠ G-DF3: status 문자열 실측 전 — '취소' 포함 시 cancelled, 그 외 cheqty/ordrem로 판정."""
        try:
            rows = self._ccld_raw("0").get("t0434OutBlock1") or []
        except Exception as e:
            log.warning("LS선물 order_status 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("ordno")) != canonical_odno(order_no):
                continue
            che = int(float(row.get("cheqty") or 0))
            rem = int(float(row.get("ordrem") or 0))
            status = str(row.get("status") or "")
            if "취소" in status:
                st = "cancelled"
            elif rem == 0 and che > 0:
                st = "filled"
            elif che > 0:
                st = "partial"
            else:
                st = "submitted"
            return {"order_no": order_no, "status": st, "filled_qty": che, "remain_qty": rem,
                    "fill_price": float(row.get("cheprice") or row.get("price") or 0)}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def pending_orders(self):
        try:
            rows = self._ccld_raw("2").get("t0434OutBlock1") or []
        except Exception as e:
            log.warning("LS선물 pending 실패: %s", e)
            return []
        out = []
        for row in rows:
            if str(row.get("orgordno") or "0") not in ("0", "", "00000000000"):   # 정정/취소행 제외(lesson #7)
                continue
            rem = int(float(row.get("ordrem") or 0))
            if rem <= 0:
                continue
            out.append({"order_no": str(row.get("ordno") or ""), "symbol": str(row.get("expcode") or "").strip(),
                        "side": "buy" if str(row.get("medosu") or "") == "매수" else "sell",
                        "qty": int(float(row.get("qty") or 0)), "remain_qty": rem,
                        "limit_price": float(row.get("price") or 0),
                        "market": "DOMESTIC", "currency": "KRW", "asset_class": "futures"})
        return out

    def _ov_acct_raw(self) -> dict:
        from datetime import datetime
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ03000",
                              {"CIDBQ03000InBlock1": {"AcntTpCode": "1", "TrdDt": datetime.now().strftime("%Y%m%d")}})

    def _ov_xchrat_raw(self) -> dict:
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ05300",
                              {"CIDBQ05300InBlock1": {"CrcyCode": "USD"}})

    def _ov_positions_raw(self) -> dict:
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ01500",
                              {"CIDBQ01500InBlock1": {"AcntTpCode": "1", "BalTpCode": "1"}})

    def overseas_account_snapshot(self) -> dict:
        """해외선물 잔고 — {account(KRW), positions}. KRW equity = USD × Xchrat(CIDBQ05300).
        2-3 TR 중 실패·Xchrat<=0은 raise(라우터 fetch_failed). ⚠ 필드명·G-OF5(USD→KRW 경로) research 기반 — 모의 실측."""
        acct_rows = self._ov_acct_raw().get("CIDBQ03000OutBlock2") or []
        acct = {}
        for r in acct_rows:
            if str(r.get("CrcyObjCode") or "TOT") in ("TOT", "", "USD"):
                acct = r
                break
        if not acct and acct_rows:
            acct = acct_rows[0]
        equity_usd = float(acct.get("EvalAssetAmt") or 0)
        order_cash_usd = float(acct.get("AbrdFutsOrdAbleAmt") or 0)
        xrows = self._ov_xchrat_raw().get("CIDBQ05300OutBlock2") or []
        xchrat = 0.0
        for r in xrows:
            if str(r.get("CrcyCode") or "") in ("USD", ""):
                xchrat = float(r.get("Xchrat") or 0)
                break
        if xchrat <= 0:
            raise RuntimeError(f"LS 해외선물 환율(Xchrat) 미수신({xchrat}) — KRW equity 산출 불가. 보류.")
        account = {
            "equity": equity_usd * xchrat,
            "order_cash": order_cash_usd * xchrat,
            "margin_total": float(acct.get("AbrdFutsCsgnMgn") or 0) * xchrat,
            "eval_pnl": float(acct.get("AbrdFutsEvalPnlAmt") or 0) * xchrat,
            "currency": "KRW", "fx_usdkrw": xchrat,
        }
        positions = []
        for it in (self._ov_positions_raw().get("CIDBQ01500OutBlock2") or []):
            qty = int(float(it.get("BalQty") or 0))
            if qty == 0:
                continue
            positions.append({
                "symbol": str(it.get("IsuCodeVal") or "").strip(),
                "side": "long" if str(it.get("BnsTpCode") or "") == "2" else "short",
                "qty": qty, "avg_price": float(it.get("PchsPrc") or 0),
                "eval_price": float(it.get("OvrsDrvtNowPrc") or 0),
                "eval_pnl": float(it.get("AbrdFutsEvalPnlAmt") or 0),
                "market": "OVERSEAS", "currency": "USD", "asset_class": "futures",
            })
        return {"account": account, "positions": positions}

    def _ov_submit(self, symbol, qty, side, ord_ptn, unit_price):
        """CIDBT00100 신규주문. BnsTpCode 2매수/1매도, AbrdFutsOrdPtnCode 1시장/2지정. 가격 double."""
        from datetime import datetime
        prc = float(unit_price) if ord_ptn == "2" else 0
        resp = self._ov._post("/overseas-futureoption/order", "CIDBT00100",
                              {"CIDBT00100InBlock1": {
                                  "OrdDt": datetime.now().strftime("%Y%m%d"),
                                  "IsuCodeVal": symbol, "FutsOrdTpCode": "1",
                                  "BnsTpCode": "2" if side == "buy" else "1",
                                  "AbrdFutsOrdPtnCode": ord_ptn, "OvrsDrvtOrdPrc": prc,
                                  "OrdQty": qty}}, is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="OvrsFutsOrdNo")

    def overseas_buy(self, symbol, qty): return self._ov_submit(symbol, qty, "buy", "1", 0)
    def overseas_sell(self, symbol, qty): return self._ov_submit(symbol, qty, "sell", "1", 0)
    def overseas_buy_limit(self, symbol, qty, limit_price): return self._ov_submit(symbol, qty, "buy", "2", float(limit_price))
    def overseas_sell_limit(self, symbol, qty, limit_price): return self._ov_submit(symbol, qty, "sell", "2", float(limit_price))

    def _ov_ccld_raw(self) -> dict:
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ02400",
                              {"CIDBQ02400InBlock1": {"QrySrtDt": today, "QryEndDt": today,
                                                      "ThdayTpCode": "1", "OrdStatCode": "0",
                                                      "OvrsDrvtFnoTpCode": "A"}})

    def overseas_order_status(self, order_no) -> dict:
        """CIDBQ02400 OvrsFutsOrdNo 매칭 → filled/partial/cancelled/submitted.
        ⚠ G-OF4: TrxStatCodeNm 문자열 실측 전 — '취소' 포함 시 cancelled, 그 외 Exec/Unerc로 판정."""
        try:
            rows = self._ov_ccld_raw().get("CIDBQ02400OutBlock2") or []
        except Exception as e:
            log.warning("LS 해외선물 order_status 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("OvrsFutsOrdNo")) != canonical_odno(order_no):
                continue
            ex = int(float(row.get("ExecQty") or 0))
            un = int(float(row.get("UnercQty") or 0))
            nm = str(row.get("TrxStatCodeNm") or "")
            if "취소" in nm:
                st = "cancelled"
            elif un == 0 and ex > 0:
                st = "filled"
            elif ex > 0:
                st = "partial"
            else:
                st = "submitted"
            return {"order_no": order_no, "status": st, "filled_qty": ex, "remain_qty": un,
                    "fill_price": float(row.get("AbrdFutsExecPrc") or 0)}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def overseas_cancel(self, order_no, symbol, orgn_ord_dt):
        """CIDBT01000 취소 — OvrsFutsOrgOrdNo+IsuCodeVal+원주문일자(OrdDt) 필수(KIS overseas_cancel 미러).
        라우터 hot-path 아님(CME 취소는 라우터서 NotImplemented·M10 직접배선 대상). 반환 {success,message,msg_cd}."""
        resp = self._ov._post("/overseas-futureoption/order", "CIDBT01000",
                              {"CIDBT01000InBlock1": {
                                  "OrdDt": str(orgn_ord_dt), "IsuCodeVal": symbol,
                                  "OvrsFutsOrgOrdNo": str(order_no), "FutsOrdTpCode": "3"}}, is_order=True)
        r = normalize_ls_order_resp(resp, ordno_field="OvrsFutsOrdNo")
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}

    # NOTE: orderable_qty(CFOAQ10100 NewOrdAbleQty)는 증거금 사이징 클램프용이나 현재 호출자
    # 없음(4원칙#2) → 제거. Trader 선물 사이징 배선 시 함께 추가(매핑=domestic-futures-research.md).
