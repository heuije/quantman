"""KIS 국내선물옵션 *거래* 브로커 (자동매매 #4) — 계약수 기반 주문·잔고.

기존 KisBroker(주식)와 별개: 선물옵션 계좌(상품코드 03)·전용 TR. 인증 흐름은 동일
(/oauth2/tokenP → Bearer+appkey/appsecret/tr_id). 단위는 *계약수*(주식 '주' 아님).

검증된 KIS 공식 spec(국내선물옵션_주문_계좌.xlsx):
- 주문  TTTO1101U(실전)/VTTO1101U(모의) POST /uapi/domestic-futureoption/v1/trading/order
        body: ORD_PRCS_DVSN_CD=02·CANO·ACNT_PRDT_CD=03·SLL_BUY_DVSN_CD(01매도/02매수)·
        SHTN_PDNO·ORD_QTY(계약수)·UNIT_PRICE(지정가)·ORD_DVSN_CD(01지정가)
- 잔고  CTFO6118R(실전)/VTFO6118R(모의) GET .../trading/inquire-balance
        output: pdno·cblc_qty(계약수)·ccld_avg_unpr1(평단)·excc_unpr(정산가)·trad_pfls_amt(손익)

라이브 모의 검증(2026-06-08, VTFO6118R):
- ✅ 토큰 발급·잔고조회 정상. 잔고 필수 파라미터 MGNA_DVSN·EXCC_STAT_CD·CTX_AREA_FK/NK200
  확정(누락 시 KIS가 'INPUT_FIELD_NAME MGNA_DVSN'로 거절). output1=포지션 list·output2=계좌요약 dict.
- ⚠ 미검증(파생 모의계좌 증거금 0이라 보류): 주문 성공 응답(ODNO)·포지션 행 키(output1[0])·
  시장가 ORD_DVSN_CD(모의 미지원 가능성). 파생상품 모의투자 충전 후 1회 검증 필요.

이 모듈은 *자동 Trader 루프에 아직 배선되지 않음*(standalone) — 임의 발주가 일어나지 않는다.
build/parse/params 순수함수는 단위검증됨.
"""
from __future__ import annotations

import json

import requests


def _json(resp) -> dict:
    """KIS 응답을 UTF-8로 명시 디코딩. KIS는 charset=utf-8 본문을 주지만 requests 자동탐지가
    한글(종목명·메시지)을 U+FFFD로 깨뜨리는 경우가 있어 r.json() 대신 사용한다."""
    return json.loads(resp.content.decode("utf-8"))

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


def build_balance_params(cano: str, acnt_prdt_cd: str) -> dict:
    """VTFO6118R/CTFO6118R 잔고조회 파라미터. 순수함수 — 단위검증 대상.

    라이브 모의 검증: MGNA_DVSN·EXCC_STAT_CD·CTX_AREA_FK200·CTX_AREA_NK200 필수
    (누락 시 KIS가 'INPUT_FIELD_NAME MGNA_DVSN'로 거절). MGNA_DVSN 01=위탁증거금.
    """
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,             # 선물옵션 "03"
        "MGNA_DVSN": "01",                        # 01: 위탁증거금
        "EXCC_STAT_CD": "1",                      # 1: 정산가 기준
        "CTX_AREA_FK200": "",                     # 연속조회 키(최초 공란)
        "CTX_AREA_NK200": "",
    }


def parse_futures_balance(resp: dict) -> dict:
    """잔고 응답 → {positions:[{symbol,qty,avg_price,eval_price,pnl}], account:{...}}.

    output1=종목별 포지션 배열(0계약 제외), output2=계좌 요약(주문가능현금·증거금·예수금·평가손익).
    output1=list·output2=dict 형태는 라이브 모의(VTFO6118R)로 확정. 단 포지션 행 키(cblc_qty 등)는
    보유 포지션이 없어 KIS 공식 spec 기준(라이브 미확정 — 파생 모의 충전 후 검증).
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

    summary = resp.get("output2")
    if isinstance(summary, list):                 # 일부 TR은 output2를 단일원소 배열로 반환
        summary = summary[0] if summary else {}
    if not isinstance(summary, dict):
        summary = {}

    def _num(key):
        try:
            return float(summary.get(key, 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    account = {
        "order_cash": _num("ord_psbl_cash"),      # 주문가능현금
        "margin_total": _num("mgna_tota"),        # 증거금 합계
        "deposit_cash": _num("dnca_cash"),        # 예수금(현금)
        "eval_pnl": _num("futr_evlu_pfls_amt"),   # 선물 평가손익
    }
    return {"positions": positions, "account": account}


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
        d = _json(r)
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
        return _json(r)

    def account_snapshot(self) -> dict:
        params = build_balance_params(self.cano, self.acnt_prdt_cd)
        r = requests.get(f"{self.base}{_BALANCE_PATH}", headers=self._headers(self._balance_tr()),
                         params=params, timeout=10)
        r.raise_for_status()
        return parse_futures_balance(_json(r))

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
