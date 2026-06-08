"""KIS 국내선물옵션 *거래* 브로커 (자동매매 #4) — 계약수 기반 주문·잔고.

기존 KisBroker(주식)와 별개: 선물옵션 계좌(상품코드 03)·전용 TR. 인증 흐름은 동일
(/oauth2/tokenP → Bearer+appkey/appsecret/tr_id). 단위는 *계약수*(주식 '주' 아님).

검증된 KIS 공식 spec([국내선물옵션] 주문_계좌·기본시세.xlsx):
- 주문  TTTO1101U(실전)/VTTO1101U(모의) POST /uapi/domestic-futureoption/v1/trading/order
        body: ORD_PRCS_DVSN_CD=02·CANO·ACNT_PRDT_CD=03·SLL_BUY_DVSN_CD(01매도/02매수)·
        SHTN_PDNO·ORD_QTY(계약수)·UNIT_PRICE·ORD_DVSN_CD(01지정가/02시장가)
- 잔고  CTFO6118R(실전)/VTFO6118R(모의) GET .../trading/inquire-balance
        output1=포지션(shtn_pdno·cblc_qty·sll_buy_dvsn_name·ccld_avg_unpr1·excc_unpr·evlu_pfls_amt)
- 시세  FHMIF10000000(실전·모의 공통) GET .../quotations/inquire-price
        output1.futs_prpr(현재가)·futs_oprc(시가)·futs_mxpr/futs_llam(상·하한 밴드)

⚠ 종목코드(SHTN_PDNO)는 **선물 6자리**(예: A01606=KOSPI200 202606). fo_idx_code.mst field-1
  `1A01606`(7자)에서 앞 `1`을 떼야 함 — 라이브 검증: A01606→주문가능, 1A01606→"모의투자 조회실패".

라이브 모의 검증(2026-06-08):
- ✅ 토큰·잔고(VTFO6118R, 필수파라미터 MGNA_DVSN 등)·시세(output1.futs_prpr) 정상. 펀딩계좌
  주문가능(VTTO5105R)로 심볼 확정. 시장가(ORD_DVSN_CD=02) spec 지원 확인.
- ⚠ 미검증(장 시간 필요): 주문 성공 응답(ODNO)·체결 후 populated 잔고 output1 형태. KIS spec은
  output1을 컬럼형(dict of arrays)으로 예시 — parse는 행형/컬럼형 양형 방어. 연속장 라운드트립 1회로 확정.

이 모듈은 *자동 Trader 루프에 아직 배선되지 않음*(standalone) — 임의 발주가 일어나지 않는다.
build/parse/params 순수함수는 단위검증됨. price/today_open(읽기전용 시세)은 라이브 검증됨.
"""
from __future__ import annotations

import json

import requests

from .kis_overseas_futures import build_overseas_order_body, parse_overseas_balance, scale_overseas_price


def _json(resp) -> dict:
    """KIS 응답을 UTF-8로 명시 디코딩. KIS는 charset=utf-8 본문을 주지만 requests 자동탐지가
    한글(종목명·메시지)을 U+FFFD로 깨뜨리는 경우가 있어 r.json() 대신 사용한다."""
    return json.loads(resp.content.decode("utf-8"))

_REAL = "https://openapi.koreainvestment.com:9443"
_VTS = "https://openapivts.koreainvestment.com:29443"
_ORDER_PATH = "/uapi/domestic-futureoption/v1/trading/order"
_BALANCE_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-balance"
_CANCEL_PATH = "/uapi/domestic-futureoption/v1/trading/order-rvsecncl"
_CCNL_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-ccnl"
_QUOTE_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-price"
_QUOTE_TR = "FHMIF10000000"   # 선물옵션 시세(실전·모의 공통)

# ── 해외선물옵션 상수 (OTFM/HHDFC TR, 실전 전용) ─────────────────────────────────
_OV_ORDER_PATH   = "/uapi/overseas-futureoption/v1/trading/order"
_OV_BALANCE_PATH = "/uapi/overseas-futureoption/v1/trading/inquire-unpd"
_OV_QUOTE_PATH   = "/uapi/overseas-futureoption/v1/quotations/inquire-price"
_OV_ORDER_TR     = "OTFM3001U"
_OV_BALANCE_TR   = "OTFM1412R"
_OV_QUOTE_TR     = "HHDFC55010000"


