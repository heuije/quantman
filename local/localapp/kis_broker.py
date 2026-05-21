"""KIS(한국투자증권) REST 브로커 — 모의투자(VTS) 연동.

자격증명은 keyring에서만 읽는다. Access Token은 APP_DIR에 캐싱(24h).
실전(virtual=False) TR_ID도 분기하지만 첫 릴리스는 모의투자만 사용한다.

Phase 9 확장:
- 지정가 주문 (ORD_DVSN="00") + 시장가 (ORD_DVSN="01")
- 주문 취소·정정 (order-rvsecncl)
- 일별 주문체결 조회 (inquire-daily-ccld) — 미체결/체결 상태 추적
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import requests

from .config import APP_DIR
from .file_security import restrict_to_owner
from .secrets_store import load_kis

log = logging.getLogger("localapp.kis_broker")

_VTS = "https://openapivts.koreainvestment.com:29443"
_REAL = "https://openapi.koreainvestment.com:9443"
_TOKEN_CACHE = APP_DIR / ".kis_token.json"


def _kis_check(r: requests.Response) -> dict:
    """KIS 응답 검증 — 오류 시 KIS가 보낸 메시지(msg_cd/msg1)를 그대로 노출."""
    try:
        body = r.json()
    except Exception:
        raise RuntimeError(f"KIS API HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code != 200:
        raise RuntimeError(f"KIS API HTTP {r.status_code} "
                           f"[{body.get('msg_cd', '')}] {body.get('msg1', body)}")
    return body


class KisBroker:
    """KIS 모의투자 브로커. Broker 인터페이스 구현.

    주문·잔고는 모의투자(VTS) 도메인, 시세 조회는 실전 도메인을 사용한다.
    KIS 모의투자 서버는 시세 API를 제대로 지원하지 않기 때문이다.
    """

    def __init__(self):
        creds = load_kis()
        if not creds:
            raise RuntimeError("KIS 자격증명이 없습니다. 먼저 setup으로 등록하세요.")
        self.key = creds["app_key"]
        self.secret = creds["app_secret"]
        self.virtual = creds.get("virtual", True)
        self.base = _VTS if self.virtual else _REAL
        self.quote_base = _REAL          # 시세는 항상 실전 도메인
        no = creds["account_no"].split("-")
        self.cano, self.acnt_cd = no[0], (no[1] if len(no) > 1 else "01")

    # ── 토큰 ──────────────────────────────────────────────────────────────────

    def _token(self) -> str:
        if _TOKEN_CACHE.exists():
            c = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
            if datetime.fromisoformat(c["expires_at"]) > datetime.now() + timedelta(minutes=30):
                return c["access_token"]
        r = requests.post(f"{self.base}/oauth2/tokenP",
                           json={"grant_type": "client_credentials",
                                 "appkey": self.key, "appsecret": self.secret},
                           timeout=10)
        r.raise_for_status()
        d = r.json()
        _TOKEN_CACHE.write_text(json.dumps({
            "access_token": d["access_token"],
            "expires_at": (datetime.now()
                           + timedelta(seconds=int(d.get("expires_in", 86400)))).isoformat(),
        }), encoding="utf-8")
        # 토큰 파일은 같은 PC의 다른 사용자·프로세스가 읽으면 안 되는 자격증명.
        restrict_to_owner(_TOKEN_CACHE)
        return d["access_token"]

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
            "appkey": self.key, "appsecret": self.secret,
            "tr_id": tr_id, "custtype": "P",
        }

    # ── WebSocket 인증 (실시간 시세용) ────────────────────────────────────────

    def get_approval_key(self) -> str:
        """KIS WebSocket용 일회성 approval_key 발급.

        REST `/oauth2/Approval` 호출. 발급 후 KIS WebSocket 연결의 header에 포함.
        토큰과는 별도 — 매 연결마다 새로 발급해도 무방.
        """
        r = requests.post(
            f"{self.base}/oauth2/Approval",
            json={"grant_type": "client_credentials",
                  "appkey": self.key, "secretkey": self.secret},
            timeout=10)
        d = _kis_check(r)
        return d["approval_key"]

    @property
    def ws_url(self) -> str:
        """KIS WebSocket URL — 모의 31000, 실전 21000."""
        return ("ws://ops.koreainvestment.com:31000" if self.virtual
                else "ws://ops.koreainvestment.com:21000")

    # ── 조회 ──────────────────────────────────────────────────────────────────

    def _balance_raw(self) -> dict:
        tr = "VTTC8434R" if self.virtual else "TTTC8434R"
        r = requests.get(f"{self.base}/uapi/domestic-stock/v1/trading/inquire-balance",
                         headers=self._headers(tr), timeout=10, params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        })
        return _kis_check(r)

    def account_snapshot(self) -> dict:
        body = self._balance_raw()
        out = body.get("output2", [{}])[0]
        positions = [{
            "symbol": it["pdno"], "name": it["prdt_name"],
            "qty": int(it["hldg_qty"]),
            "avg_price": float(it["pchs_avg_pric"]),
            "eval_price": float(it["prpr"]),
        } for it in body.get("output1", []) if int(it.get("hldg_qty", 0)) > 0]
        return {
            "balance": {"cash": int(out.get("dnca_tot_amt", 0)),
                        "total_eval": int(out.get("tot_evlu_amt", 0))},
            "positions": positions,
        }

    # ── 시장 라우팅 ──────────────────────────────────────────────────────────

    def _detect_market(self, symbol: str) -> str:
        """종목 코드 형식으로 시장 추정.

        - 6자리 숫자/알파넘 → 국내 (KOSPI/KOSDAQ, KIS J 시장구분)
        - 4자리 숫자 → 일본 TSE
        - 5자리 숫자 → 홍콩 HKS
        - 영문 1-5자 → 미국 (티커, NAS/NYS/AMS는 거래소별 마스터 필요해 NAS로 가정)
        """
        s = symbol.upper()
        if len(s) >= 6 and s[:6].isalnum() and not s.isalpha():
            return "DOMESTIC"
        if s.isdigit():
            if len(s) == 4:
                return "TSE"
            if len(s) == 5:
                return "HKS"
        if s.isalpha() and 1 <= len(s) <= 5:
            return "NAS"     # 미국 거래소 — 실거래 시 정확한 거래소는 마스터에서 조회 필요
        return "DOMESTIC"     # 안전한 기본값

    def price(self, symbol: str) -> float:
        """현재가 조회 — 시장에 따라 다른 endpoint."""
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            return self._price_domestic(symbol)
        return self._price_overseas(symbol, market)

    def _price_domestic(self, symbol: str) -> float:
        r = requests.get(
            f"{self.quote_base}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers("FHKST01010100"), timeout=10,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        body = _kis_check(r)
        return float(body.get("output", {}).get("stck_prpr", 0))

    def _price_overseas(self, symbol: str, market: str) -> float:
        # KIS overseas 시장 코드: NAS/NYS/AMS = 실시간(NASD/NYSE/AMEX) 또는 지연(NAS/NYS/AMS).
        # 우선 지연 시세(별도 신청 불필요) 사용.
        excd_map = {"NAS": "NAS", "NYS": "NYS", "AMS": "AMS",
                     "TSE": "TSE", "HKS": "HKS"}
        excd = excd_map.get(market, "NAS")
        r = requests.get(
            f"{self.quote_base}/uapi/overseas-price/v1/quotations/price",
            headers=self._headers("HHDFS00000300"), timeout=10,
            params={"AUTH": "", "EXCD": excd, "SYMB": symbol})
        body = _kis_check(r)
        last = body.get("output", {}).get("last", "0")
        try:
            return float(last)
        except (TypeError, ValueError):
            return 0.0

    # ── 주문 ──────────────────────────────────────────────────────────────────

    def _submit(self, symbol: str, qty: int, side: str,
                ord_dvsn: str, unit_price: int) -> dict:
        """주문 라우팅 — 시장에 따라 국내/해외 endpoint."""
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            return self._submit_domestic(symbol, qty, side, ord_dvsn, unit_price)
        return self._submit_overseas(symbol, qty, side, ord_dvsn, unit_price, market)

    def _submit_domestic(self, symbol: str, qty: int, side: str,
                          ord_dvsn: str, unit_price: int) -> dict:
        """국내주식 주문 — order-cash endpoint.

        ord_dvsn: 00=지정가, 01=시장가
        """
        if side == "buy":
            tr = "VTTC0802U" if self.virtual else "TTTC0802U"
        else:
            tr = "VTTC0801U" if self.virtual else "TTTC0801U"
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "PDNO": symbol, "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(unit_price) if ord_dvsn == "00" else "0",
        }
        r = requests.post(f"{self.base}/uapi/domestic-stock/v1/trading/order-cash",
                          headers=self._headers(tr), timeout=10, json=body)
        d = _kis_check(r)
        return {
            "success": d.get("rt_cd") == "0",
            "message": d.get("msg1", ""),
            "msg_cd": d.get("msg_cd", ""),
            "order_no": d.get("output", {}).get("ODNO", ""),
            "ord_branch": d.get("output", {}).get("KRX_FWDG_ORD_ORGNO", ""),
            "filled_qty": 0,
        }

    # 해외 매수/매도 TR_ID 매핑
    # ord_dvsn은 무시 (해외는 지정가만 지원, 시장가 별도 ORD_DVSN 코드)
    _OVERSEAS_TR = {
        # (market, side, virtual): TR_ID
        ("NAS", "buy",  True): "VTTT1002U", ("NAS", "buy",  False): "JTTT1002U",
        ("NAS", "sell", True): "VTTT1001U", ("NAS", "sell", False): "JTTT1001U",
        ("NYS", "buy",  True): "VTTT1002U", ("NYS", "buy",  False): "JTTT1002U",
        ("NYS", "sell", True): "VTTT1001U", ("NYS", "sell", False): "JTTT1001U",
        ("AMS", "buy",  True): "VTTT1002U", ("AMS", "buy",  False): "JTTT1002U",
        ("AMS", "sell", True): "VTTT1001U", ("AMS", "sell", False): "JTTT1001U",
        ("TSE", "buy",  True): "VTTS0308U", ("TSE", "buy",  False): "TTTS0308U",
        ("TSE", "sell", True): "VTTS0307U", ("TSE", "sell", False): "TTTS0307U",
        ("HKS", "buy",  True): "VTTS1002U", ("HKS", "buy",  False): "TTTS1002U",
        ("HKS", "sell", True): "VTTS1001U", ("HKS", "sell", False): "TTTS1001U",
    }
    _OVERSEAS_EXCD = {
        "NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX",
        "TSE": "TKSE", "HKS": "SEHK",
    }

    def _submit_overseas(self, symbol: str, qty: int, side: str,
                          ord_dvsn: str, unit_price: int, market: str) -> dict:
        """해외주식 주문 — overseas-stock/v1/trading/order endpoint.

        해외주식은 기본적으로 지정가. unit_price=0이면 호출 거부될 수 있어
        시장가 모드에서는 현재가 조회 후 사용.
        """
        tr = self._OVERSEAS_TR.get((market, side, self.virtual))
        if tr is None:
            return {"success": False, "message": f"미지원 시장: {market}",
                    "msg_cd": "", "order_no": "", "filled_qty": 0}
        if unit_price <= 0:
            # 시장가 의도 → 현재가로 대체 (해외는 지정가 강제).
            # 가격 조회 실패 시 1원으로 fallback하면 비정상 발주 위험(재정 손실)이라
            # 명시적 예외로 차단. 호출자(Trader._submit_buy/_submit_sell)는 이미
            # try/except로 감싸고 있어 decision_log에 'error'로 기록되며 발주는 보류.
            quoted = self._price_overseas(symbol, market)
            if quoted <= 0:
                raise RuntimeError(
                    f"해외 {market} {symbol} 현재가 조회 실패 ({quoted}) — "
                    f"지정가 발주를 위한 가격 없음. 주문 보류.")
            unit_price = int(quoted)
        excd = self._OVERSEAS_EXCD.get(market, "NASD")
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "OVRS_EXCG_CD": excd,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(unit_price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",       # 해외는 지정가 (00) 기본
        }
        r = requests.post(f"{self.base}/uapi/overseas-stock/v1/trading/order",
                          headers=self._headers(tr), timeout=15, json=body)
        d = _kis_check(r)
        return {
            "success": d.get("rt_cd") == "0",
            "message": d.get("msg1", ""),
            "msg_cd": d.get("msg_cd", ""),
            "order_no": d.get("output", {}).get("ODNO", ""),
            "ord_branch": "",
            "filled_qty": 0,
        }

    def buy(self, symbol: str, qty: int) -> dict:
        return self._submit(symbol, qty, "buy", "01", 0)

    def sell(self, symbol: str, qty: int) -> dict:
        return self._submit(symbol, qty, "sell", "01", 0)

    def buy_limit(self, symbol: str, qty: int, limit_price: int) -> dict:
        return self._submit(symbol, qty, "buy", "00", int(limit_price))

    def sell_limit(self, symbol: str, qty: int, limit_price: int) -> dict:
        return self._submit(symbol, qty, "sell", "00", int(limit_price))

    # ── 주문 취소 / 조회 ──────────────────────────────────────────────────────

    def cancel(self, order_no: str, symbol: str, qty: int,
               ord_branch: str = "") -> dict:
        """미체결 주문 전량 취소."""
        tr = "VTTC0803U" if self.virtual else "TTTC0803U"
        r = requests.post(
            f"{self.base}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=self._headers(tr), timeout=10, json={
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "KRX_FWDG_ORD_ORGNO": ord_branch or "",
                "ORGN_ODNO": order_no, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",       # 02 = 취소
                "ORD_QTY": str(qty), "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
            })
        d = _kis_check(r)
        return {"success": d.get("rt_cd") == "0",
                "message": d.get("msg1", ""),
                "msg_cd": d.get("msg_cd", "")}

    def _daily_ccld(self) -> dict:
        """당일 주문체결 조회 — 미체결·체결·취소 모두 포함."""
        tr = "VTTC8001R" if self.virtual else "TTTC8001R"
        today = datetime.now().strftime("%Y%m%d")
        r = requests.get(
            f"{self.base}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            headers=self._headers(tr), timeout=10, params={
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "INQR_STRT_DT": today, "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
                "PDNO": "", "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "", "ODNO": "",
                "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            })
        return _kis_check(r)

    def order_status(self, order_no: str) -> dict:
        """특정 주문번호의 현재 상태."""
        try:
            body = self._daily_ccld()
        except Exception as e:
            log.warning("주문 조회 실패: %s", e)
            return {"order_no": order_no, "status": "unknown",
                    "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in body.get("output1", []) or []:
            if row.get("odno") == order_no:
                ord_qty = int(row.get("ord_qty", 0) or 0)
                ccld_qty = int(row.get("tot_ccld_qty", 0) or 0)
                avg_px = float(row.get("avg_prvs", 0) or 0)
                cncl = row.get("cncl_yn", "") == "Y"
                if cncl:
                    status = "cancelled"
                elif ccld_qty >= ord_qty and ord_qty > 0:
                    status = "filled"
                elif ccld_qty > 0:
                    status = "partial"
                else:
                    status = "submitted"
                return {"order_no": order_no, "status": status,
                        "filled_qty": ccld_qty,
                        "remain_qty": max(0, ord_qty - ccld_qty),
                        "fill_price": avg_px,
                        "ord_branch": row.get("ord_gno_brno", "")}
        return {"order_no": order_no, "status": "unknown",
                "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def pending_orders(self) -> list[dict]:
        """현재 미체결 잔량이 있는 주문 목록."""
        try:
            body = self._daily_ccld()
        except Exception as e:
            log.warning("미체결 조회 실패: %s", e)
            return []
        out = []
        for row in body.get("output1", []) or []:
            ord_qty = int(row.get("ord_qty", 0) or 0)
            ccld_qty = int(row.get("tot_ccld_qty", 0) or 0)
            if row.get("cncl_yn", "") == "Y":
                continue
            remain = ord_qty - ccld_qty
            if remain <= 0:
                continue
            out.append({
                "order_no": row.get("odno", ""),
                "symbol": row.get("pdno", ""),
                "name": row.get("prdt_name", ""),
                "side": "buy" if row.get("sll_buy_dvsn_cd") == "02" else "sell",
                "qty": ord_qty, "filled_qty": ccld_qty, "remain_qty": remain,
                "limit_price": float(row.get("ord_unpr", 0) or 0),
                "ord_branch": row.get("ord_gno_brno", ""),
                "submitted_at": row.get("ord_tmd", ""),
            })
        return out
