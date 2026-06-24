"""LS증권 실시간 시세 WebSocket — 보유 종목 가격 tick → IntradayStopManager.on_tick.

KisWebSocket 대칭(국내주식 한정). KIS도 시세 WS는 주식만 — 선물 stop-loss는 양 브로커
모두 REST 폴링이므로 여기서도 선물·미국은 skip(parity). 비KIS 활성 시 KIS 시세 WS가
skip돼 stop-loss가 REST 폴링 주기만큼 지연되던 것을 실시간 tick으로 보강한다.

국내주식 체결가 TR: S3_(KOSPI)·K3_(KOSDAQ). 런타임 KOSPI/KOSDAQ 분류기가 없어 종목당
**둘 다 구독**한다 — 해당 안 되는 시장은 tick이 오지 않아 무해하고(LS는 구독 자체는
정상처리, G-WS3 실측), on_data는 응답 body의 shcode로 종목을 식별하므로 어느 TR이
전달했든 정확하다. (US3 통합체결은 NXT 의존이라 모의 보장 불확실 → 표준 S3_/K3_ 채택.)

공유 플럼빙(연결·재연결·JSON 라우팅·토큰)은 _LsWsBase. 여기선 구독 종목·시세 파싱만.

spec: docs/ls-api/GOTCHAS G-WS1~3 (2026-06-25 모의 프로브 실측).
"""
from __future__ import annotations

import logging
from typing import Callable

from .ls_ws_base import _LsWsBase

log = logging.getLogger("localapp.ls_ws")

# 국내주식 체결가 TR — KOSPI·KOSDAQ. 종목당 둘 다 구독(분류기 부재·무해).
_STOCK_TRS = ("S3_", "K3_")


class LsWebSocket(_LsWsBase):
    """LS 실시간 시세 WebSocket 구독 클라이언트 (국내주식 체결가).

    사용:
        ws = LsWebSocket(broker, on_tick=lambda sym, price: ...)
        ws.start()
        ws.subscribe(["005930", "000660"])
        ws.sync_subscriptions({"000660"})   # 보유 변화 시
        ws.stop()
    """

    _tag = "ls-ws"

    def __init__(self, broker, on_tick: Callable[[str, float], None]):
        super().__init__(broker)
        self.on_tick = on_tick
        self._symbols: set[str] = set()

    @staticmethod
    def _is_domestic_stock(symbol: str) -> bool:
        """국내주식 코드(6자리 숫자)만 시세 WS 대상 — 선물·미국·기타는 skip.

        KRX 종목코드는 6자리 숫자(005930). 선물 데이터심볼('코스피200선물')·계약코드
        (A0169000)·미국 티커(AAPL)는 비6자리숫자라 자연히 제외(REST 폴링이 stop-loss 평가).
        """
        return bool(symbol) and symbol.isdigit() and len(symbol) == 6

    def _send_sub(self, ws, symbol: str, sub: bool = True) -> None:
        if not self._is_domestic_stock(symbol):
            return
        for tr in _STOCK_TRS:
            try:
                ws.send(self._sub_msg(tr, symbol, sub=sub))
            except Exception as e:
                log.warning("[ls-ws] 구독%s 실패 %s/%s: %s",
                            "" if sub else "해지", symbol, tr, e)

    def _initial_subs(self, ws) -> None:
        with self._lock:
            syms = list(self._symbols)
        for s in syms:
            self._send_sub(ws, s, sub=True)

    def _on_data(self, tr_cd, tr_key, body: dict) -> None:
        if tr_cd not in _STOCK_TRS:
            return
        try:
            price = float(body.get("price") or 0)
        except (ValueError, TypeError):
            return
        if price <= 0:
            return
        symbol = body.get("shcode") or tr_key
        if symbol:
            try:
                self.on_tick(symbol, price)
            except Exception as e:
                log.exception("[ls-ws] on_tick 콜백 오류 %s: %s", symbol, e)

    # ── 구독 관리 (KisWebSocket 대칭) ──────────────────────────────────────────

    def subscribe(self, symbols: list[str]) -> int:
        added = 0
        with self._lock:
            for s in symbols:
                if not s or s in self._symbols or not self._is_domestic_stock(s):
                    continue
                self._symbols.add(s)
                added += 1
                if self._ws and self._connected.is_set():
                    self._send_sub(self._ws, s, sub=True)
        return added

    def unsubscribe(self, symbols: list[str]) -> int:
        removed = 0
        with self._lock:
            for s in symbols:
                if s not in self._symbols:
                    continue
                if self._ws and self._connected.is_set():
                    self._send_sub(self._ws, s, sub=False)
                self._symbols.discard(s)
                removed += 1
        return removed

    def sync_subscriptions(self, target: set[str]) -> dict:
        """현재 구독 vs target 차이만 구독/해지. 보유 종목 변화 시 호출."""
        target = {s for s in target if self._is_domestic_stock(s)}
        with self._lock:
            current = set(self._symbols)
        a = self.subscribe(list(target - current))
        r = self.unsubscribe(list(current - target))
        return {"added": a, "removed": r, "subscribed_now": len(self._symbols)}
