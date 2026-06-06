"""KIS 국내선물옵션 *거래* 브로커 (자동매매 #4) — 계약수 기반 주문·잔고.

기존 KisBroker(주식)와 별개: 선물옵션 계좌(상품코드 03)·전용 TR. 인증 흐름은 동일
(/oauth2/tokenP → Bearer+appkey/appsecret/tr_id). 단위는 *계약수*(주식 '주' 아님).

검증된 KIS 공식 spec(국내선물옵션_주문_계좌.xlsx):
- 주문  TTTO1101U(실전)/VTTO1101U(모의) POST /uapi/domestic-futureoption/v1/trading/order
        body: ORD_PRCS_DVSN_CD=02·CANO·ACNT_PRDT_CD=03·SLL_BUY_DVSN_CD(01매도/02매수)·
        SHTN_PDNO·ORD_QTY(계약수)·UNIT_PRICE(지정가)·ORD_DVSN_CD(01지정가)
- 잔고  CTFO6118R(실전)/VTFO6118R(모의) GET .../trading/inquire-balance
        output: pdno·cblc_qty(계약수)·ccld_avg_unpr1(평단)·excc_unpr(정산가)·trad_pfls_amt(손익)

⚠ 미검증: 실제 KIS 연결(토큰·주문 응답·잔고 output1/2 구조·시장가 ORD_DVSN_CD)은 자격증명이
있어야 검증 가능 — **국내선물 모의(virtual=True)부터** 1회 검증 후 실전. 이 모듈은 *자동 Trader
루프에 아직 배선되지 않음*(standalone) — 임의 발주가 일어나지 않는다. build/parse 순수함수는
모의 응답으로 단위검증됨.
"""
from __future__ import annotations

import requests

_REAL = "https://openapi.koreainvestment.com:9443"
_VTS = "https://openapivts.koreainvestment.com:29443"
_ORDER_PATH = "/uapi/domestic-futureoption/v1/trading/order"
_BALANCE_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-balance"


