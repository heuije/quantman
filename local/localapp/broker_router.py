"""주식·선물 브로커를 심볼로 라우팅하는 Broker 어댑터 (자동매매 #4 배선, M3).

Trader는 Broker Protocol에만 의존한다. 이 라우터가 그 Protocol을 충족하며, 심볼이 선물이면
KisFuturesBroker로, 아니면 KisBroker로 거래를 위임한다. 선물은 데이터셋 심볼(한글 상품명
"금선물"·"코스피200선물")을 라이브 계약코드(globex GCM26·국내 A01606)로 해석해 전달한다
(resolve 콜백 = futures_contracts.ContractResolver).

주식 전용 메서드(account_snapshot의 overseas 인자·buying_power_usd·pending_orders 등)는
__getattr__로 stock 브로커에 그대로 위임 — **주식 동작 완전 무변경**(라우터는 거래 메서드만
오버라이드). 선물 잔고 병합·reconcile은 M5(원장 선물화)에서. make_broker는 선물 자격증명이
없으면 라우터 없이 KisBroker를 그대로 반환하므로, 선물 미사용 환경은 영향이 전혀 없다.
"""
from __future__ import annotations

import quant_core as qc


class BrokerRouter:
    """stock(KisBroker) + futures(KisFuturesBroker)를 심볼로 라우팅. Broker Protocol 충족(duck)."""

    def __init__(self, stock, futures, *, resolve, resolve_expiry=None):
        self._stock = stock
        self._futures = futures
        self._resolve = resolve          # callable(symbol) -> 계약코드 | None
        # M6: callable(symbol) -> (계약코드, 만기일date) — 진입 시 ledger 기록(만기 자동청산).
        # 미주입(구 배선)이면 contract_expiry가 (None,None) → Trader는 만기 미기록.
        self._resolve_expiry = resolve_expiry

    # ── 라우팅 헬퍼 ─────────────────────────────────────────────────────────────
    def _is_fut(self, symbol) -> bool:
        return self._futures is not None and qc.is_futures(symbol)

    def _broker(self, symbol):
        return self._futures if self._is_fut(symbol) else self._stock

    def _code(self, symbol) -> str:
        """선물은 데이터셋 심볼→라이브 계약코드 해석. 주식은 심볼 그대로.

        해석 실패(마스터 미수신 등)면 RuntimeError → 호출부(Trader)가 발주 skip(추측 발주 금지)."""
        if not self._is_fut(symbol):
            return symbol
        code = self._resolve(symbol)
        if not code:
            raise RuntimeError(f"선물 계약코드 해석 실패(마스터 미수신/만기 등): {symbol} — 발주 skip")
        return code

    # ── Broker Protocol: 심볼 기반 거래 메서드(라우팅) ────────────────────────────
    def buy(self, symbol, qty):
        return self._broker(symbol).buy(self._code(symbol), qty)

    def sell(self, symbol, qty):
        return self._broker(symbol).sell(self._code(symbol), qty)

    def buy_limit(self, symbol, qty, limit_price):
        return self._broker(symbol).buy_limit(self._code(symbol), qty, limit_price)

    def sell_limit(self, symbol, qty, limit_price):
        return self._broker(symbol).sell_limit(self._code(symbol), qty, limit_price)

    def buy_resv_limit(self, symbol, qty, limit_price):
        return self._broker(symbol).buy_resv_limit(self._code(symbol), qty, limit_price)

    def sell_resv_moo(self, symbol, qty):
        return self._broker(symbol).sell_resv_moo(self._code(symbol), qty)

    def price(self, symbol):
        return self._broker(symbol).price(self._code(symbol))

    def today_open(self, symbol):
        return self._broker(symbol).today_open(self._code(symbol))

    def cancel(self, order_no, symbol, qty):
        return self._broker(symbol).cancel(order_no, self._code(symbol), qty)

    def order_status(self, order_no, symbol=None):
        # 선물 order_status는 1-arg(order_no), 주식은 2-arg(order_no, symbol).
        if symbol is not None and self._is_fut(symbol):
            return self._futures.order_status(order_no)
        return self._stock.order_status(order_no, symbol)

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
    def __getattr__(self, name):
        # _stock/_futures/_resolve 등 내부 속성은 정상 조회(무한재귀 방지).
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stock, name)
