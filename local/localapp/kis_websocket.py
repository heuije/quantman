"""KIS WebSocket 실시간 시세 클라이언트.

장중 보유 종목의 가격 tick을 받아 IntradayStopManager의 on_tick 콜백 호출.
sync (websocket-client 1.x) 기반 + 별 thread에서 실행 — 메인 사이클을 막지 않음.

KIS WebSocket spec:
  • URL: 모의 ws://ops.koreainvestment.com:31000, 실전 ws://ops.koreainvestment.com:21000
  • approval_key: REST /oauth2/Approval에서 발급
  • 메시지: JSON header + body
  • 국내 주식 체결가 TR_ID: H0STCNT0 (실전), H0STCNT0 동일 (모의)
  • 동시 구독 최대 ~20개 (체결가+호가 합산)
  • tick payload는 PIPE+CARET 구분 raw text — 예:
    "0|H0STCNT0|001|005930^091000^73500^^^^..." (실시간 시세 데이터)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import websocket as ws_lib

log = logging.getLogger("localapp.kis_websocket")

TR_ID_PRICE_DOMESTIC = "H0STCNT0"   # 국내 주식 체결가 실시간
TR_ID_PRICE_OVERSEAS = "HDFSCNT0"   # 해외 주식 체결가
SUBSCRIBE_MAX = 20                  # KIS 동시 구독 한도 (체결가+호가 합산)

# 미국 시세 tr_key prefix — KIS 공식 spec ([해외주식] 실시간시세.xlsx HDFSCNT0).
#   • D + 시장구분(NAS/NYS/AMS) + 종목코드  → 무료시세 (미국 0분지연 = 사실상 실시간).
#     예: DNASAAPL = D + NAS(나스닥) + AAPL. 신청 불필요.
#   • R + 시장구분 + 종목코드  → 유료시세. KIS 포럼 FAQ "해외주식 유료시세 신청방법"
#     별도 신청 시에만 사용. 일반 사용자는 D-prefix만으로 충분.
_OVERSEAS_PREFIX = {"NAS": "DNAS", "NYS": "DNYS", "AMS": "DAMS"}


class KisWebSocket:
    """KIS WebSocket 시세 구독 클라이언트.

    사용:
        ws = KisWebSocket(broker, on_tick=lambda sym, price: ...)
        ws.start()
        ws.subscribe(["005930", "000660"])
        # ... 장중 ...
        ws.unsubscribe(["005930"])
        ws.stop()

    self._symbols는 현재 구독 중인 종목 셋. on_tick 콜백은 별 thread에서 호출됨.
    재연결: ping 응답 없거나 끊김 감지 시 자동 reconnect + 구독 복구.
    """

    def __init__(self, broker, on_tick: Callable[[str, float], None]):
        self.broker = broker
        self.on_tick = on_tick
        self._symbols: set[str] = set()
        # 해외 tick의 SYMB(예: BRK/B) → 구독 시 원본 심볼(예: BRK.B) 역매핑
        self._symb_to_orig: dict[str, str] = {}
        self._ws: ws_lib.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = False
        self._connected = threading.Event()
        self._approval_key: str | None = None
        self._lock = threading.Lock()

    # ── 메시지 builder ────────────────────────────────────────────────────────

    def _tr_for(self, symbol: str) -> tuple[str, str]:
        """심볼 → (tr_id, tr_key). 미국은 HDFSCNT0 + D-prefix+티커 (무료 실시간),
        그 외는 국내(H0STCNT0 + 코드).

        미국 D-prefix는 KIS 공식 spec상 0분지연 무료시세 = 사실상 실시간.
        별도 신청 불필요. 유료시세(R-prefix)는 거의 사용 안 함.
        """
        from . import market_index
        exch = market_index.exchange_of(symbol)
        if exch in _OVERSEAS_PREFIX:
            kt = market_index.kis_ticker_of(symbol)
            self._symb_to_orig[kt] = symbol      # 역매핑 (tick SYMB → 원본)
            return TR_ID_PRICE_OVERSEAS, _OVERSEAS_PREFIX[exch] + kt
        return TR_ID_PRICE_DOMESTIC, symbol

    def _sub_msg(self, symbol: str, sub: bool = True) -> str:
        tr_id, tr_key = self._tr_for(symbol)
        return json.dumps({
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if sub else "2",   # 1=등록, 2=해지
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        })

    # ── 콜백 ──────────────────────────────────────────────────────────────────

    def _on_open(self, ws):
        log.info("[ws] 연결됨")
        self._connected.set()
        # 기존 구독 복구
        with self._lock:
            for s in list(self._symbols):
                try:
                    ws.send(self._sub_msg(s, sub=True))
                except Exception as e:
                    log.warning("[ws] 구독 복구 실패 %s: %s", s, e)

    def _on_message(self, ws, message: str):
        # KIS WebSocket 메시지 두 종류:
        #  • JSON 응답 (구독 등록/해지 ack, ping/pong, 시스템)
        #  • PIPE 구분 raw text — 실시간 tick (예: "0|H0STCNT0|001|005930^091000^73500^...")
        if not message:
            return
        if message[0] in ("0", "1"):
            self._parse_tick(message)
        else:
            try:
                d = json.loads(message)
                hdr = d.get("header") or {}
                if hdr.get("tr_id") == "PINGPONG":
                    # 시스템 ping → pong 응답 필요 없음, websocket-client가 자동 처리
                    return
                body = d.get("body") or {}
                msg = body.get("msg1") or body.get("rt_cd")
                if msg:
                    log.info("[ws] ack: %s — %s", hdr.get("tr_id"), msg)
            except Exception:
                log.debug("[ws] non-tick non-json msg: %s", message[:100])

    def _parse_tick(self, message: str) -> None:
        """PIPE 구분 tick 메시지 파싱 → on_tick 콜백 호출.

        국내 H0STCNT0: "0|H0STCNT0|n|<코드>^<시간>^<현재가>^..." (field[0]=코드, [2]=현재가)
        해외 HDFSCNT0: "0|HDFSCNT0|n|<RSYM>^<SYMB>^<소수>^...^<현재가>^..."
          field[1]=SYMB(종목코드), field[11]=LAST(현재가). 지연시세 동일 레이아웃.
        """
        parts = message.split("|")
        if len(parts) < 4:
            return
        tr_id = parts[1]
        fields = parts[3].split("^")

        if tr_id == TR_ID_PRICE_DOMESTIC:
            if len(fields) < 3:
                return
            symbol = fields[0]
            price_str = fields[2]
        elif tr_id == TR_ID_PRICE_OVERSEAS:
            if len(fields) < 12:
                return
            symb = fields[1]
            symbol = self._symb_to_orig.get(symb, symb)  # 원본 심볼로 환원
            price_str = fields[11]
        else:
            return

        try:
            price = float(price_str)
        except (ValueError, IndexError):
            return
        if price <= 0:
            return
        try:
            self.on_tick(symbol, price)
        except Exception as e:
            log.exception("[ws] on_tick 콜백 오류 %s: %s", symbol, e)

    def _on_error(self, ws, error):
        log.warning("[ws] error: %s", error)

    def _on_close(self, ws, code, msg):
        log.info("[ws] 연결 종료 (code=%s, msg=%s)", code, msg)
        self._connected.clear()

    # ── 외부 API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """별 thread에서 WebSocket 시작. 끊기면 자동 재연결."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False
        try:
            self._approval_key = self.broker.get_approval_key()
        except Exception as e:
            log.error("[ws] approval_key 발급 실패: %s", e)
            raise

        def _runner():
            backoff = 1
            while not self._stop_flag:
                try:
                    self._ws = ws_lib.WebSocketApp(
                        self.broker.ws_url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                    )
                    self._ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    log.exception("[ws] run_forever 예외: %s", e)
                if self._stop_flag:
                    break
                log.info("[ws] %d초 후 재연결", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                # 재연결 직전 approval_key 갱신
                try:
                    self._approval_key = self.broker.get_approval_key()
                except Exception as e:
                    log.warning("[ws] approval_key 갱신 실패: %s", e)

        self._thread = threading.Thread(target=_runner, daemon=True,
                                          name="kis-websocket")
        self._thread.start()
        # connection 대기 (최대 10초)
        self._connected.wait(timeout=10)

    def stop(self) -> None:
        self._stop_flag = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def subscribe(self, symbols: list[str]) -> int:
        """추가 구독. 이미 구독 중인 종목은 skip. 한도(20) 초과는 거부.

        Returns: 실제로 추가된 종목 수.
        """
        added = 0
        with self._lock:
            for s in symbols:
                if not s or s in self._symbols:
                    continue
                if len(self._symbols) >= SUBSCRIBE_MAX:
                    log.warning("[ws] 구독 한도(%d) 초과 — %s skip",
                                SUBSCRIBE_MAX, s)
                    break
                if self._ws and self._connected.is_set():
                    try:
                        self._ws.send(self._sub_msg(s, sub=True))
                        self._symbols.add(s)
                        added += 1
                    except Exception as e:
                        log.warning("[ws] 구독 실패 %s: %s", s, e)
                else:
                    # 연결 전 — symbols만 기록, _on_open이 등록
                    self._symbols.add(s)
                    added += 1
        return added

    def unsubscribe(self, symbols: list[str]) -> int:
        removed = 0
        with self._lock:
            for s in symbols:
                if s not in self._symbols:
                    continue
                if self._ws and self._connected.is_set():
                    try:
                        self._ws.send(self._sub_msg(s, sub=False))
                    except Exception as e:
                        log.warning("[ws] 해지 실패 %s: %s", s, e)
                self._symbols.discard(s)
                removed += 1
        return removed

    def sync_subscriptions(self, target: set[str]) -> dict:
        """현재 구독 vs target 비교해 차이만 구독/해지. 보유 종목 변화 시 호출.

        Returns: {"added": N, "removed": M}
        """
        with self._lock:
            current = set(self._symbols)
        to_add = list(target - current)
        to_remove = list(current - target)
        a = self.subscribe(to_add) if to_add else 0
        r = self.unsubscribe(to_remove) if to_remove else 0
        return {"added": a, "removed": r,
                "subscribed_now": len(self._symbols)}

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()
