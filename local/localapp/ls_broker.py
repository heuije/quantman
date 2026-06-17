"""LS증권(구 이베스트투자증권) REST 브로커 — 국내주식(Phase 2).

KIS의 kis_broker.py와 대칭. 자격증명은 keyring에서만 읽고, access token은 APP_DIR에
캐싱한다(계정 지문 귀속). LS는 단일 도메인에서 모의/실전을 키로 라우팅한다(KIS의 도메인
분리와 다름 — docs/ls-api 참조).

⚠ 응답 필드명(블록명·rsp_cd 성공값·OutBlock 필드)은 키 발급 후 라이브 확정 전까지 '초안'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta

import requests

from .config import APP_DIR
from .secrets_store import load_ls
from .state_store import save_json

log = logging.getLogger("localapp.ls_broker")

# LS OpenAPI — 단일 도메인, 키로 모의/실전 분기(docs/ls-api GOTCHAS G2). KIS의 _VTS/_REAL 분리 불필요.
_BASE = "https://openapi.ls-sec.co.kr:8080"
_TOKEN_CACHE = APP_DIR / ".ls_token.json"

# LS 성공코드 — ⚠ 키검증 대상(현 가정 "00000"). 한 곳(SSOT)에서만 정의.
_RSP_OK = "00000"


class _Throttle:
    """sliding-window throttle. ⚠ LS TPS 미확인 → 보수적 3/s 시작, 검증 후 조정."""
    def __init__(self, max_calls: int = 3, window_sec: float = 1.0):
        self.max_calls, self.window_sec = max_calls, window_sec
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self.window_sec]
            if len(self._calls) >= self.max_calls:
                wait = self.window_sec - (now - self._calls[0]) + 0.01
                if wait > 0:
                    # NOTE: lock 보유 중 sleep — 단일 스레드 사이클 실행에선 무해(KIS _Throttle 동일).
                    time.sleep(wait)
                    now = time.monotonic()
                    self._calls = [t for t in self._calls if now - t < self.window_sec]
            self._calls.append(now)


_GLOBAL_THROTTLE = _Throttle()   # 프로세스 전역 — 같은 LS 계정 부담 공유


class LsBroker:
    """LS증권 모의/실전 브로커. Broker Protocol 구현(국내주식 Phase 2). 조회·주문은 B6."""

    def __init__(self):
        creds = load_ls()
        if not creds:
            raise RuntimeError("LS 자격증명이 없습니다. 먼저 setup으로 등록하세요.")
        self.key = creds["app_key"]
        self.secret = creds["app_secret"]
        self.virtual = creds.get("virtual", True)
        self.base = _BASE
        # LS 계좌번호 — 하이픈 제거 보수적 처리(⚠ 형식 docs/ls-api G6 검증 대상).
        self.account_no = str(creds["account_no"]).replace("-", "")
        self._token_fp = hashlib.sha256(
            f"{self.base}:{self.key}:{int(self.virtual)}".encode()).hexdigest()[:16]

    @staticmethod
    def _read_token_cache() -> dict:
        if not _TOKEN_CACHE.exists():
            return {}
        try:
            c = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
            return c if isinstance(c, dict) and "access_token" not in c else {}
        except Exception:
            return {}

    def _token(self) -> str:
        """access token — (도메인,appkey,virtual) 지문별 캐시. 만료 30분 마진 내 적중.

        grant_type=client_credentials. expires_in을 그대로 존중(LS 익일 07:00 만료를
        expires_in으로 인코딩 — 하드코딩 금지)."""
        cache = self._read_token_cache()
        ent = cache.get(self._token_fp)
        if ent and datetime.fromisoformat(ent["expires_at"]) > datetime.now() + timedelta(minutes=30):
            return ent["access_token"]
        r = requests.post(
            f"{self.base}/oauth2/token",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "appkey": self.key, "appsecretkey": self.secret,
                  "scope": "oob"},
            timeout=10)
        r.raise_for_status()
        d = r.json()
        cache[self._token_fp] = {
            "access_token": d["access_token"],
            "expires_at": (datetime.now()
                           + timedelta(seconds=int(d.get("expires_in", 86400)))).isoformat(),
        }
        save_json(_TOKEN_CACHE, cache)   # owner-only ACL + 원자적 저장
        return d["access_token"]

    def _headers(self, tr_cd: str, tr_cont: str = "N") -> dict:
        """LS REST 헤더 — api-id(tr_cd) + Bearer 토큰 + 연속조회 플래그."""
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token()}",
            "tr_cd": tr_cd, "tr_cont": tr_cont, "tr_cont_key": "",
        }

    def _post(self, path: str, tr_cd: str, body: dict, *,
              is_order: bool = False, timeout: int = 10, tries: int = 4) -> dict:
        """LS POST. read 조회는 일시 5xx/rate-limit 재시도, order는 멱등 아님 →
        rate-limit 접수전 거부에만 재시도(이중 발주 차단).

        ⚠ LS rate-limit 응답 형식 미확인 — 현재 HTTP 429/5xx만 재시도 대상. 만약 LS가
        rate-limit을 HTTP 200 + body(rsp_cd≠"00000")로 인코딩하면(KIS는 HTTP 500 +
        EGW00201) 아래 200 분기가 에러 body를 정상 반환으로 넘긴다 — 주문 경로는
        normalize_ls_order_resp(B6)가 rsp_cd로 거부 판정하므로 오체결은 없으나 rate-limit
        재시도는 누락된다. 정확 형식은 키 발급 후 docs/ls-api 확정(GOTCHAS)."""
        last = None
        for i in range(tries):
            _GLOBAL_THROTTLE.acquire()
            r = requests.post(f"{self.base}{path}", headers=self._headers(tr_cd),
                              json=body, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            # read는 일시 5xx/429 재시도; order는 429(접수전 거부)에만 — 5xx는 주문이
            # 이미 접수됐을 수 있어 재시도 금지(이중 발주 차단).
            retryable = r.status_code in (429, 500, 502, 503)
            if retryable and i < tries - 1 and (not is_order or r.status_code == 429):
                last = r
                time.sleep(0.3 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        last.raise_for_status()        # 재시도 소진 — 마지막 비정상 응답에서 raise
        raise RuntimeError("LS _post: 재시도 소진 후 도달 불가")  # unreachable 방어
