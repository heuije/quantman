"""주식·선물 브로커를 심볼로 라우팅하는 Broker 어댑터 (자동매매 #4 배선, M3).

Trader는 Broker Protocol에만 의존한다. 이 라우터가 그 Protocol을 충족하며, 심볼이 선물이면
KisFuturesBroker로, 아니면 KisBroker로 거래를 위임한다. 선물은 데이터셋 심볼(한글 상품명
"금선물"·"코스피200선물")을 라이브 계약코드(globex GCM26·국내 A01606)로 해석해 전달한다
(resolve 콜백 = futures_contracts.ContractResolver).

주식 전용 메서드(buying_power_usd·pending_orders 등)는 __getattr__로 stock 브로커에 그대로
위임 — **주식 동작 완전 무변경**. make_broker는 선물 자격증명이 없으면 라우터 없이 KisBroker를
그대로 반환하므로, 선물 미사용 환경은 영향이 전혀 없다.

M7(잔고 통합 + market dispatch):
- `account_snapshot`을 오버라이드해 stock + 선물(국내·해외) 포지션을 **병합**하고, 선물은
  라이브 계약코드(GCM26·A01606)를 **데이터셋 심볼("금선물"·"코스피200선물")로 정규화**한다
  (dataset_for_contract). 이로써 ledger·reconcile·over-sell 클램프(전부 데이터셋 심볼 기준)가
  선물 포지션을 인식 — 병합 전엔 선물 청산이 항상 skip되고 reconcile이 고아 오삭제했다.
- 선물 **주문**(buy/sell/limit)은 `futures_market`으로 국내(KRX)→domestic 메서드·해외(CME)→
  `overseas_*` 메서드로 dispatch한다.
⚠ 해외선물 cancel/order_status/price 라우팅은 미완(P2 overseas phase2 미구현) — 국내선물·주식은
  완전. 해외 선물 lifecycle 관리(취소·체결조회·시세)는 해외 활성화 단계에서 완성(모의 없어
  라이브 검증 필요). equity(_unified_equity_krw)에 선물 증거금/평가 정밀 반영도 M8 라이브 후.
"""
from __future__ import annotations

import logging

import quant_core as qc
from quant_core.futures_contract import dataset_for_contract, futures_market

log = logging.getLogger("localapp.broker_router")


