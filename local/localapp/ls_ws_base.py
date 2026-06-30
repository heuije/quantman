"""LS증권 WebSocket 공유 베이스 — 연결·재연결·JSON 라우팅·토큰 인증.

LsWebSocket(시세)·LsOrderWebSocket(체결통보)이 공유한다. LS WS는 KIS와 달리:
  • 단일 bare /websocket 엔드포인트 (모의 29443·실전 9443, tr_cd가 라우팅)
  • OAuth access token 헤더 재사용 (approval_key 발급 불필요)
  • 전부 평문 JSON (AES-CBC 복호화·pipe payload 불필요)

서브클래스 hook:
  _initial_subs(ws)            — 연결(open) 시 구독 메시지 전송
  _on_data(tr_cd, tr_key, body) — 데이터 메시지(체결·시세) 처리

spec: docs/ls-api/GOTCHAS G-WS1~3 (2026-06-25 모의 연결 프로브 실측).
"""
from __future__ import annotations

import json
import logging
import threading
import time

import websocket as ws_lib

log = logging.getLogger("localapp.ls_ws")

_WS_HOST = "openapi.ls-sec.co.kr"


class _LsWsBase:
    """LS WS 공유 플럼빙. 서브클래스가 _tag·_initial_subs·_on_data 정의."""

    _tag = "ls-ws"

    def __init__(self, broker):
        self.broker = broker
        self._ws: ws_lib.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = False
        self._connected = threading.Event()
        self._lock = threading.Lock()

    @property
    def _ws_url(self) -> str:
        # 모의=29443·실전=9443 (G-WS1 실측). 토큰의 실전/모의와 포트가 일치해야 함.
        port = 29443 if getattr(self.broker, "virtual", True) else 9443
        return f"wss://{_WS_HOST}:{port}/websocket"

    def _sub_msg(self, tr_cd: str, tr_key: str = "", sub: bool = True, *,
                token: str) -> str:
        """구독(tr_type=1)/해지(2) 메시지. token = 해당 TR 계좌의 OAuth access token.

        LS 토큰은 계좌별 발급이라 TR마다 그 계좌 토큰을 실어야 그 계좌 체결/시세를 받는다
        (주식 SC1·시세 = 주식 계좌 토큰 · 선물 C01 = 선물 계좌 토큰). 등록 메시지 헤더에
        토큰을 실으므로 한 연결로 계좌별 라우팅 가능(LS 공식 SC1/C01 spec)."""
        return json.dumps({
            "header": {"token": token, "tr_type": "1" if sub else "2"},
            "body": {"tr_cd": tr_cd, "tr_key": tr_key},
        })

    def _stock_broker(self):
        """주식 계좌 브로커 — BrokerRouter면 내부 _stock(주식 미설정이면 None→SC1 구독 skip),
        아니면 broker 자신.

        BrokerRouter.__getattr__가 언더스코어 속성(_token) 위임을 무한재귀 방지로 막아
        broker._token() 직접 호출이 AttributeError('_token')로 실패한다(2026-06-26 라이브
        '구독 등록 실패 SC1: _token' 근본). 라우터면 내부 _stock에서 토큰을 직접 얻는다.

        ⚠ 종전 'getattr(...,"_stock",None) or self.broker'는 **주식계좌 없는 라우터**
        (선물 전용 LS 계좌)에서 _stock=None → fallback이 router 자신을 반환 → router._token()이
        다시 AttributeError('_token')로 SC1 구독을 거짓 실패시켰다(2026-06-30 LS 선물 모의 재발).
        라우터(_stock 속성 보유)면 _stock을 그대로 반환(None이면 _sub_exec가 SC1 skip),
        라우터가 아닌 단일 브로커여야만 broker 자신을 쓴다."""
        if hasattr(self.broker, "_stock"):       # BrokerRouter — 주식 미설정이면 _stock=None
            return self.broker._stock            # None → _sub_exec가 SC1 구독 skip
        return self.broker                        # 라우터 아닌 단일 브로커

    def _futures_broker(self):
        """선물 계좌 브로커 — BrokerRouter면 내부 _futures, 아니면 None(선물 미설정)."""
        return getattr(self.broker, "_futures", None)

    # ── 콜백 ──────────────────────────────────────────────────────────────────

    def _on_open(self, ws) -> None:
        log.info("[%s] 연결됨", self._tag)
        self._connected.set()
        try:
            self._initial_subs(ws)
        except Exception as e:
            log.warning("[%s] 초기 구독 실패: %s", self._tag, e)

    def _on_message(self, ws, message: str) -> None:
        """LS WS 메시지: ack(rsp_cd 존재·body=null) 또는 데이터(header.tr_cd + body)."""
        if not message:
            return
        try:
            d = json.loads(message)
        except Exception:
            log.debug("[%s] non-json: %s", self._tag, str(message)[:120])
            return
        hdr = d.get("header") or {}
        rsp_cd = hdr.get("rsp_cd")
        if rsp_cd is not None:
            lvl = log.info if rsp_cd == "00000" else log.warning
            lvl("[%s] ack: tr=%s rsp_cd=%s %s", self._tag,
                hdr.get("tr_cd"), rsp_cd, hdr.get("rsp_msg", ""))
            return
        body = d.get("body")
        if isinstance(body, dict):
            try:
                self._on_data(hdr.get("tr_cd"), hdr.get("tr_key"), body)
            except Exception as e:
                log.exception("[%s] _on_data 오류: %s", self._tag, e)

    def _on_error(self, ws, error) -> None:
        log.warning("[%s] error: %s", self._tag, error)

    def _on_close(self, ws, code, msg) -> None:
        log.info("[%s] 연결 종료 (code=%s, msg=%s)", self._tag, code, msg)
        self._connected.clear()

    # ── 외부 API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """별 thread에서 WebSocket 시작. 끊기면 자동 재연결(토큰은 _sub_msg서 매회 갱신)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False

        def _runner():
            backoff = 1
            while not self._stop_flag:
                try:
                    self._ws = ws_lib.WebSocketApp(
                        self._ws_url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                    )
                    self._ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    log.exception("[%s] run_forever 예외: %s", self._tag, e)
                if self._stop_flag:
                    break
                log.info("[%s] %d초 후 재연결", self._tag, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        self._thread = threading.Thread(target=_runner, daemon=True, name=self._tag)
        self._thread.start()
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

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── 서브클래스 hooks ──────────────────────────────────────────────────────

    def _initial_subs(self, ws) -> None:
        raise NotImplementedError

    def _on_data(self, tr_cd, tr_key, body: dict) -> None:
        raise NotImplementedError
