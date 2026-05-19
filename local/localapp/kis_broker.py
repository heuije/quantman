"""KIS(한국투자증권) REST 브로커 — 모의투자(VTS) 연동.

자격증명은 keyring에서만 읽는다. Access Token은 APP_DIR에 캐싱(24h).
실전(virtual=False) TR_ID도 분기하지만 첫 릴리스는 모의투자만 사용한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import requests

from .config import APP_DIR
from .secrets_store import load_kis

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
        return d["access_token"]

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
            "appkey": self.key, "appsecret": self.secret,
            "tr_id": tr_id, "custtype": "P",
        }

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

    def price(self, symbol: str) -> float:
        # 시세 조회는 실전 도메인 사용 (모의투자 도메인은 시세 API 미지원)
        r = requests.get(
            f"{self.quote_base}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers("FHKST01010100"), timeout=10,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        body = _kis_check(r)
        return float(body.get("output", {}).get("stck_prpr", 0))

    # ── 주문 ──────────────────────────────────────────────────────────────────

    def _order(self, symbol: str, qty: int, side: str) -> dict:
        if side == "buy":
            tr = "VTTC0802U" if self.virtual else "TTTC0802U"
        else:
            tr = "VTTC0801U" if self.virtual else "TTTC0801U"
        r = requests.post(f"{self.base}/uapi/domestic-stock/v1/trading/order-cash",
                          headers=self._headers(tr), timeout=10, json={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "PDNO": symbol, "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty), "ORD_UNPR": "0",
        })
        d = _kis_check(r)
        return {"success": d.get("rt_cd") == "0",
                "message": d.get("msg1", ""),
                "order_no": d.get("output", {}).get("ODNO", "")}

    def buy(self, symbol: str, qty: int) -> dict:
        return self._order(symbol, qty, "buy")

    def sell(self, symbol: str, qty: int) -> dict:
        return self._order(symbol, qty, "sell")