def build_futures_order_body(*, cano: str, acnt_prdt_cd: str, symbol: str,
                             qty: int, price, side: str) -> dict:
    """TTTO1101U/VTTO1101U 주문 바디(지정가). side: 'buy'|'sell', qty=계약수, price=지정가.

    순수함수 — 네트워크 없음(단위검증 대상). SLL_BUY_DVSN_CD: 02 매수 / 01 매도.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side는 buy|sell: {side}")
    return {
        "ORD_PRCS_DVSN_CD": "02",                         # 02: 주문전송
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,                     # 선물옵션 "03"
        "SLL_BUY_DVSN_CD": "02" if side == "buy" else "01",
        "SHTN_PDNO": symbol,                              # 단축상품번호(종목코드)
        "ORD_QTY": str(int(qty)),                         # 계약수
        "UNIT_PRICE": str(price),                         # 지정가 가격
        "NMPR_TYPE_CD": "",
        "KRX_NMPR_CNDT_CD": "",
        "ORD_DVSN_CD": "01",                              # 01: 지정가
    }


def parse_futures_balance(resp: dict) -> dict:
    """CTFO6118R 응답 → {positions:[{symbol,qty,avg_price,eval_price,pnl}]}.

    종목별 잔고 배열(output1 추정). 0계약 종목은 제외. ⚠ output1/output2 키는 모의 응답으로 확정 필요.
    """
    holdings = resp.get("output1")
    if not isinstance(holdings, list):
        # 일부 TR은 단일 output. 배열을 찾아 폴백.
        holdings = next((v for v in resp.values() if isinstance(v, list)), [])
    positions = []
    for r in holdings:
        if not isinstance(r, dict):
            continue
        try:
            qty = int(float(r.get("cblc_qty", 0) or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty == 0:
            continue
        positions.append({
            "symbol": str(r.get("pdno", "")).strip(),
            "qty": qty,
            "avg_price": float(r.get("ccld_avg_unpr1", 0) or 0),
            "eval_price": float(r.get("excc_unpr", 0) or 0),
            "pnl": float(r.get("trad_pfls_amt", 0) or 0),
        })
    return {"positions": positions}


class KisFuturesBroker:
    """국내선물옵션 거래 클라이언트(계약수 기반). 선물옵션 계좌 자격증명을 로컬에서 읽는다.

    ⚠ standalone — Trader 자동 루프에 배선되지 않음(임의 발주 없음). 모의 검증 후 배선(#4 phase2).
    """

    def __init__(self):
        from .secrets_store import load_kis_futures   # 지연 import — 순수 헬퍼는 keyring 없이 테스트 가능
        creds = load_kis_futures()
        if not creds:
            raise RuntimeError("선물옵션 KIS 자격증명이 없습니다 — secrets_store.save_kis_futures로 등록(모의 먼저).")
        self.key = creds["app_key"]
        self.secret = creds["app_secret"]
        self.virtual = creds.get("virtual", True)
        self.base = _VTS if self.virtual else _REAL
        no = str(creds["account_no"]).split("-")
        self.cano, self.acnt_prdt_cd = no[0], (no[1] if len(no) > 1 else "03")
        self._tok = None
        self._tok_exp = 0.0

    def _token(self) -> str:
        import time
        if self._tok and time.time() < self._tok_exp - 60:
            return self._tok
        r = requests.post(f"{self.base}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": self.key, "appsecret": self.secret}, timeout=10)
        r.raise_for_status()
        d = r.json()
        self._tok = d["access_token"]
        self._tok_exp = time.time() + int(d.get("expires_in", 86400))
        return self._tok

    def _headers(self, tr_id: str) -> dict:
        return {"content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self._token()}",
                "appkey": self.key, "appsecret": self.secret,
                "tr_id": tr_id, "custtype": "P"}

    def _order_tr(self) -> str:
        return "VTTO1101U" if self.virtual else "TTTO1101U"

    def _balance_tr(self) -> str:
        return "VTFO6118R" if self.virtual else "CTFO6118R"

    # ── Broker Protocol(계약수 기반) — 핵심 거래 메서드 ───────────────────────────

    def buy_limit(self, symbol: str, qty: int, limit_price) -> dict:
        return self._submit_order(symbol, qty, limit_price, "buy")

    def sell_limit(self, symbol: str, qty: int, limit_price) -> dict:
        return self._submit_order(symbol, qty, limit_price, "sell")

    def _submit_order(self, symbol: str, qty: int, price, side: str) -> dict:
        body = build_futures_order_body(cano=self.cano, acnt_prdt_cd=self.acnt_prdt_cd,
                                        symbol=symbol, qty=qty, price=price, side=side)
        r = requests.post(f"{self.base}{_ORDER_PATH}", headers=self._headers(self._order_tr()),
                          json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def account_snapshot(self) -> dict:
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd}
        r = requests.get(f"{self.base}{_BALANCE_PATH}", headers=self._headers(self._balance_tr()),
                         params=params, timeout=10)
        r.raise_for_status()
        return parse_futures_balance(r.json())

    # ── phase 2 (모의 검증 후 구현) — 시장가·정정취소·체결조회·실시간·시세 ──────────
    def buy(self, symbol: str, qty: int) -> dict:
        raise NotImplementedError("선물 시장가 ORD_DVSN_CD 모의 확인 후 구현(phase2). buy_limit 사용.")

    def sell(self, symbol: str, qty: int) -> dict:
        raise NotImplementedError("선물 시장가 ORD_DVSN_CD 모의 확인 후 구현(phase2). sell_limit 사용.")

    def cancel(self, order_no: str, symbol: str, qty: int) -> dict:
        raise NotImplementedError("정정취소 TTTO1103U/VTTO1103U — phase2.")

    def order_status(self, order_no: str) -> dict:
        raise NotImplementedError("주문체결내역 TTTO5201R/VTTO5201R — phase2.")

    def pending_orders(self) -> list[dict]:
        raise NotImplementedError("미체결 조회 — phase2(TTTO5201R 기반).")

    def price(self, symbol: str) -> float:
        raise NotImplementedError("선물 실시간 시세 H0IFCNT0 — phase2.")

    def today_open(self, symbol: str) -> float:
        raise NotImplementedError("선물 당일 시가 — phase2.")