class BrokerRouter:
    """stock(KisBroker) + futures(KisFuturesBroker)를 심볼로 라우팅. Broker Protocol 충족(duck)."""

    def __init__(self, stock, futures, *, resolve, resolve_expiry=None, dataset_for_code=None):
        self._stock = stock
        self._futures = futures
        self._resolve = resolve          # callable(symbol) -> 계약코드 | None
        # M6: callable(symbol) -> (계약코드, 만기일date) — 진입 시 ledger 기록(만기 자동청산).
        # 미주입(구 배선)이면 contract_expiry가 (None,None) → Trader는 만기 미기록.
        self._resolve_expiry = resolve_expiry
        # D7: 계약코드→데이터셋 심볼 역매핑 콜백. 기본=quant_core.dataset_for_contract(KIS코드 인식).
        # LS 브로커는 LS-aware 역매핑을 주입해 자신의 코드(101V6000 등)를 정규화한다.
        # KIS 호출자는 이 파라미터를 생략 → 종전과 byte-identical 동작 보존.
        self._d4c = dataset_for_code or dataset_for_contract

    # ── 라우팅 헬퍼 ─────────────────────────────────────────────────────────────
    def _is_fut(self, symbol) -> bool:
        return self._futures is not None and qc.is_futures(symbol)

    def _broker(self, symbol):
        if self._is_fut(symbol):
            return self._futures
        if self._stock is None:
            raise RuntimeError(
                f"주식 자격증명 미등록 — 주식 심볼 {symbol} 거래 불가(커버리지 게이트가 차단해야 함)")
        return self._stock

    def _code(self, symbol) -> str:
        """선물은 데이터셋 심볼→라이브 계약코드 해석. 주식은 심볼 그대로.

        해석 실패(마스터 미수신 등)면 RuntimeError → 호출부(Trader)가 발주 skip(추측 발주 금지)."""
        if not self._is_fut(symbol):
            return symbol
        code = self._resolve(symbol)
        if not code:
            raise RuntimeError(f"선물 계약코드 해석 실패(마스터 미수신/만기 등): {symbol} — 발주 skip")
        return code

    def _order_fn(self, symbol, base: str):
        """선물 주문 메서드를 market별로 — CME(해외선물)면 overseas_*(해외 컨텍스트)로 dispatch.

        국내선물(KRX)·주식은 base 메서드 그대로. 해외선물만 overseas_buy/overseas_sell/
        overseas_buy_limit/overseas_sell_limit(P2 구현)로 라우팅(국내 엔드포인트 오발주 방지)."""
        broker = self._broker(symbol)
        if self._is_fut(symbol) and futures_market(symbol) == "CME":
            return getattr(broker, "overseas_" + base)
        return getattr(broker, base)

    # ── Broker Protocol: 심볼 기반 거래 메서드(라우팅) ────────────────────────────
    def buy(self, symbol, qty):
        return self._order_fn(symbol, "buy")(self._code(symbol), qty)

    def sell(self, symbol, qty):
        return self._order_fn(symbol, "sell")(self._code(symbol), qty)

    def buy_limit(self, symbol, qty, limit_price):
        return self._order_fn(symbol, "buy_limit")(self._code(symbol), qty, limit_price)

    def sell_limit(self, symbol, qty, limit_price):
        return self._order_fn(symbol, "sell_limit")(self._code(symbol), qty, limit_price)

    def buy_resv_limit(self, symbol, qty, limit_price):
        return self._broker(symbol).buy_resv_limit(self._code(symbol), qty, limit_price)

    def sell_resv_limit(self, symbol, qty, limit_price):
        return self._broker(symbol).sell_resv_limit(self._code(symbol), qty, limit_price)

    def price(self, symbol):
        # G2: 해외선물(CME) 실시간 시세는 **사용하지 않고 0 반환** → 호출자가 dataset(yfinance,
        # 정확 스케일)로 fallback하거나(close-cycle _safe_price) skip(catch-up 손절 cur<=0 가드).
        # KIS overseas_price(HHDFC55010000)는 ① CME 실시간시세 *유료구독* 필수(미구독 시 EGW00553
        # 거부) ② sCalcDesz 미적용 raw(GC 192250 vs 실제 1922.5)라, 그 값을 손절 평가에 넣으면
        # 거짓 take-profit/stop 트리거(catch-up on_tick)가 난다. 유료 실시간피드 + scalc_desz
        # 스케일 배선은 해외선물 라이브 활성화 단계(M10)에서 함께. 자동매매 권위 시세=dataset.
        if self._is_fut(symbol) and futures_market(symbol) == "CME":
            return 0.0
        return self._broker(symbol).price(self._code(symbol))

    def today_open(self, symbol):
        # G2: price()와 동형 — 해외선물(CME) 시세는 미스케일·유료(EGW00553)이고, futures 브로커의
        # today_open은 국내 시세 TR(FHMIF10000000)이라 CME 코드로 호출하면 오라우팅된다. 0 반환 →
        # catch-up 매수가 호출자(trader open_price<=0 가드)가 prev_close(dataset) ref로 fallback.
        if self._is_fut(symbol) and futures_market(symbol) == "CME":
            return 0.0
        return self._broker(symbol).today_open(self._code(symbol))

    def orderable_qty(self, symbol, side, price):
        """국내 선물 신규 주문가능 계약수(모델 A 라이브 사이징 기준값). 국내 선물만 — 그 외 None.

        주식·해외선물(CME)·선물 미구성·브로커 미지원·해석/조회 실패는 모두 None을 반환해
        호출자(Trader)가 카탈로그 추정으로 안전 강등하게 한다. 성공 시 브로커 정수(0 포함).
        0(증거금 부족)과 None(조회 불가)을 구분 — Trader가 전자는 발주 억제, 후자는 degrade.
        """
        if not self._is_fut(symbol):
            return None                                   # 주식/선물 미구성(_is_fut가 둘 다 커버)
        if futures_market(symbol) == "CME":
            return None                                   # 해외선물 orderable은 Phase 2(별도 overseas 경로)
        fn = getattr(self._futures, "orderable_qty", None)
        if fn is None:
            return None                                   # 브로커 미지원 → degrade
        try:
            return fn(self._code(symbol), side, price)    # 계약코드 해석 후 위임(0 포함 정수 그대로)
        except Exception as e:                            # noqa: BLE001 — _code 해석 실패·broker 조회 실패
            # ⚠ 주문 경로(buy/sell)는 _code raise를 그대로 전파해 발주를 skip하지만, orderable_qty는
            # 사이징 보조 조회라 해석/조회 실패가 발주를 막으면 안 됨 → catch→None(degrade)으로 의도적 차이.
            log.warning("선물 주문가능수량 조회 실패 [%s %s] — 카탈로그 강등: %s", symbol, side, e)
            return None

    def cancel(self, order_no, symbol, qty):
        # 해외선물 취소(OTFM3003U)는 원주문일자(ORGN_ORD_DT)가 필수인데 이 시그니처엔 없다.
        # 국내 취소 메서드로 잘못 라우팅하면 다른 계좌를 건드리므로 명시 차단(취소는 Trader
        # 핫패스 아님 — KIS DAY 자동취소). 라이브 배선은 ORD_DT 보관 후 overseas_cancel 직접 호출(M10).
        if self._is_fut(symbol) and futures_market(symbol) == "CME":
            raise NotImplementedError(
                "해외선물 취소는 원주문일자(ORGN_ORD_DT)가 필요 — broker.overseas_cancel 직접 호출. "
                "(라우터 취소는 ORD_DT 미보유; M10 라이브 배선에서 주문 ORD_DT 추적 후 연결)")
        return self._broker(symbol).cancel(order_no, self._code(symbol), qty)

    def order_status(self, order_no, symbol=None, hint=None):
        # 선물 order_status는 1-arg(order_no), 주식은 2-arg(order_no, symbol).
        # 해외선물(CME)은 overseas_order_status(OTFM3116R inquire-ccld)로 분기.
        # hint(미국주식 예약주문 매칭 보조)는 주식 브로커로만 전달 — 선물은 무관.
        if symbol is not None and self._is_fut(symbol):
            if futures_market(symbol) == "CME":
                return self._futures.overseas_order_status(order_no)
            return self._futures.order_status(order_no)
        base = self._stock if self._stock is not None else self._futures
        if hint is None:
            # 레거시 2-인자 호출 보존 — hint 미지원 구현(테스트 더블 등) 호환.
            return base.order_status(order_no, symbol)
        return base.order_status(order_no, symbol, hint=hint)

    # ── 잔고 스냅샷: stock + 선물(국내·해외) 병합 + 심볼 정규화 (M7) ──────────────────
    def account_snapshot(self, overseas=True):
        """주식 스냅샷에 선물 포지션을 병합. 선물 계약코드→데이터셋 심볼로 정규화.

        선물 포지션을 ledger·reconcile·over-sell 클램프(전부 데이터셋 심볼 기준)가 인식하도록
        병합한다. 선물 브로커가 국내(account_snapshot)·해외(overseas_account_snapshot) 컨텍스트별
        잔고를 주면 둘 다 병합(미구성 컨텍스트는 try/except skip). **주식 동작 무변경**(선물 없으면
        stock 스냅샷 그대로). equity 정밀 병합(증거금/평가→_unified_equity_krw)은 라이브 검증 후."""
        snap = (self._stock.account_snapshot(overseas)
                if self._stock is not None else {"balance": {}, "positions": []})
        if self._futures is None:
            return snap
        out = {"balance": dict(snap.get("balance", {}) or {}),
               "positions": list(snap.get("positions", []) or [])}
        fetch_failed = list(out["balance"].get("fetch_failed") or [])
        for getter, cfg_attr, marker, mkt in (
                ("account_snapshot", "domestic_configured", "futures", "kr"),
                ("overseas_account_snapshot", "overseas_configured", "futures_overseas", "us")):
            fn = getattr(self._futures, getter, None)
            if fn is None:
                continue
            # 미구성 컨텍스트는 조용히 skip(실패 아님). 구성 여부 속성이 없는 구현
            # (테스트 더블 등)은 종전대로 호출한다.
            if not getattr(self._futures, cfg_attr, True):
                continue
            try:
                fsnap = fn() or {}
            except Exception as e:                   # noqa: BLE001 — 통신 실패는 병합 skip + 표식
                log.warning("선물 잔고 조회 실패(%s) — 병합 skip: %s", getter, e)
                # ★ε: 구성된 계좌의 조회 실패 — 부분 equity 표식. 킬스위치·day_start·
                # equity 시계열이 이 표식으로 평가를 보류한다(거짓 -98% 청산 차단).
                fetch_failed.append(marker)
                continue
            # 선물계좌 equity를 통합자산에 합산 → kill-switch·drawdown이 선물 PnL 인지(미배선 시
            # 완전 무시). 국내=추정예탁자산(prsm_dpast_amt, KRW)·해외=총자산평가(OTFM1411R
            # fm_tot_asst_evlu_amt, CRCY_CD=TKR로 KRW환산) — **둘 다 KRW라 FX 추측 없이 직접 합산**
            # (G3: 종전 해외 account 미노출로 kill-switch가 해외선물 손익 무시하던 결함 닫음).
            fut_acct = fsnap.get("account") or {}
            fut_eq = fut_acct.get("equity")
            if fut_eq:
                out["balance"]["futures_eval_krw"] = (
                    float(out["balance"].get("futures_eval_krw", 0) or 0) + float(fut_eq))
            # 선물 주문가능 증거금현금 — 시장별 분리 키로 기록(trader가 시장에 맞는 예산만 사용).
            # 주식 현금(balance["cash"])과 분리: 선물 주문은 선물계좌 증거금으로 체결.
            # 국내(kr)·해외(us) 별도 키 → 단일 시장 주문이 상대방 계좌 예산을 쓰는
            # 다계좌 과대사이징(C4)을 차단한다.
            fut_cash = fut_acct.get("order_cash")
            if fut_cash:
                out["balance"][f"futures_order_cash_{mkt}"] = float(fut_cash)
            for p in (fsnap.get("positions") or []):
                np = dict(p)
                code = str(p.get("symbol", "") or "")
                ds = self._d4c(code)
                if ds:                               # 계약코드 → 데이터셋 심볼(ledger 매칭 키)
                    np["contract_code"] = code
                    np["symbol"] = ds
                else:
                    # 정규화 실패는 조용히 지나가면 안 된다 — 원장(상품명)과 매칭이 깨져
                    # reconcile이 자기 포지션을 "외부 매도"로 오판·삭제하는 사고 부류(2026-07
                    # 분기 인시던트, LS 잔고 KRX형 코드 미인식)가 수 주간 은닉됐다. 표식은
                    # reconcile fail-safe(파괴적 정정 차단)의 신호이고, 원시 코드는 symbol로
                    # 유지해 웹 표시·external 집계는 계속된다.
                    np["symbol_unmapped"] = True
                    log.error("선물 포지션 심볼 정규화 실패 — 계약코드 %r 미등록 형식. "
                              "원장 매칭 불가 → reconcile 자동 정정 차단(표면화만)", code)
                np.setdefault("asset_class", "futures")
                out["positions"].append(np)
        if fetch_failed:
            out["balance"]["fetch_failed"] = fetch_failed
        return out

    # ── 진입 시 계약코드·만기일 (M6 만기 자동청산 — Trader가 ledger에 기록) ──────────
    def contract_expiry(self, symbol):
        """선물 심볼 → (라이브 계약코드, 만기일date). 비선물·미주입·해석실패 → (None, None).

        실패해도 (None,None)만 반환 — 진입 자체를 막지 않는다(거래는 _code 경로가 별도 검증).
        만기 미해석이면 그 포지션의 만기 백스톱이 비활성될 뿐(Trader가 경고 표면화)."""
        if self._resolve_expiry is None or not self._is_fut(symbol):
            return None, None
        try:
            return self._resolve_expiry(symbol)
        except Exception:                            # noqa: BLE001 — 해석 실패는 미기록(발주 비차단)
            return None, None

    # ── 그 외(주식 전용·잔고·여력 등)는 stock으로 위임 ────────────────────────────
    def pending_orders(self) -> list[dict]:
        """주식+선물 미체결 병합 (R2-③/R6-④ — 종전 __getattr__ 위임은 stock만).

        선물 미체결이 §19 A1 자기주문 제외와 broker_pending 관측(스냅샷)에 안
        보이던 맹점을 닫는다. 선물 심볼은 계약코드→데이터셋 심볼로 정규화해
        account_snapshot 포지션 병합과 같은 규약을 따른다(A1 키 매칭). 한쪽
        조회 실패는 다른 쪽 유지 — 각 브로커 내부의 국내/해외 병합 패턴과 동일.
        """
        out: list[dict] = []
        if self._stock is not None:
            try:
                out.extend(self._stock.pending_orders() or [])
            except Exception as e:
                log.warning("pending 병합 — 주식측 실패(선물측 유지): %s", e)
        if self._futures is not None and hasattr(self._futures, "pending_orders"):
            try:
                for r in self._futures.pending_orders() or []:
                    sym = str(r.get("symbol") or "")
                    ds = self._d4c(sym) if sym else None
                    if ds:
                        r["symbol"] = ds
                    out.append(r)
            except Exception as e:
                log.warning("pending 병합 — 선물측 실패(주식측 유지): %s", e)
        return out

    def __getattr__(self, name):
        # _stock/_futures/_resolve 등 내부 속성은 정상 조회(무한재귀 방지).
        if name.startswith("_"):
            raise AttributeError(name)
        target = self._stock if self._stock is not None else self._futures
        return getattr(target, name)
