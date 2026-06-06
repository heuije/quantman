"""서버용 KIS *데이터* 클라이언트 (읽기 전용 시세) — 국내선물 일봉 등 백테스트 데이터 수집.

서버는 그동안 KIS 인증 호출이 없었다(공개 마스터 파일만 다운로드). 국내선물 일봉
(FHKIF03020100)은 인증이 필요해 이 최소 클라이언트를 둔다. 인증 흐름은 검증된 로컬
kis_broker(_token/_headers)와 동일: /oauth2/tokenP로 access_token 발급 → 헤더에 Bearer+
appkey/appsecret/tr_id.

⚠ 거래용 아님. 데이터(시세) 전용 앱키를 권장(거래 자격증명과 분리). 자격증명은 Railway
환경변수로만 주입(코드·리포에 없음):
  QP_KIS_DATA_APPKEY · QP_KIS_DATA_APPSECRET · (선택) QP_KIS_DATA_VIRTUAL=1 (모의 도메인)
env 미설정이면 get_kis_data_client()가 None을 반환 → 호출부는 no-op(fail-safe). 즉 키가
설정되기 전까지 이 경로는 *비활성*이라 기존 데이터 갱신에 영향 없음.

⚠ 미검증: 실제 KIS 연결(키 발급·토큰·헤더)은 자격증명이 있어야 검증 가능 — 키 설정 시점에
1회 검증 필요(아래 verify 런북). 인증 형태는 로컬 kis_broker에서 가져왔다.
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("app.kis_data")

_REAL = "https://openapi.koreainvestment.com:9443"
_MOCK = "https://openapivts.koreainvestment.com:29443"

# tr_id → REST path (데이터 전용 — 현재 국내선물 일봉만).
_TR_PATH = {
    "FHKIF03020100": "/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice",
}


class KisDataClient:
    """인증된 KIS GET 클라이언트(데이터 전용). request(tr_id, params) → JSON dict."""

    def __init__(self, appkey: str, appsecret: str, base: str):
        self.key, self.secret, self.base = appkey, appsecret, base
        self._tok: str | None = None
        self._tok_exp = 0.0

    def _token(self) -> str:
        if self._tok and time.time() < self._tok_exp - 60:
            return self._tok
        r = requests.post(f"{self.base}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": self.key, "appsecret": self.secret},
                          timeout=10)
        r.raise_for_status()
        d = r.json()
        self._tok = d["access_token"]
        self._tok_exp = time.time() + int(d.get("expires_in", 86400))
        return self._tok

    def request(self, tr_id: str, params: dict) -> dict:
        path = _TR_PATH.get(tr_id)
        if path is None:
            raise ValueError(f"미등록 데이터 tr_id: {tr_id}")
        headers = {"content-type": "application/json; charset=utf-8",
                   "authorization": f"Bearer {self._token()}",
                   "appkey": self.key, "appsecret": self.secret, "tr_id": tr_id}
        r = requests.get(f"{self.base}{path}", headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json()


def get_kis_data_client() -> KisDataClient | None:
    """env에 데이터 앱키가 있으면 클라이언트, 없으면 None(호출부 no-op — fail-safe)."""
    key = os.getenv("QP_KIS_DATA_APPKEY", "").strip()
    sec = os.getenv("QP_KIS_DATA_APPSECRET", "").strip()
    if not key or not sec:
        return None
    virtual = os.getenv("QP_KIS_DATA_VIRTUAL", "").lower() in ("1", "true", "yes")
    return KisDataClient(key, sec, _MOCK if virtual else _REAL)
