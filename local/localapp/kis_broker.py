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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import APP_DIR
from .secrets_store import load_kis
from .state_store import save_json

log = logging.getLogger("localapp.kis_broker")

_VTS = "https://openapivts.koreainvestment.com:29443"
_REAL = "https://openapi.koreainvestment.com:9443"
_TOKEN_CACHE = APP_DIR / ".kis_token.json"


def _overseas_query_window(now: datetime | None = None) -> tuple[str, str]:
    """해외 체결조회 날짜창 [미국 현지 D-1, KST 오늘] (yyyymmdd, yyyymmdd).

    KIS inquire-ccnl 체결행의 주문일자는 **미국 현지 날짜**다 — KST 자정~미장마감
    (≈06:00) 구간에서 KST 당일만 조회하면 진행 중 세션의 체결이 0행이 된다(실측
    2026-06-12: KST 04:55 발주 GOOG 매도의 체결행 주문일자=20260611 → 20260612
    단일일 조회 0행 → 'unknown' → 체결 영구 미기록). 미국 현지 D-1부터 KST 오늘
    까지 조회해 시차·날짜 convention 차이를 모두 덮는다.
    """
    now = now or datetime.now(timezone.utc)
    start = (now.astimezone(ZoneInfo("America/New_York")).date()
             - timedelta(days=1)).strftime("%Y%m%d")
    end = now.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    return start, end


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
                # ★ε: "조회 실패"와 "진짜 0"을 소비자가 구분하도록 표식. 표시용
                # 소비자는 국내만으로 견고하게 동작하되, 위험 결정(킬스위치·
                # day_start·equity 시계열)은 이 표식을 보고 평가를 보류한다 —
                # 06-09 부분 equity(-98%)가 킬스위치를 거짓 발동해 US 보유 전량을
                # 청산한 사고의 근본 수정.
                balance["fetch_failed"] = ["overseas"]

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

    def expected_fill_price(self, symbol: str) -> float:
        """국내주식 동시호가 예상체결가(antc_cnpr) — 없으면(연속거래 등 0) 현재가.

        §18 주식 크레딧 기준가 정밀화(2026-07-19 유저 확정): 사이징 시점(아침 08:35~55·
        종가 15:25 발주)이 동시호가 창이라 '현재가'는 직전가(전일 종가·15:19가)에 머문다 —
        FHKST01010200(호가/예상체결) output2의 예상체결가가 단일가 형성 예상치로 더 정확.
        국내 전용(해외는 동시호가 예상가 미제공) — 비국내·파싱 불가는 0.0(호출자 폴백).
        """
        if self._detect_market(symbol) != "DOMESTIC":
            return 0.0
        body = self._get_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            base=self.quote_base)
        out2 = body.get("output2") or {}
        try:
            antc = float(out2.get("antc_cnpr", 0) or 0)
            if antc > 0:
                return antc
            return float(out2.get("stck_prpr", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

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

    # ── 종가창 급등/상한가 스캔 (자동매매 템플릿 limit_up_close_v1) ────────────────

    def scan_close_surge(self, min_change_pct: float) -> list[dict]:
        """마감 동시호가 중 예상체결 상승 상위 스캔 — FHPST01820000 + 상한가 대조.

        예상체결 상승상위(장마감예상 모드 fid_mkop_cls_code=1)로 당일 등락률 상위를
        받고(상승률순·최대 30건·연속조회 불가 — 상한가 후보 규모에 충분), 임계 이상
        후보만 종목별 현재가 TR(FHKST01010100)의 상한가(stck_mxpr)와 대조해
        is_limit_up(예상체결가≥상한가 = 상한가 잠김 마감 예상)을 판정한다.

        시세 도메인(quote_base=실전) 호출 — 모의계좌도 실전 시세앱키로 동작 전망이나
        랭킹 TR은 공식 문서상 모의 미지원이라 실측 게이트(설계 §6 ⓐ)로 확정한다.
        개별 row 파싱 실패는 그 종목만 제외+경고(전체 스캔은 계속), TR 자체 실패는
        예외 전파 — 호출자(runner)가 진입 skip+결정 경보로 표면화한다(fail-soft).
        """
        body = self._get_retry(
            "/uapi/domestic-stock/v1/ranking/exp-trans-updown", "FHPST01820000",
            {"fid_rank_sort_cls_code": "0",      # 상승률순(내림차순 — 임계 미달에서 중단)
             "fid_cond_mrkt_div_code": "J",
             "fid_cond_scr_div_code": "20182",
             "fid_input_iscd": "0000",            # 전체(시장 필터는 호출자 — 브로커 중립)
             "fid_div_cls_code": "0",
             "fid_aply_rang_prc_1": "",
             "fid_vol_cnt": "",
             "fid_pbmn": "",
             "fid_blng_cls_code": "0",
             "fid_mkop_cls_code": "1"},           # 장마감예상
            base=self.quote_base)
        rows: list[dict] = []
        for it in body.get("output") or []:
            code = str(it.get("stck_shrn_iscd") or "").strip()
            try:
                px = float(it.get("stck_prpr") or 0)       # 예상체결가(마감예상 화면 맥락)
                chg = float(it.get("prdy_ctrt") or 0)      # 전일 대비율(%)
            except (TypeError, ValueError):
                log.warning("[스캔] FHPST01820000 row 파싱 실패 — 제외: %s", it)
                continue
            if chg < min_change_pct:
                break                                       # 상승률순 — 이하 전부 미달
            if not code or px <= 0:
                continue
            mxpr = self._upper_limit_price(code)
            rows.append({"symbol": code,
                         "name": str(it.get("hts_kor_isnm") or "").strip(),
                         "price": px, "change_pct": chg,
                         "is_limit_up": mxpr > 0 and px >= mxpr,
                         "ask_rem": float(it.get("total_askp_rsqn") or 0)})
        return rows

    def _upper_limit_price(self, symbol: str) -> float:
        """당일 상한가(stck_mxpr) — FHKST01010100. 조회 실패는 0.0(호출자가 is_limit_up=False로
        보수 판정 — 잠김 아님으로 제외되는 방향이라 자금 안전)."""
        body = self._get_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            base=self.quote_base)
        try:
            return float(body.get("output", {}).get("stck_mxpr", 0) or 0)
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
                               unit_price: float, market: str) -> dict:
        """미국 예약주문 — overseas-stock/v1/trading/order-resv endpoint.

        개장 전 접수 → KIS가 정규장 개시(22:30 서머타임)에 자동 전송.
        매수·매도 모두 **지정가(ORD_DVSN=00)**. KIS 예약매수는 지정가만 가능하고,
        예약매도의 MOO(31)는 모의 지원이 미검증(KB GOTCHAS)이라 모의=실전 통일을
        위해 양방향 00 지정가로 고정한다. limit=신선한 현재가×(1±tol)이 개장가를
        넉넉히 brackets → 개장 단일가 체결(시장가 근사).
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
            "FT_ORD_UNPR3": f"{unit_price:.2f}",            # 소수 USD($0.01 틱) 지정가
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",          # 00=지정가 (예약 매수·매도 공통)
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
        """미국 예약 매수 — 지정가(00). 개장 전 접수 → 개장 단일가 체결."""
        from . import market_index
        market = market_index.exchange_of(symbol) or "NAS"
        return self._submit_overseas_resv(symbol, qty, "buy",
                                           float(limit_price), market)

    def sell_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """미국 예약 매도 — 지정가(00). 개장 전 접수 → 개장 단일가 체결.

        MOO(31) 대신 지정가 — 예약매도 MOO의 모의 지원이 미검증이라 모의=실전
        통일 위해 지정가로 발주(limit=현재가×(1−tol)이 개장가보다 낮아 매도 체결)."""
        from . import market_index
        market = market_index.exchange_of(symbol) or "NAS"
        return self._submit_overseas_resv(symbol, qty, "sell",
                                           float(limit_price), market)

    # ── 주문 취소 / 조회 ──────────────────────────────────────────────────────

    def _us_excd(self, symbol: str) -> str:
        """미국 종목 → KIS 주문/조회용 거래소 코드 (NASD/NYSE/AMEX)."""
        from . import market_index
        return self._OVERSEAS_EXCD.get(market_index.exchange_of(symbol) or "NAS",
                                        "NASD")

    def cancel(self, order_no: str, symbol: str, qty: int,
               ord_branch: str = "", *, partial: bool = False) -> dict:
        """미체결 주문 취소 — 국내/해외 시장에 따라 endpoint 분기.

        partial=True = 잔량 중 qty만 취소(부분취소). 기본 False는 종전 전량 취소와
        byte-identical이다. 국내 QTY_ALL_ORD_YN은 KB 정의가 'Y@전량 N@일부'
        (docs/kis-api/endpoints/domestic-order/TTTC0013U_주식주문-정정취소.md)라
        그 필드로 의사를 전송한다. **국내주식 자체 실측은 없다(문서 정의 근거).**
        다만 같은 성격의 선물 필드(RMN_QTY_YN)는 실측 2026-07-20에서 문서대로
        동작했다 — "Y"는 ORD_QTY를 무시하고 잔량 전부를 취소한다. 즉 플래그를
        "Y"로 둔 채 부분취소를 요청하면 주문 전체가 취소되므로, 수량만으로
        부분취소가 될 것이라 가정하지 않는다.
        해외는 잔량전부 플래그 자체가 없어(KB TTTT1004U 요청 필드표) ORD_QTY가
        곧 취소 수량 — 전달할 필드가 없으므로 만들지 않는다."""
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
                "QTY_ALL_ORD_YN": "N" if partial else "Y",
            }, timeout=10)
        return {"success": d.get("rt_cd") == "0",
                "message": d.get("msg1", ""),
                "msg_cd": d.get("msg_cd", "")}

    def _cancel_overseas(self, order_no: str, symbol: str, qty: int) -> dict:
        """해외 미체결 취소 — order-rvsecncl (VTTT1004U/TTTT1004U).

        잔량전부 플래그(국내 QTY_ALL_ORD_YN 상당)가 요청 규격에 없다 — ORD_QTY가
        곧 취소 수량이라 부분/전량이 같은 경로다(KB TTTT1004U 요청 필드표 대조
        2026-07-20). 없는 필드를 추측으로 만들지 않는다."""
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

    def _daily_ccld(self, start: str | None = None,
                    end: str | None = None) -> dict:
        """일별 주문체결 조회 — 미체결·체결·취소 모두 포함. 기본 = 당일.

        KIS 공식 spec: TTTC0081R / VTTC0081R (3개월 이내). v0.8.4 이전엔
        TTTC8001R 사용 — KIS grace로 동작했으나 공식 미명시.
        start/end("YYYYMMDD") — R2 익일 회수의 제출일자 조회용(기본 인자면
        종전과 byte-identical 당일 조회).
        """
        tr = "VTTC0081R" if self.virtual else "TTTC0081R"
        today = datetime.now().strftime("%Y%m%d")
        return self._get_retry(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "INQR_STRT_DT": start or today, "INQR_END_DT": end or today,
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
                "PDNO": "", "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "", "ODNO": "",
                "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            })

    # ── R2 공개 종결·조회 seam ────────────────────────────────────────────────
    # reconcile(intents)·익일 회수(trader)가 쓰는 조회를 **공개 이름**으로 노출한다.
    # 라우터(BrokerRouter.__getattr__)가 언더스코어를 차단해 _daily_ccld 직접 의존이
    # 선물 라우터 구성 유저에서 구조적 dead였다(리뷰 R2-ⓑ) — 공개 이름은 라우터가
    # stock 브로커로 자동 위임하므로 구성 무관하게 동작한다.

    def daily_orders_today(self) -> list[dict]:
        """당일 국내 주문 raw rows(output1) — intents.reconcile_submitting 매칭용."""
        return self._daily_ccld().get("output1", []) or []

    def fills_on(self, date_yyyymmdd: str) -> list[dict]:
        """지정일 국내 주문의 (주문번호·체결량·체결평균가) 목록 — 익일 회수 확인용.

        DAY 만료로 당일 조회창을 벗어난 pending의 '체결됐었나'를 제출일자로
        재확인한다(R2-② — fill 부재 확인 없이 만료 종결하면 크래시로 미기장된
        지각 체결이 drift 되팔기 실손으로 이어지는 부류).
        """
        body = self._daily_ccld(start=date_yyyymmdd, end=date_yyyymmdd)
        out = []
        for row in body.get("output1", []) or []:
            out.append({"odno": row.get("odno", ""),
                        "filled_qty": int(row.get("tot_ccld_qty", 0) or 0),
                        "fill_price": float(row.get("avg_prvs", 0) or 0),
                        "cancelled": row.get("cncl_yn", "") == "Y"})
        return out

    def overseas_fills_today(self, symbol: str) -> list:
        """당일 해외 체결 rows — intents US reconcile 매칭용(공개 seam)."""
        return self._overseas_ccnl_today(symbol)

    def order_status(self, order_no: str, symbol: str | None = None, *,
                     hint: dict | None = None) -> dict:
        """특정 주문번호의 현재 상태 — 국내/해외 시장에 따라 분기.

        symbol이 미국 종목이면 해외 체결조회(inquire-ccnl), 아니면 국내
        일별체결조회(inquire-daily-ccld). symbol 없으면 국내(레거시 호환).
        hint: 해외 전용 매칭 보조({side,qty,reserved,submitted_ts,exclude_odnos}) —
        예약주문 번호공간 불일치 해소(_overseas_order_status). 국내는 무시.
        """
        from . import market_index
        if symbol and market_index.is_us(symbol):
            return self._overseas_order_status(order_no, symbol, hint=hint)
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

    def _overseas_ccnl_today(self, symbol: str,
                             start_yyyymmdd: str | None = None) -> list[dict]:
        """해외 주문체결 내역 (inquire-ccnl, VTTS3035R/TTTS3035R) — [미국 현지 D-1, KST 오늘] 창.

        start_yyyymmdd: 더 이른 시작일 요청(오래된 pending 추적용). 창 시작일과
        비교해 더 이른 쪽을 쓴다. ⚠ CTX 연속조회 미구현 — 창이 길고 주문이 많으면
        첫 페이지만 본다(소매 주문 빈도에선 실질 무영향, 기존 한계 동일).
        """
        tr = "VTTS3035R" if self.virtual else "TTTS3035R"
        w_start, w_end = _overseas_query_window()
        start = min(start_yyyymmdd, w_start) if start_yyyymmdd else w_start
        d = self._get_retry(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl", tr, {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_cd,
                "PDNO": "", "ORD_STRT_DT": start, "ORD_END_DT": w_end,
                "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": self._us_excd(symbol), "SORT_SQN": "DS",
                "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        return d.get("output", []) or d.get("output1", []) or []

    @staticmethod
    def _status_from_ccnl_row(row: dict, order_no: str) -> dict:
        """inquire-ccnl 행 → 표준 status dict (국내와 동일 어휘 + exec_odno)."""
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
                "fill_price": fill_px, "ord_branch": "",
                # 체결행의 odno — 예약주문 청구 dedup 키(호출자가 레지스트리에 영속)
                "exec_odno": (row.get("odno") or "")}

    def _overseas_order_status(self, order_no: str, symbol: str, *,
                               hint: dict | None = None) -> dict:
        """해외 주문 상태 — inquire-ccnl 매칭. 국내와 동일 status 어휘.

        1차: odno 정확 매칭(일반 주문 — 접수=체결 번호공간 일치).
        2차(hint["reserved"]): 예약주문은 접수번호(예약 번호공간, 실측 3자리 "448")와
        체결행 odno(주문 번호공간 10자리)가 **불일치**해 1차가 영원히 실패한다(실측
        2026-06-11). 종목+매수매도+수량으로 매칭하되, 이미 다른 주문이 청구한 행
        (hint["exclude_odnos"])은 제외 — 동형 주문 2건이 같은 체결행을 이중 기장하는
        것을 차단한다. 동형 후보가 여럿이면 odno 오름차순 첫 행(호출자가 exclude를
        누적하며 1:1 배정).
        """
        hint = hint or {}
        start = None
        sub_ts = hint.get("submitted_ts")
        if sub_ts:
            # 제출일(미국 현지) D-1부터 — 오래된 pending도 체결행을 찾도록.
            sub_et = datetime.fromtimestamp(float(sub_ts),
                                            tz=ZoneInfo("America/New_York"))
            start = (sub_et.date() - timedelta(days=1)).strftime("%Y%m%d")
        try:
            rows = self._overseas_ccnl_today(symbol, start_yyyymmdd=start)
        except Exception as e:
            log.warning("해외 주문 조회 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown",
                    "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("odno")) == canonical_odno(order_no):
                return self._status_from_ccnl_row(row, order_no)
        if hint.get("reserved"):
            want_side = "02" if hint.get("side") == "buy" else "01"
            want_qty = int(hint.get("qty") or 0)
            excl = {canonical_odno(x)
                    for x in (hint.get("exclude_odnos") or ())}
            cands = []
            for row in rows:
                if (row.get("pdno") or "").strip() != symbol:
                    continue
                side_cd = (row.get("sll_buy_dvsn_cd")
                           or row.get("sll_buy_dvsn", "") or "")
                if side_cd not in (want_side, want_side[-1]):
                    continue
                if int(float(row.get("ft_ord_qty", 0) or 0)) != want_qty:
                    continue
                if canonical_odno(row.get("odno")) in excl:
                    continue
                cands.append(row)
            if cands:
                cands.sort(key=lambda r: canonical_odno(r.get("odno")))
                return self._status_from_ccnl_row(cands[0], order_no)
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
                    # asset_class — §19 A1 _pend_scope 정합(R6-④): 종가 주식창
                    # (instrument_class="stock")이 이 키로 필터하는데 부재 시
                    # ""≠"stock"이라 주식 pending이 스코프에서 전부 배제됐다.
                    "market": "DOMESTIC", "currency": "KRW", "asset_class": "stock",
                })
        except Exception as e:
            log.warning("국내 미체결 조회 실패: %s", e)
        try:
            for r in self._overseas_pending():
                r.setdefault("asset_class", "stock")
                out.append(r)
        except Exception as e:
            log.warning("해외 미체결 조회 실패: %s", e)
        return out
