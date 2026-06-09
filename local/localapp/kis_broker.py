"""KIS(한국투자증권) REST 브로커 — 모의투자(VTS) 연동.

자격증명은 keyring에서만 읽는다. Access Token은 APP_DIR에 캐싱(24h).
실전(virtual=False) TR_ID도 분기하지만 첫 릴리스는 모의투자만 사용한다.

Phase 9 확장:
- 지정가 주문 (ORD_DVSN="00") + 시장가 (ORD_DVSN="01")
- 주문 취소·정정 (order-rvsecncl)
- 일별 주문체결 조회 (inquire-daily-ccld) — 미체결/체결 상태 추적
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
from .secrets_store import load_kis
from .state_store import save_json

log = logging.getLogger("localapp.kis_broker")

_VTS = "https://openapivts.koreainvestment.com:29443"
_REAL = "https://openapi.koreainvestment.com:9443"
_TOKEN_CACHE = APP_DIR / ".kis_token.json"


def canonical_odno(s) -> str:
    """KIS 주문번호(ODNO) 비교용 정규화 — 단일 출처(M2).

    발주응답 ODNO·일별체결 odno는 zero-padded-10("0001569157")으로 오지만 WS
    실시간 ODER_NO의 패딩은 KIS spec에 보장돼 있지 않다. 선행 0를 제거한 형태로
    통일해 체결 인지 3경로(WS·국내 REST·해외 REST)가 같은 기준으로 매칭하게 한다.
    pending 키·취소·조회는 KIS가 준 raw 형태를 그대로 쓰고, 이 함수는 *비교 시점*
    에서만 호출한다(라운드트립 raw 보존).
    """
    return str(s).strip().lstrip("0") if s is not None else ""


class _Throttle:
    """Sliding window throttle — Phase 48.

    KIS API 공식 한도: 개인 1초당 10건. 안전 마진 8건/초로 운영.
    EGW00201 reactive retry(_get_retry/_post_retry)와 함께 다층 방어.
    호출 burst 시 1초 윈도우가 차면 자동 sleep 후 진행.
    프로세스 전역 단일 인스턴스(_GLOBAL_THROTTLE). 시세·주문·잔고가 모두 공유.
    """

    def __init__(self, max_calls: int = 8, window_sec: float = 1.0):
        self.max_calls = max_calls
        self.window_sec = window_sec
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls
                            if now - t < self.window_sec]
            if len(self._calls) >= self.max_calls:
                wait = self.window_sec - (now - self._calls[0]) + 0.01
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
                    self._calls = [t for t in self._calls
                                    if now - t < self.window_sec]
            self._calls.append(now)


# 프로세스 전역 단일 throttle — 모든 KisBroker 인스턴스가 공유 (TWS의 50/s와
# 다른 점: KIS는 계정/앱별 한도이므로 인스턴스가 분리돼도 같은 KIS 계정에
# 부담을 주면 차단되므로 전역 공유가 안전).
_GLOBAL_THROTTLE = _Throttle(max_calls=8, window_sec=1.0)


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
        # 시세 조회용 자격증명 — KIS는 시세를 실전 도메인 전용으로 제공하고 모의 앱키를 거부
        # (EGW02004)한다. 모의(virtual)면 별도 실전 앱키 필수, 실전이면 주문 앱키가 곧 실전이라
        # 그대로 재사용. 시세 호출만 이 키로(주문·잔고는 self.key 그대로).
        if self.virtual:
            self.quote_key = creds.get("quote_app_key") or ""
            self.quote_secret = creds.get("quote_app_secret") or ""
            if not (self.quote_key and self.quote_secret):
                raise RuntimeError(
                    "모의투자 시세 조회용 실전 앱키가 없습니다. KIS 실전 앱키를 발급해 "
                    "setup에서 '시세용 실전 앱키'로 등록하세요 (모의 앱키는 시세 도메인에서 거부됨).")
        else:
            self.quote_key, self.quote_secret = self.key, self.secret
        # 토큰 캐시 귀속 지문 — (도메인, appkey)별. KIS access token은 발급 도메인
        # (VTS=모의 / REAL=실전)에 묶이므로 단일 캐시를 만료시각만으로 재사용하면 모의↔실전
        # 전환·이중키에서 잘못된 토큰을 재사용한다. _token_for가 fp로 분리 캐시·검증.
        self._token_fp = hashlib.sha256(
            f"{self.base}:{self.key}".encode()).hexdigest()[:16]
        self._quote_fp = hashlib.sha256(
            f"{self.quote_base}:{self.quote_key}".encode()).hexdigest()[:16]

    # ── 토큰 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_token_cache() -> dict:
        """토큰 캐시(다중 엔트리: {fp: {access_token, expires_at}})를 읽는다.

        구버전 단일 엔트리({access_token, expires_at, fp})는 fp 키 구조와 충돌하므로
        무시(빈 dict 반환) → 재발급으로 자동 마이그레이션."""
        if not _TOKEN_CACHE.exists():
            return {}
        try:
            c = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
            if not isinstance(c, dict) or "access_token" in c:   # 구버전 단일 엔트리
                return {}
            return c
        except Exception:
            return {}

    def _token_for(self, appkey: str, appsecret: str, base: str, fp: str) -> str:
        """(appkey, 도메인) 조합별 access token — fp로 분리 캐시. 주문·시세 공용 발급기.

        KIS access token은 발급 도메인(VTS/REAL)에 묶이므로 주문(모의)·시세(실전)가 서로
        다른 토큰을 갖는다. 만료 30분 마진 이내 캐시 적중, 아니면 재발급."""
        cache = self._read_token_cache()
        ent = cache.get(fp)
        if ent and datetime.fromisoformat(ent["expires_at"]) > datetime.now() + timedelta(minutes=30):
            return ent["access_token"]
        r = requests.post(f"{base}/oauth2/tokenP",
                           json={"grant_type": "client_credentials",
                                 "appkey": appkey, "appsecret": appsecret},
                           timeout=10)
        r.raise_for_status()
        d = r.json()
        cache[fp] = {
            "access_token": d["access_token"],
            "expires_at": (datetime.now()
                           + timedelta(seconds=int(d.get("expires_in", 86400)))).isoformat(),
        }
        # owner-only ACL + 원자적 저장(R5). 쓰는 중 종료에도 파일 무결성 유지.
        save_json(_TOKEN_CACHE, cache)
        return d["access_token"]

    def _token(self) -> str:
        """주문·잔고용 토큰 — self.base(모의=VTS/실전=REAL) + 주문 앱키."""
        return self._token_for(self.key, self.secret, self.base, self._token_fp)

    def _quote_token(self) -> str:
        """시세용 토큰 — 실전 도메인 + 시세 앱키(모의면 별도 실전 앱키, 실전이면 주문 앱키)."""
        return self._token_for(self.quote_key, self.quote_secret, self.quote_base, self._quote_fp)

    def _headers(self, tr_id: str) -> dict:
        """주문·잔고 헤더 — 주문 앱키/토큰(모의 도메인)."""
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
            "appkey": self.key, "appsecret": self.secret,
            "tr_id": tr_id, "custtype": "P",
        }

    def _quote_headers(self, tr_id: str) -> dict:
        """시세 헤더 — 시세(실전) 앱키/토큰. KIS 시세 도메인은 실전 앱키만 허용(EGW02004)."""
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._quote_token()}",
            "appkey": self.quote_key, "appsecret": self.quote_secret,
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
        return self._get_retry(
            "/uapi/domestic-stock/v1/trading/inquire-balance", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            })

    def account_snapshot(self, overseas: bool = True) -> dict:
        """국내 + 해외(미국) 통합 잔고 스냅샷.

        반환 balance:
          cash         국내 원화 예수금 (KRW)
          total_eval   국내 평가금액 (KRW) — 통합 equity는 P6에서 결정
          cash_usd     해외 미국달러 예수금 (USD)
          fx_usdkrw    USD/KRW 환율 (KIS 최초고시환율)
          foreign_eval_krw  외화 평가총액 (KRW 환산)
        positions: 국내 + 미국 (각 항목에 market/currency 태그).

        overseas: 해외(미국) 잔고·환율·보유 포함 여부. 기본 True(통합). KRX 전용
          사이징처럼 국내 현금만 필요한 빈번 호출은 overseas=False로 불필요한
          해외 API 2건(present-balance+balance)을 건너뛴다(rate-limit·지연 절감).

        해외 조회 실패는 비치명적 — 국내 스냅샷은 유지(견고성). USD 키는 0/None.
        """
        body = self._balance_raw()
        out = body.get("output2", [{}])[0]
        positions = [{
            "symbol": it["pdno"], "name": it["prdt_name"],
            "qty": int(it["hldg_qty"]),
            "avg_price": float(it["pchs_avg_pric"]),
            "eval_price": float(it["prpr"]),
            "market": "DOMESTIC", "currency": "KRW",
        } for it in body.get("output1", []) if int(it.get("hldg_qty", 0)) > 0]

        balance = {"cash": int(out.get("dnca_tot_amt", 0)),
                   "total_eval": int(out.get("tot_evlu_amt", 0)),
                   "cash_usd": 0.0, "fx_usdkrw": 0.0, "foreign_eval_krw": 0.0}
        if overseas:
            try:
                ov = self.overseas_snapshot()
                balance["cash_usd"] = ov["usd_cash"]
                balance["fx_usdkrw"] = ov["fx_usdkrw"]
                balance["foreign_eval_krw"] = ov["foreign_eval_krw"]
                positions.extend(ov["positions"])
            except Exception as e:
                log.warning("해외 잔고 조회 실패 — 국내만 반영: %s", e)

        return {"balance": balance, "positions": positions}

    # ── 해외(미국) 잔고·환율·매수가능금액 ─────────────────────────────────────

    def _get_retry(self, path: str, tr: str, params: dict,
                   base: str | None = None, tries: int = 4) -> dict:
        """KIS GET + 초당거래제한(EGW00201) 재시도. 국내·해외·시세 조회 버스트 보호.

        rate limit은 HTTP 500 + msg_cd EGW00201로 오며 일시적 — 짧게 backoff 후
        재시도. 그 외 오류는 _kis_check가 즉시 raise. base 미지정 시 주문/잔고
        도메인(self.base), 시세 조회는 self.quote_base를 넘긴다.

        Phase 48: proactive sliding-window throttle(_GLOBAL_THROTTLE) — 호출 전
        8건/초 한도 자체 페이싱. EGW00201 reactive retry는 안전망으로 유지.
        """
        base = base or self.base
        # 시세(실전 도메인) 호출이고 모의 계정이면 시세용 실전 앱키 헤더, 아니면 주문 헤더.
        # 실전 계정(virtual=False)은 주문 앱키가 이미 실전이라 _headers로 충분.
        hdr = (self._quote_headers(tr) if (base == self.quote_base and self.virtual)
               else self._headers(tr))
        last = None
        for i in range(tries):
            _GLOBAL_THROTTLE.acquire()
            r = requests.get(f"{base}{path}",
                             headers=hdr, timeout=15, params=params)
            if r.status_code == 200:
                return _kis_check(r)
            mc = ""
            try:
                mc = r.json().get("msg_cd", "")
            except Exception:
                pass
            if mc == "EGW00201" or r.status_code in (429, 500):
                last = r
                time.sleep(0.3 * (i + 1))
                continue
            return _kis_check(r)        # 비-rate-limit 오류 → 즉시 raise
        return _kis_check(last)         # 재시도 소진 → raise

    def _post_retry(self, path: str, tr: str, body: dict,
                    timeout: int = 15, tries: int = 4) -> dict:
        """KIS POST(주문/취소) + 초당거래제한(EGW00201) 재시도.

        주문 POST는 멱등하지 않으므로 **EGW00201(처리 전 rate-limit 거부)에만**
        재시도한다 — 이 코드는 주문이 생성되기 전에 거부된 것이라 중복 발주
        위험이 없다. 그 외 오류(HTTP 500 등 모호한 응답)는 즉시 raise해 호출자가
        판단하게 한다(섣부른 재시도로 이중 발주 방지).

        Phase 48: proactive throttle(_GLOBAL_THROTTLE) — 호출 전 8건/초 페이싱.
        """
        for i in range(tries):
            _GLOBAL_THROTTLE.acquire()
            r = requests.post(f"{self.base}{path}",
                              headers=self._headers(tr), timeout=timeout, json=body)
            if r.status_code == 200:
                return _kis_check(r)
            mc = ""
            try:
                mc = r.json().get("msg_cd", "")
            except Exception:
                pass
            if mc == "EGW00201" and i < tries - 1:
                time.sleep(0.3 * (i + 1))
                continue
            return _kis_check(r)        # rate-limit 외 또는 소진 → raise
        return _kis_check(r)

    def _overseas_present_raw(self) -> dict:
        """해외 통합 현재잔고 — 통화별 외화예수금 + 환율 + 종합. (CTRP6504R/VTRP6504R)"""
        tr = "VTRP6504R" if self.virtual else "CTRP6504R"
        return self._get_retry(
            "/uapi/overseas-stock/v1/trading/inquire-present-balance", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"})

    def _overseas_balance_raw(self) -> dict:
        """해외 보유종목 — 모의(VTS)는 OVRS_EXCG_CD=NASD가 미국 전체. (TTTS3012R/VTTS3012R)"""
        tr = "VTTS3012R" if self.virtual else "TTTS3012R"
        return self._get_retry(
            "/uapi/overseas-stock/v1/trading/inquire-balance", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})

    def overseas_snapshot(self) -> dict:
        """미국 USD 예수금 + 환율 + 보유종목 (KIS 검증된 필드).

        present-balance: USD 현금(frcr_dncl_amt_2)·환율(frst_bltn_exrt).
        inquire-balance: 보유종목(ovrs_pdno/ovrs_cblc_qty/pchs_avg_pric/now_pric2).

        foreign_eval_krw — v0.9.12 변경: KIS `output3.frcr_evlu_tota` 필드를
        쓰던 옛 코드가 실측 ~5.4배 더 큰 값 반환 (사용자 보유 $39K × fx ≈ ₩59M
        인데 KIS 응답 ₩320M). KIS docs description 부재 + mismatch — 의미 모호.
        본질 fix: 환산 식을 *직접 계산*으로 — `(usd_cash + Σ qty·eval_price) × fx`.
        모든 보유가 USD라는 가정 (현 시점 우리 자동매매 범위).
        """
        from . import market_index
        pb = self._overseas_present_raw()
        usd_cash = fx = 0.0
        for row in pb.get("output2", []) or []:
            if row.get("crcy_cd") == "USD":
                usd_cash = float(row.get("frcr_dncl_amt_2", 0) or 0)
                fx = float(row.get("frst_bltn_exrt", 0) or 0)
                break

        positions = []
        try:
            bal = self._overseas_balance_raw()
            for it in bal.get("output1", []) or []:
                qty = int(float(it.get("ovrs_cblc_qty", 0) or 0))
                if qty <= 0:
                    continue
                # KIS는 클래스주를 슬래시(BRK/B)로 주지만, dataset·ledger·us_metrics는
                # 대시 표준형(BRK-B)을 쓴다 → 정규화해야 reconcile가 오판하지 않음.
                sym = (it.get("ovrs_pdno") or "").strip().replace("/", "-")
                positions.append({
                    "symbol": sym,
                    "name": it.get("ovrs_item_name", ""),
                    "qty": qty,
                    "avg_price": float(it.get("pchs_avg_pric", 0) or 0),
                    "eval_price": float(it.get("now_pric2", 0) or 0),
                    "market": market_index.exchange_of(sym) or "US",
                    "currency": "USD",
                })
        except Exception as e:
            log.warning("해외 보유종목 조회 실패 — 현금·환율만 반영: %s", e)

        # foreign_eval_krw 직접 계산 — KIS frcr_evlu_tota 필드 mismatch (~5x) 회피.
        positions_eval_usd = sum(p["qty"] * p["eval_price"] for p in positions)
        foreign_eval_krw = (usd_cash + positions_eval_usd) * fx if fx > 0 else 0.0

        return {"usd_cash": usd_cash, "fx_usdkrw": fx,
                "foreign_eval_krw": foreign_eval_krw, "positions": positions}

    def buying_power_usd(self, symbol: str, ref_price: float) -> dict:
        """특정 미국 종목·가격 기준 USD 주문가능금액·수량 + 환율. (TTTS3007R/VTTS3007R)

        ref_price 기준 사이징(P6)에 사용. exrt(환율)는 사이징 검증·표시용.
        """
        from . import market_index
        tr = "VTTS3007R" if self.virtual else "TTTS3007R"
        excd = market_index.exchange_of(symbol) or "NAS"
        d = self._get_retry(
            "/uapi/overseas-stock/v1/trading/inquire-psamount", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "OVRS_EXCG_CD": self._OVERSEAS_EXCD.get(excd, "NASD"),
                "OVRS_ORD_UNPR": f"{ref_price:.2f}",
                "ITEM_CD": market_index.kis_ticker_of(symbol)})
        o = d.get("output", {}) or {}
        return {
            "usd_orderable": float(o.get("frcr_ord_psbl_amt1", 0) or 0),
            "max_qty": int(float(o.get("max_ord_psbl_qty", 0) or 0)),
            "fx_usdkrw": float(o.get("exrt", 0) or 0),
        }

    # ── 시장 라우팅 ──────────────────────────────────────────────────────────

    def _detect_market(self, symbol: str) -> str:
        """종목 → 시장/거래소. KIS 마스터 인덱스를 권위 소스로 사용.

        반환: "DOMESTIC" 또는 미국 거래소 "NAS"/"NYS"/"AMS".
        과거의 코드길이 휴리스틱(영문이면 NAS 가정)은 NYSE/AMEX 오라우팅을
        유발해 폐기. 미국 티커인데 마스터 인덱스에 없으면(다운로드 실패 등)
        거래소를 추측하지 않고 RoutingError를 던져 발주를 차단한다.
        (호출자 Trader._submit_*가 try/except로 감싸 'error' 결정으로 기록·보류.)
        """
        from . import market_index
        exch = market_index.exchange_of(symbol)
        if exch:
            return exch                       # NAS / NYS / AMS
        if market_index._looks_domestic(symbol):
            return "DOMESTIC"
        s = symbol.strip().upper()
        if s.isalpha() and 1 <= len(s) <= 5:
            raise market_index.RoutingError(
                f"미국 티커로 보이나 마스터 인덱스에 없음: {symbol} — "
                f"인덱스 갱신 필요. 발주 보류.")
        return "DOMESTIC"                     # 국내 안전 기본

    def price(self, symbol: str) -> float:
        """현재가 조회 — 시장에 따라 다른 endpoint."""
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            return self._price_domestic(symbol)
        return self._price_overseas(symbol, market)

    def today_open(self, symbol: str) -> float:
        """당일 시가 조회 — 시장에 따라 다른 endpoint.

        catch-up cycle에서 시장가 매수를 시초가 limit으로 변환할 때 사용.
        백테스트의 시장가 모델("시가 + slippage")과 alignment 위해 시초가
        기준 limit 발주.

        시가 못 받으면(장 시작 전·휴장·종목 코드 오류 등) 0.0 반환 — 호출자가
        catch-up skip 결정.
        """
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            return self._open_domestic(symbol)
        return self._open_overseas(symbol, market)

    def _price_domestic(self, symbol: str) -> float:
        body = self._get_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            base=self.quote_base)
        return float(body.get("output", {}).get("stck_prpr", 0))

    def _open_domestic(self, symbol: str) -> float:
        """국내 당일 시가 — inquire-price 응답의 stck_oprc."""
        body = self._get_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            base=self.quote_base)
        try:
            return float(body.get("output", {}).get("stck_oprc", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _open_overseas(self, symbol: str, market: str) -> float:
        """해외 당일 시가 — HHDFS76200200 (해외주식 현재가상세).

        v0.9.7-beta — HHDFS00000300(현재체결가)에서 변경.
        이유 (사용자 환경 실측):
          - HHDFS00000300 응답에 open 필드 자체 없음 (last·base·tvol만 11개 필드)
            → 모의·실전 모두 시초가 미제공 → catch-up 매수 6/6 skip
          - HHDFS76200200(현재가상세)는 41 필드 응답에 open·high·low 명시 포함
          - doc상 "모의 미지원"이지만 시세는 모든 KIS 사용자가 실전 도메인 사용
            (self.quote_base=_REAL) → 모의 appkey + 실전 도메인 조합 동작 확인
          - 한 번 REST 호출로 OHLC + 52주·PER·EPS·시가총액까지 받음
          - 분봉 endpoint는 NREC 한도(~120봉)로 미장 4시간+ 진행 시 시초가 도달 불가

        호출 실패·output 비어있으면 0.0 반환 → trader.catchup branch가
        prev_close fallback으로 처리 (PR-1 정당: KIS API 진짜 한계 대비).
        """
        from . import market_index
        # 시세(quote) endpoint는 short EXCD(NAS/NYS/AMS)를 그대로 사용. 지원 시장
        # 집합은 _OVERSEAS_EXCD 단일 출처로 검증(주문용 NASD-form과 키 동일).
        excd = market if market in self._OVERSEAS_EXCD else "NAS"
        body = self._get_retry(
            "/uapi/overseas-price/v1/quotations/price-detail",
            "HHDFS76200200",
            {"AUTH": "", "EXCD": excd, "SYMB": market_index.kis_ticker_of(symbol)},
            base=self.quote_base)
        v = body.get("output", {}).get("open")
        if v not in (None, "", "0"):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _price_overseas(self, symbol: str, market: str) -> float:
        # KIS overseas 시장 코드: NAS/NYS/AMS = 실시간(NASD/NYSE/AMEX) 또는 지연(NAS/NYS/AMS).
        # 우선 지연 시세(별도 신청 불필요) 사용.
        from . import market_index
        excd = market if market in self._OVERSEAS_EXCD else "NAS"
        body = self._get_retry(
            "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
            {"AUTH": "", "EXCD": excd, "SYMB": market_index.kis_ticker_of(symbol)},
            base=self.quote_base)
        last = body.get("output", {}).get("last", "0")
        try:
            return float(last)
        except (TypeError, ValueError):
            return 0.0

    # ── 주문 ──────────────────────────────────────────────────────────────────

    def _submit(self, symbol: str, qty: int, side: str,
                ord_dvsn: str, unit_price: float) -> dict:
        """주문 라우팅 — 시장에 따라 국내/해외 endpoint.

        unit_price는 float — 국내는 정수 KRW로, 해외는 소수 USD로 포맷한다.
        (해외 $0.01 틱 가격이 int 절삭으로 망가지지 않도록.)
        """
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            return self._submit_domestic(symbol, qty, side, ord_dvsn, unit_price)
        return self._submit_overseas(symbol, qty, side, ord_dvsn, unit_price, market)

    def _submit_domestic(self, symbol: str, qty: int, side: str,
                          ord_dvsn: str, unit_price: float) -> dict:
        """국내주식 주문 — order-cash endpoint.

        ord_dvsn: 00=지정가, 01=시장가. 가격은 정수 KRW로 포맷.
        """
        # KIS 공식 spec ([국내주식] 주문_계좌.xlsx 주식주문(현금)):
        #   매수: TTTC0012U (실전) / VTTC0012U (모의)
        #   매도: TTTC0011U (실전) / VTTC0011U (모의)
        # v0.8.4 이전엔 TTTC0802U/0801U 사용 — KIS grace로 동작했으나
        # 공식 spec엔 미명시. v0.8.5에서 새 spec으로 migrate.
        if side == "buy":
            tr = "VTTC0012U" if self.virtual else "TTTC0012U"
        else:
            tr = "VTTC0011U" if self.virtual else "TTTC0011U"
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "PDNO": symbol, "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(unit_price)) if ord_dvsn == "00" else "0",
        }
        d = self._post_retry("/uapi/domestic-stock/v1/trading/order-cash",
                             tr, body, timeout=10)
        return {
            "success": d.get("rt_cd") == "0",
            "message": d.get("msg1", ""),
            "msg_cd": d.get("msg_cd", ""),
            "order_no": d.get("output", {}).get("ODNO", ""),
            "ord_branch": d.get("output", {}).get("KRX_FWDG_ORD_ORGNO", ""),
            "filled_qty": 0,
        }

    # 해외 매수/매도 TR_ID 매핑 — KIS 공식 spec ([해외주식] 주문_계좌.xlsx 해외주식 주문)
    # 실전 미국 매수: TTTT1002U, 미국 매도: TTTT1006U (1001 아님 — v0.8.5 이전 잘못)
    # 실전 미국 J-prefix는 spec에 없음 — v0.8.5 이전 잘못된 옛 형식
    # 모의 V-prefix 매수=1002U / 매도=1001U는 spec 그대로
    _OVERSEAS_TR = {
        # (market, side, virtual): TR_ID
        ("NAS", "buy",  True): "VTTT1002U", ("NAS", "buy",  False): "TTTT1002U",
        ("NAS", "sell", True): "VTTT1001U", ("NAS", "sell", False): "TTTT1006U",
        ("NYS", "buy",  True): "VTTT1002U", ("NYS", "buy",  False): "TTTT1002U",
        ("NYS", "sell", True): "VTTT1001U", ("NYS", "sell", False): "TTTT1006U",
        ("AMS", "buy",  True): "VTTT1002U", ("AMS", "buy",  False): "TTTT1002U",
        ("AMS", "sell", True): "VTTT1001U", ("AMS", "sell", False): "TTTT1006U",
        ("TSE", "buy",  True): "VTTS0308U", ("TSE", "buy",  False): "TTTS0308U",
        ("TSE", "sell", True): "VTTS0307U", ("TSE", "sell", False): "TTTS0307U",
        ("HKS", "buy",  True): "VTTS1002U", ("HKS", "buy",  False): "TTTS1002U",
        ("HKS", "sell", True): "VTTS1001U", ("HKS", "sell", False): "TTTS1001U",
    }
    _OVERSEAS_EXCD = {
        "NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX",
        "TSE": "TKSE", "HKS": "SEHK",
    }
    # 미국 예약주문 TR — KIS 공식 spec ([해외주식] 예약주문접수, order-resv).
    # 개장 전(서머타임 10:00~22:20 KST 접수) 발주 → 정규장 개시(22:30)에 자동 전송.
    # 미국 전용 (아시아는 접수시간·body 규격이 달라 별도 — 현재 미지원).
    _OVERSEAS_RESV_TR = {
        # (side, virtual): TR_ID
        ("buy",  True): "VTTT3014U", ("buy",  False): "TTTT3014U",
        ("sell", True): "VTTT3016U", ("sell", False): "TTTT3016U",
    }

    def _submit_overseas(self, symbol: str, qty: int, side: str,
                          ord_dvsn: str, unit_price: float, market: str) -> dict:
        """해외주식 주문 — overseas-stock/v1/trading/order endpoint.

        해외주식은 기본적으로 지정가. unit_price=0이면 호출 거부될 수 있어
        시장가 모드에서는 현재가 조회 후 사용. 가격은 소수 USD($0.01)로 포맷.
        """
        tr = self._OVERSEAS_TR.get((market, side, self.virtual))
        if tr is None:
            return {"success": False, "message": f"미지원 시장: {market}",
                    "msg_cd": "", "order_no": "", "filled_qty": 0}
        if unit_price <= 0:
            # 시장가 의도 → 현재가로 대체 (해외는 지정가 강제).
            # 가격 조회 실패 시 fallback하면 비정상 발주 위험(재정 손실)이라
            # 명시적 예외로 차단. 호출자(Trader._submit_buy/_submit_sell)는 이미
            # try/except로 감싸고 있어 decision_log에 'error'로 기록되며 발주는 보류.
            quoted = self._price_overseas(symbol, market)
            if quoted <= 0:
                raise RuntimeError(
                    f"해외 {market} {symbol} 현재가 조회 실패 ({quoted}) — "
                    f"지정가 발주를 위한 가격 없음. 주문 보류.")
            unit_price = quoted
        from . import market_index
        excd = self._OVERSEAS_EXCD.get(market, "NASD")
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "OVRS_EXCG_CD": excd,
            "PDNO": market_index.kis_ticker_of(symbol),   # 슬래시 정규화 (BRK/B)
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{unit_price:.2f}",          # 소수 USD ($0.01 틱)
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",       # 해외는 지정가 (00) 기본
        }
        d = self._post_retry("/uapi/overseas-stock/v1/trading/order", tr, body)
        return {
            "success": d.get("rt_cd") == "0",
            "message": d.get("msg1", ""),
            "msg_cd": d.get("msg_cd", ""),
            "order_no": d.get("output", {}).get("ODNO", ""),
            "ord_branch": "",
            "filled_qty": 0,
        }

    def _submit_overseas_resv(self, symbol: str, qty: int, side: str,
                               ord_dvsn: str, unit_price: float,
                               market: str) -> dict:
        """미국 예약주문 — overseas-stock/v1/trading/order-resv endpoint.

        개장 전 접수 → KIS가 정규장 개시(22:30 서머타임)에 자동 전송.
        매수는 지정가(ORD_DVSN=00, FT_ORD_UNPR3=지정가), 매도는 MOO 장개시
        시장가(ORD_DVSN=31, FT_ORD_UNPR3=0). KIS는 미국 예약 *매수* 시장가를
        지원하지 않으므로 매수는 지정가만 가능.
        """
        if market not in ("NAS", "NYS", "AMS"):
            return {"success": False, "message": f"예약주문 미지원 시장: {market}",
                    "msg_cd": "", "order_no": "", "filled_qty": 0}
        tr = self._OVERSEAS_RESV_TR[(side, self.virtual)]
        from . import market_index
        excd = self._OVERSEAS_EXCD.get(market, "NASD")
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
            "OVRS_EXCG_CD": excd,
            "PDNO": market_index.kis_ticker_of(symbol),   # 슬래시 정규화 (BRK/B)
            "FT_ORD_QTY": str(qty),
            # 지정가는 소수 USD($0.01 틱), MOO(31)는 가격 미사용 → "0".
            "FT_ORD_UNPR3": f"{unit_price:.2f}" if ord_dvsn == "00" else "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,       # 00=지정가(매수), 31=MOO 장개시시장가(매도)
        }
        d = self._post_retry("/uapi/overseas-stock/v1/trading/order-resv", tr, body)
        return {
            "success": d.get("rt_cd") == "0",
            "message": d.get("msg1", ""),
            "msg_cd": d.get("msg_cd", ""),
            "order_no": d.get("output", {}).get("ODNO", ""),
            "ord_branch": "",
            "filled_qty": 0,
        }

    def buy(self, symbol: str, qty: int) -> dict:
        return self._submit(symbol, qty, "buy", "01", 0.0)

    def sell(self, symbol: str, qty: int) -> dict:
        return self._submit(symbol, qty, "sell", "01", 0.0)

    def buy_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        return self._submit(symbol, qty, "buy", "00", float(limit_price))

    def sell_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        return self._submit(symbol, qty, "sell", "00", float(limit_price))

    def buy_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """미국 예약 매수 — 지정가. 개장 전 접수 → 시초가 전송."""
        from . import market_index
        market = market_index.exchange_of(symbol) or "NAS"
        return self._submit_overseas_resv(symbol, qty, "buy", "00",
                                           float(limit_price), market)

    def sell_resv_moo(self, symbol: str, qty: int) -> dict:
        """미국 예약 매도 — MOO(장개시시장가). 개장 전 접수 → 시초가 시장가 체결."""
        from . import market_index
        market = market_index.exchange_of(symbol) or "NAS"
        return self._submit_overseas_resv(symbol, qty, "sell", "31", 0.0, market)

    # ── 주문 취소 / 조회 ──────────────────────────────────────────────────────

    def _us_excd(self, symbol: str) -> str:
        """미국 종목 → KIS 주문/조회용 거래소 코드 (NASD/NYSE/AMEX)."""
        from . import market_index
        return self._OVERSEAS_EXCD.get(market_index.exchange_of(symbol) or "NAS",
                                        "NASD")

    def cancel(self, order_no: str, symbol: str, qty: int,
               ord_branch: str = "") -> dict:
        """미체결 주문 전량 취소 — 국내/해외 시장에 따라 endpoint 분기."""
        from . import market_index
        if symbol and market_index.is_us(symbol):
            return self._cancel_overseas(order_no, symbol, qty)
        # 정정/취소 — KIS 공식 spec: TTTC0013U / VTTC0013U
        tr = "VTTC0013U" if self.virtual else "TTTC0013U"
        d = self._post_retry(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "KRX_FWDG_ORD_ORGNO": ord_branch or "",
                "ORGN_ODNO": order_no, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",       # 02 = 취소
                "ORD_QTY": str(qty), "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
            }, timeout=10)
        return {"success": d.get("rt_cd") == "0",
                "message": d.get("msg1", ""),
                "msg_cd": d.get("msg_cd", "")}

    def _cancel_overseas(self, order_no: str, symbol: str, qty: int) -> dict:
        """해외 미체결 취소 — order-rvsecncl (VTTT1004U/TTTT1004U)."""
        from . import market_index
        tr = "VTTT1004U" if self.virtual else "TTTT1004U"
        d = self._post_retry(
            "/uapi/overseas-stock/v1/trading/order-rvsecncl", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "OVRS_EXCG_CD": self._us_excd(symbol),
                "PDNO": market_index.kis_ticker_of(symbol),
                "ORGN_ODNO": order_no,
                "RVSE_CNCL_DVSN_CD": "02",       # 02 = 취소
                "ORD_QTY": str(qty),
                "OVRS_ORD_UNPR": "0",
                "ORD_SVR_DVSN_CD": "0",
            })
        return {"success": d.get("rt_cd") == "0",
                "message": d.get("msg1", ""),
                "msg_cd": d.get("msg_cd", "")}

    def _daily_ccld(self) -> dict:
        """당일 주문체결 조회 — 미체결·체결·취소 모두 포함.

        KIS 공식 spec: TTTC0081R / VTTC0081R (3개월 이내). v0.8.4 이전엔
        TTTC8001R 사용 — KIS grace로 동작했으나 공식 미명시.
        """
        tr = "VTTC0081R" if self.virtual else "TTTC0081R"
        today = datetime.now().strftime("%Y%m%d")
        return self._get_retry(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "INQR_STRT_DT": today, "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
                "PDNO": "", "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "", "ODNO": "",
                "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            })

    def order_status(self, order_no: str, symbol: str | None = None) -> dict:
        """특정 주문번호의 현재 상태 — 국내/해외 시장에 따라 분기.

        symbol이 미국 종목이면 해외 체결조회(inquire-ccnl), 아니면 국내
        일별체결조회(inquire-daily-ccld). symbol 없으면 국내(레거시 호환).
        """
        from . import market_index
        if symbol and market_index.is_us(symbol):
            return self._overseas_order_status(order_no, symbol)
        try:
            body = self._daily_ccld()
        except Exception as e:
            log.warning("주문 조회 실패: %s", e)
            return {"order_no": order_no, "status": "unknown",
                    "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in body.get("output1", []) or []:
            if canonical_odno(row.get("odno")) == canonical_odno(order_no):
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

    # ── 해외 체결/미체결 조회 (inquire-ccnl / inquire-nccs) ───────────────────

    def _overseas_ccnl_today(self, symbol: str) -> list[dict]:
        """해외 당일 주문체결 내역 (inquire-ccnl, VTTS3035R/TTTS3035R)."""
        tr = "VTTS3035R" if self.virtual else "TTTS3035R"
        today = datetime.now().strftime("%Y%m%d")
        d = self._get_retry(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "PDNO": "", "ORD_STRT_DT": today, "ORD_END_DT": today,
                "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": self._us_excd(symbol), "SORT_SQN": "DS",
                "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        return d.get("output", []) or d.get("output1", []) or []

    def _overseas_order_status(self, order_no: str, symbol: str) -> dict:
        """해외 주문 상태 — inquire-ccnl에서 odno 매칭. 국내와 동일 status 어휘."""
        try:
            rows = self._overseas_ccnl_today(symbol)
        except Exception as e:
            log.warning("해외 주문 조회 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown",
                    "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("odno")) != canonical_odno(order_no):
                continue
            ord_qty = int(float(row.get("ft_ord_qty", 0) or 0))
            ccld_qty = int(float(row.get("ft_ccld_qty", 0) or 0))
            fill_px = float(row.get("ft_ccld_unpr3", 0) or 0)
            prcs = row.get("prcs_stat_name", "") or ""
            rjct = (row.get("rjct_rson_name", "") or "").strip()
            if rjct or "거부" in prcs:
                status = "cancelled"        # 거부 — pending에서 제거
            elif "취소" in prcs:
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
                    "fill_price": fill_px, "ord_branch": ""}
        return {"order_no": order_no, "status": "unknown",
                "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def _overseas_pending(self) -> list[dict]:
        """해외 미체결 목록 (inquire-nccs, TTTS3018R).

        KIS 공식 spec: 모의투자 미지원. v0.8.5 이전엔 VTTS3018R 사용했으나
        spec 미명시 — 호출 실패 위험. virtual=True 면 빈 결과 반환.
        """
        if self.virtual:
            return []   # KIS 모의는 inquire-nccs 미지원 (spec)
        tr = "TTTS3018R"
        excgs = ["NASD", "NYSE", "AMEX"]
        out = []
        for excd in excgs:
            try:
                d = self._get_retry(
                    "/uapi/overseas-stock/v1/trading/inquire-nccs", tr, {
                        "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                        "OVRS_EXCG_CD": excd, "SORT_SQN": "DS",
                        "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
            except Exception as e:
                log.warning("해외 미체결 조회 실패 [%s]: %s", excd, e)
                continue
            for row in d.get("output", []) or d.get("output1", []) or []:
                remain = int(float(row.get("nccs_qty", 0) or 0))
                if remain <= 0:
                    continue
                out.append({
                    "order_no": row.get("odno", ""),
                    "symbol": (row.get("pdno") or "").strip().replace("/", "-"),
                    "name": row.get("prdt_name", "") or row.get("name", ""),
                    "side": "buy" if row.get("sll_buy_dvsn_cd") == "02" else "sell",
                    "qty": int(float(row.get("ft_ord_qty", 0) or 0)),
                    "filled_qty": int(float(row.get("ft_ccld_qty", 0) or 0)),
                    "remain_qty": remain,
                    "limit_price": float(row.get("ft_ord_unpr3", 0) or 0),
                    "ord_branch": "", "submitted_at": row.get("ord_dt", ""),
                    "market": "US", "currency": "USD",
                })
        return out

    def pending_orders(self) -> list[dict]:
        """현재 미체결 잔량이 있는 주문 목록 — 국내 + 해외(미국) 통합.

        해외 조회 실패는 비치명적 — 국내 목록은 유지(견고성).
        """
        out = []
        try:
            body = self._daily_ccld()
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
                    "market": "DOMESTIC", "currency": "KRW",
                })
        except Exception as e:
            log.warning("국내 미체결 조회 실패: %s", e)
        try:
            out.extend(self._overseas_pending())
        except Exception as e:
            log.warning("해외 미체결 조회 실패: %s", e)
        return out
