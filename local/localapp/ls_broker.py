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


class LsBroker:
    """LS증권 모의/실전 브로커. Broker Protocol 구현(국내주식 Phase 2).

    ⚠ B6 조회·주문 메서드 전체 초안(draft) — Phase C 키 발급 후 실측 확정.
    """

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

    def account_snapshot(self, overseas: bool = True) -> dict:
        """국내주식 잔고 스냅샷.

        overseas: Broker 프로토콜 서명 패리티를 위해 수락하지만 무시한다 — LS는 국내 only.

        반환 balance:
          cash         추정D2예수금(KRW) — t0424OutBlock.sunamt1 (공식문서 대조 확인 2026-06-19)
                       ⚠ TODO(Phase C): 정확한 주문가능금액은 별도 TR CSPAQ22200 필요.
          total_eval   평가금액(KRW) — t0424OutBlock.tappamt (공식문서 대조 확인 2026-06-19)
          cash_usd     0.0  (LS 국내 only)
          fx_usdkrw    0.0
          foreign_eval_krw  0.0

        positions: qty>0 인 국내 종목만. market="DOMESTIC", currency="KRW".

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
        # ⚠ TODO(Phase C): 정확한 주문가능금액은 별도 TR CSPAQ22200 필요.
        # total_eval: tappamt = 평가금액 (공식문서 대조 확인 2026-06-19)
        cash = int(float(summary.get("sunamt1") or 0))
        total_eval = int(float(summary.get("tappamt") or 0))

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

        return {
            "balance": {
                "cash": cash,
                "total_eval": total_eval,
                "cash_usd": 0.0,
                "fx_usdkrw": 0.0,
                "foreign_eval_krw": 0.0,
            },
            "positions": positions,
        }

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

    def price(self, symbol: str) -> float:
        """현재가(KRW) 반환. ⚠ t1102OutBlock.price 필드 — A2 KB 🟢."""
        body = self._price_raw(symbol)
        out = body.get("t1102OutBlock") or {}
        return float(out.get("price") or 0)

    def today_open(self, symbol: str) -> float:
        """당일 시가 반환. catch-up cycle에서 시장가→시초가 limit 변환 시 사용.

        시가 없으면(개장 전·장 종료 후·오류) 0.0 반환 → caller가 catch-up skip 결정.
        ⚠ t1102OutBlock.open 필드 — A2 KB 🟢.
        """
        try:
            body = self._price_raw(symbol)
            out = body.get("t1102OutBlock") or {}
            v = out.get("open")
            if v is None or v == "" or v == 0:
                return 0.0
            return float(v)
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

    def buy(self, symbol: str, qty: int) -> dict:
        """시장가 매수. ⚠ OrdprcPtnCode="03" — A2 KB G8 🟢."""
        return self._submit(symbol, qty, "buy", "03", 0.0)

    def sell(self, symbol: str, qty: int) -> dict:
        """시장가 매도. ⚠ OrdprcPtnCode="03" — A2 KB G8 🟢."""
        return self._submit(symbol, qty, "sell", "03", 0.0)

    def buy_limit(self, symbol: str, qty: int, limit_price: int) -> dict:
        """지정가 매수. ⚠ OrdprcPtnCode="00" — A2 KB G8 🟢."""
        return self._submit(symbol, qty, "buy", "00", float(limit_price))

    def sell_limit(self, symbol: str, qty: int, limit_price: int) -> dict:
        """지정가 매도. ⚠ OrdprcPtnCode="00" — A2 KB G8 🟢."""
        return self._submit(symbol, qty, "sell", "00", float(limit_price))

    def buy_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """국내주식 예약주문 미구현 — 명시적 에러(조용한 no-op 금지).

        LS 국내주식에 예약주문 TR이 없음(해외주식 후속 plan에서 검토 예정).
        Broker 인터페이스 계약 이행: 미지원을 침묵으로 가장하면 Trader가
        주문이 접수된 것으로 오판한다.
        """
        raise NotImplementedError("LS 예약주문은 해외주식 단계(후속 plan)")

    def sell_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict:
        """국내주식 예약주문 미구현 — 명시적 에러."""
        raise NotImplementedError("LS 예약주문은 해외주식 단계(후속 plan)")

    # ── 취소 (CSPAT00801) ─────────────────────────────────────────────────────

    def cancel(self, order_no: str, symbol: str, qty: int) -> dict:
        """미체결 주문 취소 — CSPAT00801.

        PATH "/stock/order" — 신규/정정/취소 모두 동일 경로, tr_cd 헤더로 TR 구분.
        커뮤니티 래퍼 대조 확인: "/stock/order-cancel" 경로는 404 반환.
        ⚠ InBlock 키 "CSPAT00801InBlock1" — A2 KB 🟢.
        ⚠ OrgOrdNo long, IsuNo="A"+6자리, OrdQty — A2 KB 🟢.
        ⚠ AcntNo/InptPwd 필요 여부 — 미검증. 현재 AcntNo 포함.
        """
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

    def _pending_raw(self) -> dict:
        """t0425 주식미체결조회 — 전 종목 미체결 주문.

        ⚠ PATH "/stock/accno" — A2 KB 가정(t0424와 동일 경로, tr_cd로 구분).
        ⚠ InBlock 필드(expcode/chegb/medosu/sortgb/cts_ordno) — A2 KB 🟢.
        chegb="2" → 미체결만 조회.
        """
        return self._post(
            "/stock/accno", "t0425",
            {"t0425InBlock": {
                "expcode": "",      # 전 종목 🟢
                "chegb": "2",       # 미체결 🟢
                "medosu": "0",      # 전체(매수+매도) 🟢
                "sortgb": "1",      # 최신 역순 🟢
                "cts_ordno": "",
            }},
        )

    def order_status(self, order_no: str, symbol: str | None = None,
                     hint: dict | None = None) -> dict:
        """특정 주문번호의 현재 상태 — t0425 미체결 조회.

        hint: Broker 인터페이스 계약 파라미터. 국내주식은 무시(해외 예약주문 전용).

        반환 어휘: filled | partial | submitted | cancelled | unknown
        ⚠ t0425OutBlock1 필드(ordno/qty/ordrem/medosu/price) — A2 KB 🟢.
        ⚠ **한계(docs/ls-api GOTCHAS G10)**: t0425를 chegb="2"(미체결만)로 조회하므로
          전량 체결·취소된 주문은 목록에서 사라져 status="unknown"으로 떨어진다 —
          즉 폴링으로는 filled/cancelled를 인지하지 못하고, 체결은 15:50 정산
          reconcile_with_kis(실보유 diff)가 백스톱으로 잡는다. Phase C에서 chegb="0"(전체)
          또는 일별체결 TR로 전환(filled/cancelled 구분 필드 실측 후). 자금/방향 위험 없음.
        """
        try:
            body = self._pending_raw()
        except Exception as e:
            log.warning("LS order_status 조회 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown",
                    "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

        for row in body.get("t0425OutBlock1") or []:
            if canonical_odno(row.get("ordno")) == canonical_odno(order_no):
                orig_qty = int(float(row.get("qty") or 0))
                remain_qty = int(float(row.get("ordrem") or 0))
                filled_qty = max(0, orig_qty - remain_qty)
                # ⚠ t0425는 체결/취소 구분 필드 미확인 — 현재 qty/ordrem으로만 판정.
                if remain_qty == 0 and orig_qty > 0:
                    status = "filled"
                elif 0 < remain_qty < orig_qty:
                    status = "partial"
                else:
                    status = "submitted"
                return {
                    "order_no": order_no,
                    "status": status,
                    "filled_qty": filled_qty,
                    "remain_qty": remain_qty,
                    # cheprice = 체결가격, price = 주문가격 (A2 KB 🟢 공식문서 대조 확인 2026-06-19)
                    "fill_price": float(row.get("cheprice") or row.get("price") or 0),
                }

        # 목록에 없으면 unknown (이미 체결 전체 → chegb="2" 에서 사라짐 가능)
        return {"order_no": order_no, "status": "unknown",
                "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def pending_orders(self) -> list[dict]:
        """미체결 주문 목록 — t0425 기반.

        실패는 비치명적 — 로그 후 [] 반환.
        ⚠ t0425OutBlock1 medosu 필드: "매수"/"매도" 문자열 반환 — A2 KB 🟢.
        ⚠ hname(종목명) 필드: t0425OutBlock1에 포함 여부 미확인 — 없으면 "".
        ⚠ submitted_at: ordtime 형식(HHMMSSMMM) — A2 KB 🟢.
        """
        try:
            body = self._pending_raw()
        except Exception as e:
            log.warning("LS pending_orders 조회 실패: %s", e)
            return []

        out = []
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
        return out


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
