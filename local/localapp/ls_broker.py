"""LS증권(구 이베스트투자증권) REST 브로커 — 국내주식(Phase 2).

KIS의 kis_broker.py와 대칭. 자격증명은 keyring에서만 읽고, access token은 APP_DIR에
캐싱한다(계정 지문 귀속). LS는 단일 도메인에서 모의/실전을 키로 라우팅한다(KIS의 도메인
분리와 다름 — docs/ls-api 참조).

⚠ 응답 필드명(블록명·rsp_cd 성공값·OutBlock 필드)은 키 발급 후 라이브 확정 전까지 '초안'.
  B6 구현 전체가 '초안(draft)' — Phase C 키 발급 후 docs/ls-api 실측으로 교체 필요.
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
from . import market_index

log = logging.getLogger("localapp.ls_broker")

# LS OpenAPI — 단일 도메인, 키로 모의/실전 분기(docs/ls-api GOTCHAS G2). KIS의 _VTS/_REAL 분리 불필요.
_BASE = "https://openapi.ls-sec.co.kr:8080"
_TOKEN_CACHE = APP_DIR / ".ls_token.json"


class _Throttle:
    """sliding-window throttle. LS 비공식 실측 ~2 req/s, 공식 미공개 → Phase C 확정."""
    def __init__(self, max_calls: int = 2, window_sec: float = 1.0):
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


class _LsAuth:
    """LS OpenAPI 인증/HTTP 베이스 — LsBroker·LsFuturesBroker 공통 재사용.

    OAuth 토큰(client_credentials)·throttle·_post를 담는다. 국내주식·선물 모두
    동일 도메인·동일 인증 방식이라 여기 한 번만 구현한다(DRY).

    creds dict 필수 키: app_key, app_secret, account_no.
    선택 키: virtual(기본 True).
    """

    def __init__(self, creds: dict) -> None:
        if not creds:
            raise RuntimeError("LS 자격증명이 없습니다. setup에서 등록하세요.")
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

    def _token(self, force: bool = False) -> str:
        """access token — (도메인,appkey,virtual) 지문별 캐시. 만료 30분 마진 내 적중.

        grant_type=client_credentials. expires_in을 그대로 존중(LS 익일 07:00 만료를
        expires_in으로 인코딩 — 하드코딩 금지).

        force=True면 캐시를 건너뛰고 새 토큰을 발급한다 — 서버가 *만료 전* 토큰을
        무효화(모의 중복로그인 제한 등)해 데이터 endpoint가 500을 반환할 때 _post가
        재인증하는 경로(2026-06-22 실측: 만료 전 캐시 토큰 500, 새 토큰 200)."""
        cache = self._read_token_cache()
        ent = cache.get(self._token_fp)
        if not force and ent and datetime.fromisoformat(ent["expires_at"]) > datetime.now() + timedelta(minutes=30):
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

    def _headers(self, tr_cd: str, tr_cont: str = "N", force_token: bool = False) -> dict:
        """LS REST 헤더 — api-id(tr_cd) + Bearer 토큰 + 연속조회 플래그.

        force_token=True면 새 토큰으로 재인증(_post의 토큰무효화 500 자가복구 경로)."""
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token(force=force_token)}",
            "tr_cd": tr_cd, "tr_cont": tr_cont, "tr_cont_key": "",
        }

    def _post(self, path: str, tr_cd: str, body: dict, *,
              is_order: bool = False, timeout: int = 10, tries: int = 4) -> dict:
        """LS POST. **LS는 오류를 HTTP 500 + 본문 rsp_cd로 담는다**(실측 2026-06-22):
          · IGW40014 = 요청필드 오류(예: RecCnt를 int로 보냄) — 재시도·재인증 무의미 → 즉시 실패.
          · IGW00201 = 호출한도 초과(rate limit) — **재인증 금지**(토큰 발급이 한도를 더 악화) → backoff 재시도만.
          · 그 외 500 = 토큰 무효화 추정(모의 중복로그인 등, 만료 전 무효화) — read는 새 토큰 1회 재인증해 자가복구.
        order는 5xx에 재시도·재인증 안 함 — 주문이 이미 접수됐을 수 있어 이중 발주 차단
        (사이클은 주문 전 read[잔고]가 토큰을 먼저 갱신하므로 주문은 새 토큰을 쓴다)."""
        last = None
        force_token = False
        reauthed = False
        for i in range(tries):
            _GLOBAL_THROTTLE.acquire()
            r = requests.post(f"{self.base}{path}", headers=self._headers(tr_cd, force_token=force_token),
                              json=body, timeout=timeout)
            force_token = False        # one-shot — 재인증 후엔 새 토큰이 캐시돼 일반 경로
            if r.status_code == 200:
                return r.json()
            if r.status_code == 500 and not is_order:
                emsg = r.text or ""
                if "IGW40014" in emsg or "is neither a decimal" in emsg:
                    r.raise_for_status()             # 요청 필드 오류 — 재시도·재인증 무의미, 즉시 실패
                if "IGW00201" not in emsg and not reauthed:
                    reauthed = True                  # 토큰 무효화 추정 → 새 토큰 1회 재인증
                    force_token = True
                    continue
                # IGW00201(rate limit) 또는 재인증 후 실패 → 재인증 않고 backoff 재시도
            # read는 일시 5xx/429 재시도; order는 429(접수전 거부)에만.
            retryable = r.status_code in (429, 500, 502, 503)
            if retryable and i < tries - 1 and (not is_order or r.status_code == 429):
                last = r
                time.sleep(0.3 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        last.raise_for_status()        # 재시도 소진 — 마지막 비정상 응답에서 raise
        raise RuntimeError("LS _post: 재시도 소진 후 도달 불가")  # unreachable 방어


class LsBroker(_LsAuth):
    """LS증권 모의/실전 브로커. Broker Protocol 구현(국내주식 Phase 2).

    ⚠ B6 조회·주문 메서드 전체 초안(draft) — Phase C 키 발급 후 실측 확정.
    """

    def __init__(self):
        super().__init__(load_ls())
        # 해외주식 모의 미제공(IGW40014/002US 또는 01900/"모의투자에서는 해당업무가 제공되지 않습니다")
        # 감지 시 True — 이후 overseas 호출을 skip해 매 사이클 500 로그 스팸·rate-limit 낭비 방지.
        # ⚠ 일시적 실패(네트워크 등)는 이 플래그를 설정하지 않는다 — 영구 시그니처만 캐시.
        self._overseas_unavailable: bool = False

    # ── ⚠ B6 구현 이하 전체 초안 — Phase C 키 발급 후 실측 확정 필요 ──────────────

    # ── 잔고·포지션 조회 (t0424) ─────────────────────────────────────────────────

    def _balance_raw(self) -> dict:
        """t0424 주식잔고조회2 — 보유 포지션·잔고 요약.

        ⚠ PATH "/stock/accno" — A2 KB 가정. Phase C 실측 확정.
        ⚠ InBlock 필드명(prcgb/chegb/dangb/charge) — A2 KB 가정.
        """
        return self._post(
            "/stock/accno", "t0424",
            {"t0424InBlock": {
                "prcgb": "1",   # 평균단가 ⚠
                "chegb": "2",   # 체결기준잔고 (결제 전이지만 체결된 것 포함) ⚠
                "dangb": "0",   # 정규장 ⚠
                "charge": "1",  # 제비용 포함 ⚠
                "cts_expcode": "",
            }},
        )

    def _orderable_cash_krw(self):
        """CSPAQ22200 현금주문가능금액(MnyOrdAbleAmt) — 정산중 매도대금까지 반영한 매수여력.

        t0424 sunamt1(현재 예수금)은 같은날 매도대금을 반영 못해 데이트레이드 자본 재활용이
        막힌다(2026-06-23 실측: 180주 매도 후 sunamt1=−26.8M[미수] vs CSPAQ22200=493M).
        실패 시 None → 호출자가 sunamt1 fallback. InBlock BalCreTp="1"(현금기준)."""
        try:
            r = self._post("/stock/accno", "CSPAQ22200",
                           {"CSPAQ22200InBlock1": {"BalCreTp": "1"}})
            ob2 = r.get("CSPAQ22200OutBlock2") or {}
            return int(float(ob2.get("MnyOrdAbleAmt") or 0))
        except Exception as e:
            log.warning("CSPAQ22200 주문가능금액 조회 실패 — sunamt1 fallback: %s", e)
            return None

    def account_snapshot(self, overseas: bool = True) -> dict:
        """국내+미국 잔고 스냅샷. overseas=True(기본)면 COSOQ00201 해외 잔고도 병합.

        overseas=True: 해외 잔고(COSOQ00201)를 호출해 balance와 positions에 병합.
                       실패 시 balance["fetch_failed"]=["overseas"] 마커 설정 후 국내만 반환.
        overseas=False: 해외 잔고 조회 없이 국내만 반환.

        반환 balance:
          cash         추정D2예수금(KRW) — t0424OutBlock.sunamt1 (공식문서 대조 확인 2026-06-19)
                       ⚠ TODO(Phase C): 정확한 주문가능금액은 별도 TR CSPAQ22200 필요.
          total_eval   총자산(KRW) — t0424OutBlock.sunamt(추정순자산 = 주식평가 + 예수금). 모의 실측
                       확정(2026-06-20): tappamt(평가금액)는 보유 시가만이라 현금 제외 → 킬스위치
                       오발동(아래 주석). KIS tot_evlu_amt(총평가=유가증권+예수금)와 동일 의미.
          cash_usd     USD 예수금 (overseas=True·성공 시). 기본 0.0.
          fx_usdkrw    USD/KRW 환율 (overseas=True·성공 시). 기본 0.0.
          foreign_eval_krw  해외 평가금액 KRW 환산 (overseas=True·성공 시). 기본 0.0.

        positions: qty>0 인 국내 종목(market="DOMESTIC"·currency="KRW") +
                   overseas=True·성공 시 미국 종목(market="NAS"/"NYS"·currency="USD").

        조회 실패 시 balance["fetch_failed"]=["domestic"] 설정 — 절대 0으로 위장하지 않음.
        이 마커가 없으면 Trader 킬스위치가 "평가금액=0=−98% 손실"로 오판해 전량 청산한다
        (−98% 부류버그 근본 수정: kis_broker.py 동일 패턴).
        ⚠ t0424OutBlock1 필드명(expcode/hname/janqty/pamt/price) — A2 KB 가정.
        """
        try:
            body = self._balance_raw()
        except Exception as e:
            log.warning("LS 잔고 조회 실패 — fetch_failed 마커 설정: %s", e)
            return {
                "balance": {
                    "cash": 0, "total_eval": 0,
                    "cash_usd": 0.0, "fx_usdkrw": 0.0, "foreign_eval_krw": 0.0,
                    # ★ε: "조회 실패"와 "진짜 0"을 소비자가 구분하도록 표식.
                    # Trader 킬스위치·equity 시계열은 이 마커가 있으면 평가를 보류해야 한다.
                    "fetch_failed": ["domestic"],
                },
                "positions": [],
            }

        summary = body.get("t0424OutBlock") or {}
        # cash: sunamt1 = 추정D2예수금 (공식문서 대조 확인 2026-06-19)
        # cash(매수여력) = CSPAQ22200 현금주문가능금액(MnyOrdAbleAmt) — 정산중 매도대금 반영
        #   (2026-06-23 실측: sunamt1[현 예수금]은 같은날 매도대금 미반영→데이트레이드 자본 재활용
        #   막힘. 180주 매도 후 sunamt1=−26.8M vs CSPAQ22200=493M). 조회 실패 시 sunamt1 fallback.
        # total_eval = sunamt(추정순자산=주식평가+예수금=총자산). 모의 실측 확정(2026-06-20):
        #   tappamt(평가금액)는 보유 시가만이라 현금 제외 → 미보유 계좌서 total_eval≈0 →
        #   _unified_equity_krw(국내 equity=total_eval) 거짓 -100% 킬스위치 오발동(−98% 부류버그).
        #   KIS tot_evlu_amt(총평가=유가증권+예수금)와 동의 LS 필드가 sunamt.
        orderable = self._orderable_cash_krw()
        cash = orderable if orderable is not None else int(float(summary.get("sunamt1") or 0))
        total_eval = int(float(summary.get("sunamt") or 0))

        positions = []
        for it in body.get("t0424OutBlock1") or []:
            qty = int(float(it.get("janqty") or 0))
            if qty <= 0:
                continue
            positions.append({
                "symbol": str(it.get("expcode", "")).strip(),  # 🟢 6자리
                "name": str(it.get("hname", "")).strip(),      # 🟢
                "qty": qty,                                     # 🟢
                "avg_price": float(it.get("pamt") or 0),       # 🟢 평균단가
                "eval_price": float(it.get("price") or 0),     # 🟢 현재가
                "market": "DOMESTIC",
                "currency": "KRW",
            })

        balance = {
            "cash": cash, "total_eval": total_eval,
            "cash_usd": 0.0, "fx_usdkrw": 0.0, "foreign_eval_krw": 0.0,
        }
        if overseas and not self._overseas_unavailable:
            try:
                ov = self.overseas_snapshot()
                balance["cash_usd"] = ov["usd_cash"]
                balance["fx_usdkrw"] = ov["fx_usdkrw"]
                balance["foreign_eval_krw"] = ov["foreign_eval_krw"]
                positions.extend(ov["positions"])
            except Exception as e:
                # 영구 미제공 시그니처: IGW40014/002US — LS 모의 해외주식 미제공(2026-06-23 실측 확정).
                # 또는 rsp_cd 01900 "모의투자에서는 해당업무가 제공되지 않습니다".
                # 이 시그니처면 세션 내 이후 호출을 skip해 rate-limit 낭비·로그 스팸 방지.
                # ⚠ 일시적 실패(네트워크 등)는 절대 영구 비활성화 금지 — 이 조건만 캐시.
                err_text = str(e)
                resp_text = ""
                _resp_obj = getattr(e, "response", None)
                if _resp_obj is not None:
                    try:
                        resp_text = _resp_obj.text or ""
                    except Exception:
                        pass
                combined = err_text + resp_text
                _permanent = ("IGW40014" in combined or "002US" in combined
                              or "01900" in combined
                              or "모의투자에서는 해당업무가 제공되지 않습니다" in combined)
                if _permanent:
                    self._overseas_unavailable = True
                    log.warning("LS 해외주식 모의 미제공(영구) — 이후 overseas 호출 skip: %s", e)
                else:
                    # 일시적 실패는 fetch_failed 마커만(다음 사이클 재시도).
                    # ⚠ 0으로 degrade 금지: 미국 보유 사용자의 일시적 실패를 평가금 0으로 읽으면
                    #   −98% 거짓 청산 재발(trader.py:149 부류버그).
                    log.warning("LS 해외 잔고 조회 실패 — 국내만 반영: %s", e)
                balance["fetch_failed"] = ["overseas"]
        elif overseas and self._overseas_unavailable:
            # 영구 미제공 캐시 후 — 호출 skip, fetch_failed 유지(킬스위치 보류 일관).
            balance["fetch_failed"] = ["overseas"]
        return {"balance": balance, "positions": positions}

    # ── 시세 조회 (t1102) ─────────────────────────────────────────────────────

    def _price_raw(self, symbol: str) -> dict:
        """t1102 주식현재가 — 현재가·OHLC.

        ⚠ PATH "/stock/market-data" — A2 KB 가정. Phase C 실측 확정.
        ⚠ InBlock 키 "t1102InBlock", 필드 "shcode" — A2 KB 🟢.
        ⚠ OutBlock 키 "t1102OutBlock" — A2 KB 🟢.
        """
        return self._post(
            "/stock/market-data", "t1102",
            {"t1102InBlock": {"shcode": symbol}},  # shcode = 6자리 코드, A-접두사 불필요 🟢
        )

    def _quote_overseas_raw(self, symbol: str, market: str) -> dict:
        """g3101 해외 현재가 — keysymbol = exchcd(82/81) + bare 티커."""
        excd = self._ls_excd(market)
        return self._post("/overseas-stock/market-data", "g3101",
                          {"g3101InBlock": {"delaygb": "R", "keysymbol": f"{excd}{self._ls_ticker(symbol)}",
                                            "exchcd": excd, "symbol": self._ls_ticker(symbol)}})

    def _price_overseas(self, symbol: str, market: str) -> float:
        out = self._quote_overseas_raw(symbol, market).get("g3101OutBlock") or {}
        return float(out.get("price") or 0)

    def price(self, symbol: str) -> float:
        """현재가 반환. 국내=t1102(KRW), 해외=g3101(USD). ⚠ t1102OutBlock.price — A2 KB 🟢."""
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            out = self._price_raw(symbol).get("t1102OutBlock") or {}
            return float(out.get("price") or 0)
        return self._price_overseas(symbol, market)

    def today_open(self, symbol: str) -> float:
        """당일 시가 반환. catch-up cycle에서 시장가→시초가 limit 변환 시 사용.

        시가 없으면(개장 전·장 종료 후·오류) 0.0 반환 → caller가 catch-up skip 결정.
        국내=t1102OutBlock.open, 해외=g3101OutBlock.open.
        """
        try:
            market = self._detect_market(symbol)
            if market == "DOMESTIC":
                out = self._price_raw(symbol).get("t1102OutBlock") or {}
                v = out.get("open")
            else:
                out = self._quote_overseas_raw(symbol, market).get("g3101OutBlock") or {}
                v = out.get("open")
            return float(v) if v not in (None, "", 0, "0") else 0.0
        except Exception:
            return 0.0

    # ── 주문 (CSPAT00601) ─────────────────────────────────────────────────────

    def _submit(self, symbol: str, qty: int, side: str,
                ord_prc_ptn_code: str, unit_price: float) -> dict:
        """국내주식 신규주문 — CSPAT00601 단일 TR(매수·매도 BnsTpCode로 구분).

        🟢 PATH "/stock/order" — 신규·정정·취소 단일 경로(tr_cd로 구분), 커뮤니티 래퍼 대조 확인(GOTCHAS G18).
        ⚠ InBlock 키 "CSPAT00601InBlock1" — A2 KB 🟢.
        ⚠ BnsTpCode: "2"=매수/"1"=매도 — A2 KB G7 🟢.
        ⚠ OrdprcPtnCode: "00"=지정가/"03"=시장가 — A2 KB G8 🟢.
        ⚠ IsuNo = "A" + 6자리 — A2 KB G9 🟢 (모의). 실전 포함 Phase C 확인.
        ⚠ AcntNo/InptPwd 필요 여부 — A2 KB 미검증. 현재 AcntNo 포함, InptPwd 빈 문자열.
        ⚠ MgntrnCode="000"(일반), LoanDt="", OrdCndiTpCode="0" — A2 KB 가정.
        """
        bns_tp = "2" if side == "buy" else "1"
        # KRX 정수호가(원 단위 — 소수 틱 없음) → 지정가는 int, 시장가는 0.
        ord_prc = int(unit_price) if ord_prc_ptn_code == "00" else 0
        resp = self._post(
            "/stock/order", "CSPAT00601",
            {"CSPAT00601InBlock1": {
                "AcntNo": self.account_no,   # ⚠ 형식 G6 검증 대상
                "InptPwd": "",               # ⚠ 필요 여부 미검증
                "IsuNo": f"A{symbol}",       # 🟢 "A" + 6자리 (G9)
                "OrdQty": qty,
                "OrdPrc": ord_prc,
                "BnsTpCode": bns_tp,         # 🟢 G7
                "OrdprcPtnCode": ord_prc_ptn_code,  # 🟢 G8
                "MgntrnCode": "000",         # 일반 ⚠
                "LoanDt": "",
                "OrdCndiTpCode": "0",        # 없음 ⚠
            }},
            is_order=True,
        )
        return normalize_ls_order_resp(resp, ordno_field="OrdNo")

    def _submit_overseas(self, symbol: str, qty: int, side: str,
                         unit_price: float, market: str) -> dict:
        """COSAT00301 미국 주문. OrdPtnCode 02매수/01매도. 해외는 지정가(00) 강제 —
        시장가 의도(unit_price<=0)는 g3101 현재가로 대체(OG3 안전·KIS 패턴). 가격 float."""
        if unit_price <= 0:
            quoted = self._price_overseas(symbol, market)
            if quoted <= 0:
                raise RuntimeError(
                    f"해외 {market} {symbol} 현재가 조회 실패({quoted}) — 지정가 발주 불가. 주문 보류.")
            unit_price = quoted
        ord_ptn = "02" if side == "buy" else "01"
        resp = self._post("/overseas-stock/order", "COSAT00301",
                          {"COSAT00301InBlock1": {
                              "RecCnt": 1,
                              "OrdPtnCode": ord_ptn,
                              "OrdMktCode": self._ls_excd(market), "IsuNo": self._ls_ticker(symbol),
                              "OrdQty": qty, "OvrsOrdPrc": float(unit_price),
                              "OrdprcPtnCode": "00", "BrkTpCode": ""}}, is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="OrdNo")

    def buy(self, symbol: str, qty: int) -> dict:
        """시장가 매수. 국내=CSPAT00601·시장가(03), 해외=COSAT00301·지정가(현재가 조회)."""
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "buy", "03", 0.0) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "buy", 0.0, m)

    def sell(self, symbol: str, qty: int) -> dict:
        """시장가 매도. 국내=CSPAT00601·시장가(03), 해외=COSAT00301·지정가(현재가 조회)."""
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "sell", "03", 0.0) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "sell", 0.0, m)

    def buy_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """지정가 매수. 국내=CSPAT00601·지정가(00), 해외=COSAT00301·지정가(00)."""
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "buy", "00", float(limit_price)) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "buy", float(limit_price), m)

    def sell_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """지정가 매도. 국내=CSPAT00601·지정가(00), 해외=COSAT00301·지정가(00)."""
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "sell", "00", float(limit_price)) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "sell", float(limit_price), m)

    def _submit_overseas_resv(self, symbol: str, qty: int, side: str,
                               unit_price: float, market: str) -> dict:
        """COSAT00400 미국 예약주문(등록). 지정가(00). AcntNo/Pwd body 필수(G23-5).
        실행일창 = 오늘~오늘(당일 개장 단일가). ⚠ enum/필드 research 기반 — 모의 실측(OG4).
        상태 추적(RsvOrdNo→COSAQ01400)은 모의 E2E(G-E4)에서 확정 — 현재 등록 scope만."""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        resp = self._post("/overseas-stock/order", "COSAT00400",
                          {"COSAT00400InBlock1": {
                              "TrxTpCode": "1",
                              "CntryCode": "US",
                              "BnsTpCode": "2" if side == "buy" else "1",
                              "AcntNo": self.account_no, "Pwd": "",
                              "FcurrMktCode": self._ls_excd(market), "IsuNo": self._ls_ticker(symbol),
                              "OrdQty": qty, "OvrsOrdPrc": float(unit_price), "OrdprcPtnCode": "00",
                              "RsvOrdSrtDt": today, "RsvOrdEndDt": today}}, is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="RsvOrdNo")

    def buy_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """해외 예약주문 매수(COSAT00400). 국내는 미지원 — 명시적 NotImplementedError."""
        m = self._detect_market(symbol)
        if m == "DOMESTIC":
            raise NotImplementedError("LS 국내주식 예약주문 미지원")
        return self._submit_overseas_resv(symbol, qty, "buy", float(limit_price), m)

    def sell_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """해외 예약주문 매도(COSAT00400). 국내는 미지원 — 명시적 NotImplementedError."""
        m = self._detect_market(symbol)
        if m == "DOMESTIC":
            raise NotImplementedError("LS 국내주식 예약주문 미지원")
        return self._submit_overseas_resv(symbol, qty, "sell", float(limit_price), m)

    # ── 취소 (CSPAT00801 / COSAT00301) ──────────────────────────────────────

    def _cancel_overseas(self, order_no, symbol, qty):
        """COSAT00301 OrdPtnCode='08' 취소(OrgOrdNo+IsuNo+OrdQty)."""
        market = market_index.exchange_of(symbol) or "NAS"
        resp = self._post("/overseas-stock/order", "COSAT00301",
                          {"COSAT00301InBlock1": {
                              "RecCnt": 1,
                              "OrdPtnCode": "08",
                              "OrgOrdNo": int(order_no) if str(order_no).isdigit() else order_no,
                              "OrdMktCode": self._ls_excd(market), "IsuNo": self._ls_ticker(symbol),
                              "OrdQty": qty, "OvrsOrdPrc": 0, "OrdprcPtnCode": "00",
                              "BrkTpCode": ""}}, is_order=True)
        r = normalize_ls_order_resp(resp, ordno_field="OrdNo")
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}

    def cancel(self, order_no: str, symbol: str, qty: int) -> dict:
        """미체결 주문 취소. 해외=COSAT00301(08), 국내=CSPAT00801.

        PATH "/stock/order" — 신규/정정/취소 모두 동일 경로, tr_cd 헤더로 TR 구분.
        커뮤니티 래퍼 대조 확인: "/stock/order-cancel" 경로는 404 반환.
        ⚠ InBlock 키 "CSPAT00801InBlock1" — A2 KB 🟢.
        ⚠ OrgOrdNo long, IsuNo="A"+6자리, OrdQty — A2 KB 🟢.
        ⚠ AcntNo/InptPwd 필요 여부 — 미검증. 현재 AcntNo 포함.
        """
        if symbol and market_index.is_us(symbol):
            return self._cancel_overseas(order_no, symbol, qty)
        resp = self._post(
            "/stock/order", "CSPAT00801",
            {"CSPAT00801InBlock1": {
                "AcntNo": self.account_no,   # ⚠ 형식 G6
                "InptPwd": "",               # ⚠ 필요 여부 미검증
                "OrgOrdNo": int(order_no) if str(order_no).isdigit() else order_no,  # 🟢 long
                "IsuNo": f"A{symbol}",       # 🟢 G9
                "OrdQty": qty,               # 🟢 미체결잔량 기준
            }},
            is_order=True,
        )
        r = normalize_ls_order_resp(resp, ordno_field="OrdNo")
        # cancel 반환 계약: {success, message, msg_cd} — order_no 포함해도 무방
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}

    # ── 미체결 조회 (t0425) ───────────────────────────────────────────────────

    def _pending_raw(self, chegb: str = "2") -> dict:
        """t0425 주식체결/미체결조회 — 전 종목.

        chegb="2" → 미체결만(pending_orders 용), chegb="0" → 전체(체결 포함·order_status 용).
        PATH "/stock/accno"(t0424와 동일 경로, tr_cd로 구분). OutBlock1에 cheqty(체결수량)·
        status(주문상태)·cheprice(체결가) 포함 — 2026-06-23 가이드 t0425 실측 확인.
        """
        return self._post(
            "/stock/accno", "t0425",
            {"t0425InBlock": {
                "expcode": "",      # 전 종목 🟢
                "chegb": chegb,     # "2" 미체결 / "0" 전체(체결 인지)
                "medosu": "0",      # 전체(매수+매도) 🟢
                "sortgb": "1",      # 최신 역순 🟢
                "cts_ordno": "",
            }},
        )

    def _overseas_ccld_raw(self, exec_yn: str) -> dict:
        """COSAQ00102 계좌주문체결내역 — ExecYn 0전체/1체결/2미체결. OrdDt=당일."""
        # COSAQ00102InBlock1 — LsApiHelper 스펙: 12필드 전부 필수(research 4필드는 불완전). RecCnt·SrtOrdNo=int.
        # ⚠ OrdMktCode/CrcyCode 전체조회 값("00"/"000")은 모의 실측 확정 필요(비치명: 실패해도 order_status=unknown).
        return self._post("/overseas-stock/accno", "COSAQ00102",
                          {"COSAQ00102InBlock1": {
                              "RecCnt": 1, "QryTpCode": "1", "BkseqTpCode": "1", "OrdMktCode": "00",
                              "BnsTpCode": "0", "IsuNo": "", "SrtOrdNo": 999999999,
                              "OrdDt": datetime.now().strftime("%Y%m%d"), "ExecYn": exec_yn,
                              "CrcyCode": "000", "ThdayBnsAppYn": "0", "LoanBalHldYn": "0"}})

    def _overseas_order_status(self, order_no: str, symbol: str) -> dict:
        """COSAQ00102(ExecYn='0') OrdNo 매칭 → filled/partial/cancelled/submitted.
        ⚠ G-E2: OrdTrxPtnNm 부분체결/거부 문자열 실측 전 — '취소' 포함 시 cancelled, 그 외 Exec/Unerc로 판정."""
        try:
            rows = self._overseas_ccld_raw("0").get("COSAQ00102OutBlock3") or []
        except Exception as e:
            log.warning("LS 해외 order_status 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("OrdNo")) != canonical_odno(order_no):
                continue
            exec_q = int(float(row.get("ExecQty") or 0))
            unerc = int(float(row.get("UnercQty") or 0))
            nm = str(row.get("OrdTrxPtnNm") or "")
            if "취소" in nm:
                st = "cancelled"
            elif unerc == 0 and exec_q > 0:
                st = "filled"
            elif exec_q > 0:
                st = "partial"
            else:
                st = "submitted"
            return {"order_no": order_no, "status": st, "filled_qty": exec_q, "remain_qty": unerc,
                    "fill_price": float(row.get("OvrsExecPrc") or 0)}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def _overseas_pending(self) -> list[dict]:
        """COSAQ00102(ExecYn='2') 미체결 해외 주문 목록.
        ⚠ G-E3: BnsTpCode(2매수/1매도) 필드 미확정 — 모의 E2E 실측 후 교정."""
        try:
            rows = self._overseas_ccld_raw("2").get("COSAQ00102OutBlock3") or []
        except Exception as e:
            log.warning("LS 해외 pending 실패: %s", e)
            return []
        out = []
        for row in rows:
            if str(row.get("OrgOrdNo") or "0") not in ("0", "", "000000000"):  # 정정/취소행 제외
                continue
            unerc = int(float(row.get("UnercQty") or 0))
            if unerc <= 0:
                continue
            # ⚠ G-E3: COSAQ00102 매수/매도 필드 미확정 — LS 관례 BnsTpCode(2매수/1매도) 가정, 없으면 buy.
            #         모의 E2E에서 정확 필드 실측 후 교정(라우팅엔 order_no 사용 — side는 표시/대사용).
            side = "buy" if str(row.get("BnsTpCode") or "2") == "2" else "sell"
            out.append({
                "order_no": str(row.get("OrdNo") or ""),
                "symbol": str(row.get("ShtnIsuNo") or "").strip().upper(),
                "name": "",
                "side": side,
                "qty": int(float(row.get("OrdQty") or 0)),
                "filled_qty": int(float(row.get("ExecQty") or 0)),
                "remain_qty": unerc,
                "limit_price": float(row.get("OvrsOrdPrc") or 0),
                "ord_branch": "",
                "submitted_at": str(row.get("OrdTime") or ""),
                "market": "US",
                "currency": "USD",
            })
        return out

    def order_status(self, order_no: str, symbol: str | None = None,
                     hint: dict | None = None) -> dict:
        """특정 주문번호의 현재 상태.

        해외(symbol이 미국 종목): COSAQ00102(ExecYn='0') OrdNo 매칭.
        국내: t0425 미체결 조회.

        hint: Broker 인터페이스 계약 파라미터. 국내주식은 무시(해외 예약주문 전용).

        반환 어휘: filled | partial | submitted | cancelled | unknown
        t0425를 **chegb="0"(전체)**로 조회 — 체결·취소 주문도 목록에 남아 cheqty(체결수량)·
        status(주문상태)로 filled/partial/cancelled를 인지한다(G10 해소·2026-06-23 가이드 실측).
        ⚠ 전일 이전 주문은 당일 t0425에서 사라질 수 있어(당일 조회) status="unknown" — 15:50
          정산 reconcile_with_kis(실보유 diff)가 백스톱.
        """
        if symbol and market_index.is_us(symbol):
            return self._overseas_order_status(order_no, symbol)

        try:
            body = self._pending_raw(chegb="0")   # 전체조회 — 체결·취소 인지
        except Exception as e:
            log.warning("LS order_status 조회 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown",
                    "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

        for row in body.get("t0425OutBlock1") or []:
            if canonical_odno(row.get("ordno")) == canonical_odno(order_no):
                orig_qty = int(float(row.get("qty") or 0))
                filled_qty = int(float(row.get("cheqty") or 0))    # 체결수량(chegb=0 전체조회라 신뢰)
                remain_qty = int(float(row.get("ordrem") or 0))
                st = str(row.get("status") or "")
                # cheqty(체결수량)·status로 판정 — 취소는 status에 "취소" 포함(전량체결과 구분).
                if "취소" in st:
                    status = "cancelled"
                elif filled_qty >= orig_qty and orig_qty > 0:
                    status = "filled"
                elif filled_qty > 0:
                    status = "partial"
                else:
                    status = "submitted"
                return {
                    "order_no": order_no,
                    "status": status,
                    "filled_qty": filled_qty,
                    "remain_qty": remain_qty,
                    # cheprice = 체결가격, price = 주문가격 (가이드 t0425 🟢)
                    "fill_price": float(row.get("cheprice") or row.get("price") or 0),
                }

        # 목록에 없으면 unknown (전일 이전 주문은 당일 t0425에서 사라짐)
        return {"order_no": order_no, "status": "unknown",
                "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def pending_orders(self) -> list[dict]:
        """미체결 주문 목록 — 국내 t0425 + 해외 COSAQ00102(ExecYn='2') 병합.

        실패는 비치명적 — 국내·해외 각각 로그 후 [] 반환.
        ⚠ t0425OutBlock1 medosu 필드: "매수"/"매도" 문자열 반환 — A2 KB 🟢.
        ⚠ hname(종목명) 필드: t0425OutBlock1에 포함 여부 미확인 — 없으면 "".
        ⚠ submitted_at: ordtime 형식(HHMMSSMMM) — A2 KB 🟢.
        """
        out = []
        try:
            body = self._pending_raw()
            for row in body.get("t0425OutBlock1") or []:
                remain = int(float(row.get("ordrem") or 0))
                if remain <= 0:
                    continue
                orig_qty = int(float(row.get("qty") or 0))
                out.append({
                    "order_no": str(row.get("ordno") or ""),
                    "symbol": str(row.get("expcode") or "").strip(),   # 🟢 6자리
                    "name": str(row.get("hname") or ""),               # ⚠ 필드 존재 여부 미확인
                    "side": "buy" if str(row.get("medosu") or "") == "매수" else "sell",  # 🟢
                    "qty": orig_qty,
                    "filled_qty": max(0, orig_qty - remain),
                    "remain_qty": remain,
                    "limit_price": float(row.get("price") or 0),       # 🟢 주문가격
                    "ord_branch": "",   # LS t0425에 해당 필드 없음(KIS ord_gno_brno 대응 없음)
                    "submitted_at": str(row.get("ordtime") or ""),      # 🟢 HHMMSSMMM
                    "market": "DOMESTIC",
                    "currency": "KRW",
                })
        except Exception as e:
            log.warning("LS 국내 pending 실패: %s", e)
        try:
            out.extend(self._overseas_pending())
        except Exception as e:
            log.warning("LS 해외 pending 실패: %s", e)
        return out

    # ── 해외(미국) 시장 라우팅 ───────────────────────────────────────────────
    # 거래소코드 — research G23-1: 82=NASDAQ, 81=NYSE+AMEX(AMEX 통합). KIS NAS/NYS/AMS 3분할과 다름.
    _LS_EXCD = {"NAS": "82", "NYS": "81", "AMS": "81"}

    def _detect_market(self, symbol: str) -> str:
        """종목→시장. 'DOMESTIC' 또는 미국 거래소 'NAS'/'NYS'/'AMS'.
        market_index(브로커 무관 권위 소스) 재사용 — KIS _detect_market와 동일.
        미국 티커 형태인데 인덱스에 없으면 추측 않고 RoutingError(발주 차단)."""
        exch = market_index.exchange_of(symbol)
        if exch:
            return exch
        if market_index._looks_domestic(symbol):
            return "DOMESTIC"
        s = symbol.strip().upper()
        if s.isalpha() and 1 <= len(s) <= 5:
            raise market_index.RoutingError(
                f"미국 티커로 보이나 마스터 인덱스에 없음: {symbol} — 인덱스 갱신 필요. 발주 보류.")
        return "DOMESTIC"

    def _ls_excd(self, market: str) -> str:
        """미국 거래소(NAS/NYS/AMS) → LS 시장코드(82/81)."""
        return self._LS_EXCD.get(market, "82")

    def _ls_ticker(self, symbol: str) -> str:
        """LS 해외 IsuNo/keysymbol용 bare 티커(대문자). 클래스주(BRK-B)는 OG-E1(모의 실측)."""
        return symbol.strip().upper()

    # ── 해외(미국) 잔고 조회 (COSOQ00201) ────────────────────────────────────

    def _overseas_balance_raw(self) -> dict:
        """COSOQ00201 해외 종합잔고평가 — 통화별(OB3)·종목별(OB4). BaseDt=당일."""
        from datetime import datetime
        # COSOQ00201InBlock1 — LsApiHelper 스펙(LS 공식문서 스크래핑): RecCnt=int·BaseDt(str8)·
        #   CrcyCode(str3, "ALL"=전통화)·AstkBalTpCode(str2). RecCnt는 int(문자열 아님).
        return self._post("/overseas-stock/accno", "COSOQ00201",
                          {"COSOQ00201InBlock1": {"RecCnt": 1, "BaseDt": datetime.now().strftime("%Y%m%d"),
                                                  "CrcyCode": "ALL", "AstkBalTpCode": "00"}})

    def overseas_snapshot(self) -> dict:
        """미국 USD 예수금+환율+보유종목. foreign_eval_krw는 직접계산(벤더 환산필드 불일치 회피·KIS 동일).
        ⚠ 필드명(FcurrDps/BaseXchrat/ShtnIsuNo/AstkBalQty/FcstckUprc/OvrsScrtsCurpri) research 기반 — 모의 실측 확정."""
        body = self._overseas_balance_raw()
        usd_cash = fx = 0.0
        for row in body.get("COSOQ00201OutBlock3") or []:
            if str(row.get("CrcyCode") or "") == "USD":
                usd_cash = float(row.get("FcurrDps") or 0)
                fx = float(row.get("BaseXchrat") or 0)
                break
        positions = []
        for it in body.get("COSOQ00201OutBlock4") or []:
            qty = int(float(it.get("AstkBalQty") or 0))
            if qty <= 0:
                continue
            sym = str(it.get("ShtnIsuNo") or "").strip().upper()
            positions.append({
                "symbol": sym, "name": str(it.get("IsuKorNm") or it.get("IsuNm") or ""),
                "qty": qty,
                "avg_price": float(it.get("FcstckUprc") or 0),
                "eval_price": float(it.get("OvrsScrtsCurpri") or 0),
                "market": market_index.exchange_of(sym) or "US", "currency": "USD",
            })
        positions_eval_usd = sum(p["qty"] * p["eval_price"] for p in positions)
        foreign_eval_krw = (usd_cash + positions_eval_usd) * fx if fx > 0 else 0.0
        return {"usd_cash": usd_cash, "fx_usdkrw": fx,
                "foreign_eval_krw": foreign_eval_krw, "positions": positions}

    def _overseas_deposit_raw(self) -> dict:
        """COSOQ02701 해외주식 예수금 — 통화별 OutBlock3(FcurrOrdAbleAmt 외화주문가능·BaseXchrat 환율).
        COSOQ00201(종합잔고)과 달리 예수금 전용 TR — 해외 매수여력 사이징에 사용.
        InBlock(RecCnt:int·CrcyCode:"ALL")는 ls_openapi_guide.md COSOQ02701 요청 예시와 일치."""
        return self._post("/overseas-stock/accno", "COSOQ02701",
                          {"COSOQ02701InBlock1": {"RecCnt": 1, "CrcyCode": "ALL"}})

    def buying_power_usd(self, symbol: str, ref_price: float) -> dict:
        """미국 종목 USD 주문가능액·수량·환율 — COSOQ02701 해외 예수금 기준. KIS buying_power_usd 미러.

        반환 {usd_orderable, max_qty, fx_usdkrw} — KIS와 동일 키(trader 사이징 P6이 소비).
        LS 모의 해외는 현금계좌(통합증거금 없음) → 계좌 USD 주문가능액(FcurrOrdAbleAmt)으로
        사이징, max_qty=floor(orderable/ref_price). 실패 시 trader가 try/except로 'error' 기록·보류.
        """
        body = self._overseas_deposit_raw()
        usd_orderable = fx = 0.0
        for row in body.get("COSOQ02701OutBlock3") or []:
            if str(row.get("CrcyCode") or "") == "USD":
                usd_orderable = float(row.get("FcurrOrdAbleAmt") or 0)
                fx = float(row.get("BaseXchrat") or 0)
                break
        max_qty = int(usd_orderable / ref_price) if ref_price > 0 else 0
        return {"usd_orderable": usd_orderable, "max_qty": max_qty, "fx_usdkrw": fx}


# ─────────────────────────────────────────────────────────────────────────────
# ⚠ 이하 B6 구현 전체 초안(draft) — 필드명/경로/코드값은 docs/ls-api A2 KB 기반.
#   Phase C(키 발급) 후 docs/ls-api 실측 확정 필요.
# ─────────────────────────────────────────────────────────────────────────────


def canonical_odno(s) -> str:
    """LS 주문번호 비교용 정규화 — 선행 0 제거 (KIS canonical_odno와 동일 패턴).

    t0425 ordno 필드가 leading-zero int/str으로 오는 경우(실측: KIS "0001569157")에
    대비해 strip+lstrip("0")로 통일. 비교 시점에만 호출 — raw 보존.
    """
    return str(s).strip().lstrip("0") if s is not None else ""


def normalize_ls_order_resp(raw: dict, *, ordno_field: str) -> dict:
    """LS 주문/취소 응답 → Broker 정규형 {success, order_no, message, msg_cd}.

    성공 판정 = OrdNo 존재 여부.
    근거: LS 주문 TR(CSPAT006xx)은 매수 성공 "00040"/매도 "00039" 등 비표준 코드를 쓰고,
    조회 TR만 "00000"을 사용한다(programgarden-finance 문서·커뮤니티 래퍼 대조 확인).
    따라서 rsp_cd로 성공을 판정하면 *모든* 정상 주문을 실패로 읽는다.
    OrdNo가 실제 주문번호(non-empty/non-zero)이면 접수 성공이 보장된다.
    정확한 성공 rsp_cd 값은 Phase C 키 발급 후 docs/ls-api에 확정.

    OutBlock 탐색: OutBlock2 우선 → 다른 OutBlock fallback (키 순서 보장 불가이므로
    전체 순회 후 OutBlock2 히트가 있으면 그 값을 사용, 없으면 첫 번째 OutBlock 값 사용).

    Trader가 의존하는 키:
      success  bool  — bool(order_no) (실제 OrdNo = 접수 성공)
      order_no str   — OutBlock 내 ordno_field 값 (없으면 "")
      message  str   — rsp_msg
      msg_cd   str   — rsp_cd

    raw LS 키(rsp_cd, OutBlock*)는 반환 dict에 포함하지 않는다(정규형 계약).
    """
    message = raw.get("rsp_msg", "")
    msg_cd = raw.get("rsp_cd", "")

    # OutBlock 탐색: OutBlock2 우선, fallback은 첫 번째 OutBlock 값
    order_no = ""
    ob2_hit = ""
    ob_any_hit = ""
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        if "OutBlock" not in key:
            continue
        v = val.get(ordno_field)
        # None/""/0/"0" 은 미접수로 간주
        if v is None or v == "" or v == 0 or v == "0":
            continue
        candidate = str(v)
        if "OutBlock2" in key:
            ob2_hit = candidate
        elif not ob_any_hit:
            ob_any_hit = candidate

    order_no = ob2_hit or ob_any_hit
    success = bool(order_no)

    return {
        "success": success,
        "order_no": order_no,
        "message": message,
        "msg_cd": msg_cd,
    }