def build_futures_order_body(*, cano: str, acnt_prdt_cd: str, symbol: str,
                             qty: int, price, side: str, order_type: str = "limit") -> dict:
    """TTTO1101U/VTTO1101U 주문 바디(지정가·시장가). side: 'buy'|'sell', qty=계약수,
    price=지정가(limit) 또는 0(market). order_type='limit'(01)|'market'(02).

    순수함수 — 네트워크 없음(단위검증 대상). SLL_BUY_DVSN_CD: 02 매수 / 01 매도.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side는 buy|sell: {side}")
    if order_type not in ("limit", "market"):
        raise ValueError(f"order_type는 limit|market: {order_type}")
    _is_limit = order_type == "limit"

    return {
        "ORD_PRCS_DVSN_CD": "02",                         # 02: 주문전송
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,                     # 선물옵션 "03"
        "SLL_BUY_DVSN_CD": "02" if side == "buy" else "01",
        "SHTN_PDNO": symbol,                              # 단축상품번호(종목코드)
        "ORD_QTY": str(int(qty)),                         # 계약수
        "UNIT_PRICE": str(price) if _is_limit else "0",  # 시장가는 0
        "NMPR_TYPE_CD": "",
        "KRX_NMPR_CNDT_CD": "",
        "ORD_DVSN_CD": "01" if _is_limit else "02",      # 01 지정가 / 02 시장가
    }


def build_futures_cancel_body(*, cano: str, acnt_prdt_cd: str, order_no, qty: int) -> dict:
    """VTTO1103U/TTTO1103U 취소 바디(전량). 순수함수 — 단위검증 대상.

    취소: RVSE_CNCL_DVSN_CD=02·UNIT_PRICE=0·KRX_NMPR_CNDT_CD=0·ORD_DVSN_CD=01·RMN_QTY_YN=Y.
    ORD_QTY는 모의계좌 필수(전량이라도 입력).
    """
    return {
        "ORD_PRCS_DVSN_CD": "02",
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "RVSE_CNCL_DVSN_CD": "02",          # 02: 취소
        "ORGN_ODNO": str(order_no),
        "ORD_QTY": str(int(qty)),
        "UNIT_PRICE": "0",
        "NMPR_TYPE_CD": "",
        "KRX_NMPR_CNDT_CD": "0",
        "RMN_QTY_YN": "Y",                  # 전량
        "FUOP_ITEM_DVSN_CD": "",
        "ORD_DVSN_CD": "01",
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


def _balance_rows(output1) -> list[dict]:
    """잔고 output1을 행(dict) 리스트로 정규화.

    KIS는 일관되지 않음: 빈 잔고=[](array), 보유 시 공식 spec 예시는 컬럼형(dict of arrays,
    {shtn_pdno:[...], cblc_qty:[...], ...})이나 문서 type은 array. 행형(list of dicts)도 대비해
    양형 모두 처리한다(라이브 populated 형태는 연속장 검증 예정).
    """
    if isinstance(output1, list):
        return [r for r in output1 if isinstance(r, dict)]
    if isinstance(output1, dict):
        cols = {k: v for k, v in output1.items() if isinstance(v, list)}
        if not cols:
            return []
        n = max(len(v) for v in cols.values())
        return [{k: (v[i] if i < len(v) else None) for k, v in cols.items()} for i in range(n)]
    return []


def parse_futures_balance(resp: dict) -> dict:
    """잔고 응답 → {positions:[{symbol,side,qty,avg_price,settle_price,eval_pnl}], account:{...}}.

    output1=종목별 포지션(0계약 제외), output2=계좌 요약(주문가능현금·증거금·예수금·평가손익).
    symbol=shtn_pdno(6자 거래코드), side=롱/숏(sll_buy_dvsn_name), settle_price=정산단가(excc_unpr),
    eval_pnl=평가손익(evlu_pfls_amt). 키는 KIS 공식 spec(VTFO6118R) 기준.
    """
    positions = []
    for r in _balance_rows(resp.get("output1")):
        try:
            qty = int(float(r.get("cblc_qty", 0) or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty == 0:
            continue
        side_name = str(r.get("sll_buy_dvsn_name", "") or "").strip()
        side = "sell" if side_name in ("매도", "SLL") else ("buy" if side_name in ("매수", "BUY") else "")
        positions.append({
            "symbol": str(r.get("shtn_pdno", "") or "").strip(),   # 6자 단축 거래코드
            "side": side,                                          # 롱(buy)/숏(sell)
            "qty": qty,
            "avg_price": float(r.get("ccld_avg_unpr1", 0) or 0),   # 체결평균단가
            "settle_price": float(r.get("excc_unpr", 0) or 0),     # 정산단가
            "eval_pnl": float(r.get("evlu_pfls_amt", 0) or 0),     # 평가손익(미실현)
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


def parse_ccnl_order_status(resp: dict, order_no) -> dict:
    """inquire-ccnl output1에서 order_no 행 → {order_no,status,filled_qty,remain_qty,fill_price}.

    canonical odno 비교(lstrip "0"). status: rejected(rjct>0) / filled(잔량0·체결>0) /
    partial(체결>0) / submitted(체결0) / unknown(행 없음).
    """
    rows = resp.get("output1")
    if not isinstance(rows, list):
        rows = []
    target = str(order_no).lstrip("0")
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("odno", "")).lstrip("0") == target:
            def _i(k):
                try:
                    return int(float(r.get(k, 0) or 0))
                except (ValueError, TypeError):
                    return 0
            filled, remain, rjct = _i("tot_ccld_qty"), _i("qty"), _i("rjct_qty")
            if rjct > 0:
                status = "rejected"
            elif remain == 0 and filled > 0:
                status = "filled"
            elif filled > 0:
                status = "partial"
            else:
                status = "submitted"
            return {"order_no": str(r.get("odno", "")), "status": status,
                    "filled_qty": filled, "remain_qty": remain,
                    "fill_price": float(r.get("avg_idx", 0) or 0)}
    return {"order_no": str(order_no), "status": "unknown",
            "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}


class KisFuturesBroker:
    """국내선물옵션 거래 클라이언트(계약수 기반). 선물옵션 계좌 자격증명을 로컬에서 읽는다.

    ⚠ standalone — Trader 자동 루프에 배선되지 않음(임의 발주 없음). 모의 검증 후 배선(#4 phase2).
    """

    def __init__(self):
        # 지연 import — 순수 헬퍼는 keyring 없이 테스트 가능
        from .secrets_store import load_kis_futures, load_kis_overseas_futures

        domestic = load_kis_futures()
        overseas = load_kis_overseas_futures()

        if not domestic and not overseas:
            raise RuntimeError(
                "KIS 자격증명이 없습니다. "
                "국내선물: secrets_store.save_kis_futures, "
                "해외선물: secrets_store.save_kis_overseas_futures 로 등록하세요."
            )

        # ── 국내선물 컨텍스트 (없으면 None — 국내 메서드 호출 시 명시 오류) ────────────
        if domestic:
            self.key = domestic["app_key"]
            self.secret = domestic["app_secret"]
            self.virtual = domestic.get("virtual", True)
            self.base = _VTS if self.virtual else _REAL
            no = str(domestic["account_no"]).split("-")
            self.cano, self.acnt_prdt_cd = no[0], (no[1] if len(no) > 1 else "03")
            self._tok = None
            self._tok_exp = 0.0
        else:
            self.key = self.secret = self.base = self.cano = self.acnt_prdt_cd = None
            self.virtual = None
            self._tok = None
            self._tok_exp = 0.0

        # ── 해외선물 컨텍스트 (없으면 None — 해외 메서드 호출 시 명시 오류) ────────────
        if overseas:
            self._ov_key = overseas["app_key"]
            self._ov_secret = overseas["app_secret"]
            ov_no = str(overseas["account_no"]).split("-")
            self._ov_cano = ov_no[0]
            self._ov_acnt_prdt_cd = ov_no[1] if len(ov_no) > 1 else "08"
            self._ov_base = _REAL   # 해외선물은 실전 전용
            self._ov_tok = None
            self._ov_tok_exp = 0.0
        else:
            self._ov_key = None

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

    # ── 해외선물 인증 (실전 전용, 토큰 캐시) ─────────────────────────────────────────

    def _ov_token(self) -> str:
        import time
        if self._ov_tok and time.time() < self._ov_tok_exp - 60:
            return self._ov_tok
        r = requests.post(f"{self._ov_base}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": self._ov_key, "appsecret": self._ov_secret},
                          timeout=10)
        r.raise_for_status()
        d = _json(r)
        self._ov_tok = d["access_token"]
        self._ov_tok_exp = time.time() + int(d.get("expires_in", 86400))
        return self._ov_tok

    def _ov_headers(self, tr_id: str) -> dict:
        return {"content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self._ov_token()}",
                "appkey": self._ov_key, "appsecret": self._ov_secret,
                "tr_id": tr_id, "custtype": "P"}

    # ── Broker Protocol(계약수 기반) — 핵심 거래 메서드 ───────────────────────────

    def buy_limit(self, symbol: str, qty: int, limit_price) -> dict:
        return self._submit_order(symbol, qty, limit_price, "buy")

    def sell_limit(self, symbol: str, qty: int, limit_price) -> dict:
        return self._submit_order(symbol, qty, limit_price, "sell")

    def _submit_order(self, symbol: str, qty: int, price, side: str,
                      order_type: str = "limit") -> dict:
        body = build_futures_order_body(cano=self.cano, acnt_prdt_cd=self.acnt_prdt_cd,
                                        symbol=symbol, qty=qty, price=price, side=side,
                                        order_type=order_type)
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

    # ── 시세(읽기전용, 라이브 검증됨) ──────────────────────────────────────────────
    def _quote(self, symbol: str) -> dict:
        """선물옵션 시세 output1(현재가·시가·밴드 등). FID: F=지수선물."""
        r = requests.get(f"{self.base}{_QUOTE_PATH}", headers=self._headers(_QUOTE_TR),
                         params={"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": symbol}, timeout=10)
        r.raise_for_status()
        return _json(r).get("output1") or {}

    def price(self, symbol: str) -> float:
        return float(self._quote(symbol).get("futs_prpr", 0) or 0)        # 현재가

    def today_open(self, symbol: str) -> float:
        return float(self._quote(symbol).get("futs_oprc", 0) or 0)        # 당일 시가

    # ── 해외선물 거래 메서드 (CME via KIS, 실전 전용) ─────────────────────────────────

    def overseas_buy_limit(self, symbol: str, qty: int, limit_price) -> dict:
        body = build_overseas_order_body(cano=self._ov_cano, acnt_prdt_cd=self._ov_acnt_prdt_cd,
                                         symbol=symbol, side="buy", qty=qty,
                                         price=limit_price, order_type="limit")
        r = requests.post(f"{self._ov_base}{_OV_ORDER_PATH}",
                          headers=self._ov_headers(_OV_ORDER_TR), json=body, timeout=10)
        r.raise_for_status()
        return _json(r)

    def overseas_sell_limit(self, symbol: str, qty: int, limit_price) -> dict:
        body = build_overseas_order_body(cano=self._ov_cano, acnt_prdt_cd=self._ov_acnt_prdt_cd,
                                         symbol=symbol, side="sell", qty=qty,
                                         price=limit_price, order_type="limit")
        r = requests.post(f"{self._ov_base}{_OV_ORDER_PATH}",
                          headers=self._ov_headers(_OV_ORDER_TR), json=body, timeout=10)
        r.raise_for_status()
        return _json(r)

    def overseas_buy(self, symbol: str, qty: int) -> dict:
        body = build_overseas_order_body(cano=self._ov_cano, acnt_prdt_cd=self._ov_acnt_prdt_cd,
                                         symbol=symbol, side="buy", qty=qty,
                                         price=0, order_type="market")
        r = requests.post(f"{self._ov_base}{_OV_ORDER_PATH}",
                          headers=self._ov_headers(_OV_ORDER_TR), json=body, timeout=10)
        r.raise_for_status()
        return _json(r)

    def overseas_sell(self, symbol: str, qty: int) -> dict:
        body = build_overseas_order_body(cano=self._ov_cano, acnt_prdt_cd=self._ov_acnt_prdt_cd,
                                         symbol=symbol, side="sell", qty=qty,
                                         price=0, order_type="market")
        r = requests.post(f"{self._ov_base}{_OV_ORDER_PATH}",
                          headers=self._ov_headers(_OV_ORDER_TR), json=body, timeout=10)
        r.raise_for_status()
        return _json(r)

    def overseas_account_snapshot(self) -> dict:
        params = {"CANO": self._ov_cano, "ACNT_PRDT_CD": self._ov_acnt_prdt_cd,
                  "FUOP_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        r = requests.get(f"{self._ov_base}{_OV_BALANCE_PATH}",
                         headers=self._ov_headers(_OV_BALANCE_TR), params=params, timeout=10)
        r.raise_for_status()
        return parse_overseas_balance(_json(r))

    def overseas_price(self, symbol: str, scalc_desz: int = 0) -> float:
        r = requests.get(f"{self._ov_base}{_OV_QUOTE_PATH}",
                         headers=self._ov_headers(_OV_QUOTE_TR),
                         params={"SRS_CD": symbol}, timeout=10)
        r.raise_for_status()
        raw = _json(r).get("output1", {}).get("last_price", "")
        return scale_overseas_price(raw, scalc_desz)

    def buy(self, symbol: str, qty: int) -> dict:
        return self._submit_order(symbol, qty, 0, "buy", order_type="market")

    def sell(self, symbol: str, qty: int) -> dict:
        return self._submit_order(symbol, qty, 0, "sell", order_type="market")

    # ── phase 2 (연속장 라이브 검증 후 구현) — 정정취소·체결조회 ──────────────────────
    # spec 확보 완료(취소 order-rvsecncl, 체결조회 inquire-ccnl).
    # 추측 발주 방지를 위해 라이브 라운드트립 검증 전까지 미구현 유지.

    def cancel(self, order_no: str, symbol: str, qty: int) -> dict:
        body = build_futures_cancel_body(cano=self.cano, acnt_prdt_cd=self.acnt_prdt_cd,
                                         order_no=order_no, qty=qty)
        tr = "VTTO1103U" if self.virtual else "TTTO1103U"
        r = requests.post(f"{self.base}{_CANCEL_PATH}", headers=self._headers(tr),
                          json=body, timeout=10)
        r.raise_for_status()
        return _json(r)

    def _inquire_ccnl(self, only_unfilled: bool = False) -> dict:
        """inquire-ccnl 조회(당일). only_unfilled=True면 미체결만."""
        import datetime
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y%m%d")
        tr = "VTTO5201R" if self.virtual else "TTTO5201R"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                  "STRT_ORD_DT": today, "END_ORD_DT": today,
                  "SLL_BUY_DVSN_CD": "00", "CCLD_NCCS_DVSN": "02" if only_unfilled else "00",
                  "SORT_SQN": "DS", "STRT_ODNO": "", "PDNO": "", "MKET_ID_CD": "00",
                  "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        r = requests.get(f"{self.base}{_CCNL_PATH}", headers=self._headers(tr),
                         params=params, timeout=10)
        r.raise_for_status()
        return _json(r)

    def order_status(self, order_no: str) -> dict:
        return parse_ccnl_order_status(self._inquire_ccnl(), order_no)

    def pending_orders(self) -> list[dict]:
        rows = self._inquire_ccnl(only_unfilled=True).get("output1") or []
        return [parse_ccnl_order_status({"output1": [r]}, r.get("odno", ""))
                for r in rows if isinstance(r, dict)]
