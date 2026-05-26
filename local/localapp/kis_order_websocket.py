"""KIS 체결 통보 WebSocket — 주문·체결·취소·거부 즉시 push 수신.

KIS H0STCNI0 (실전) / H0STCNI9 (모의):
  • tr_key = HTS ID (계좌번호 아님)
  • payload AES-CBC 암호화 — 구독 응답에 key/iv 함께 옴
  • 한 사용자 1개 구독만 (체결가는 종목별 20개와 별도 슬롯)
  • 26개 field, CARET 구분
  • CNTG_YN: 2=체결, 1=주문·정정·취소·거부 접수

핸들러는 ledger·trades·pending_orders를 즉시 갱신 + 서버 push.
시세 WebSocket(kis_websocket.py)과 같은 패턴, AES 복호화만 추가.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from base64 import b64decode
from typing import Callable

import websocket as ws_lib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

log = logging.getLogger("localapp.kis_order_ws")

# H0STCNI0 payload 26개 field (KIS 공식 spec)
EXEC_FIELDS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND", "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR",
    "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY",
    "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN", "FILLER", "CRDT_CLS",
    "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC",
]


def _aes_cbc_decrypt(key: str, iv: str, b64_cipher: str) -> str:
    """KIS 체결 통보 AES-CBC-base64 복호화 → UTF-8 평문 문자열.

    KIS가 구독 등록 응답에 key/iv를 16-byte ASCII string으로 보냄.
    cryptography 라이브러리 사용 (pycryptodome 의존성 회피).
    """
    cipher = Cipher(
        algorithms.AES(key.encode("utf-8")),
        modes.CBC(iv.encode("utf-8")),
    )
    decryptor = cipher.decryptor()
    raw = decryptor.update(b64decode(b64_cipher)) + decryptor.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    plain = unpadder.update(raw) + unpadder.finalize()
    return plain.decode("utf-8")


def parse_exec_payload(plain: str) -> list[dict]:
    """복호화된 평문 → 체결 통보 dict 리스트.

    한 메시지에 여러 체결이 들어올 수 있음 — RECORD 단위 분리 후 CARET fields.
    """
    out = []
    for record in plain.split("\n"):
        record = record.strip()
        if not record:
            continue
        fields = record.split("^")
        d = {EXEC_FIELDS[i]: fields[i] if i < len(fields) else ""
             for i in range(len(EXEC_FIELDS))}
        out.append(d)
    return out


class KisOrderWebSocket:
    """KIS 체결 통보 WebSocket 클라이언트.

    사용:
        ws = KisOrderWebSocket(broker, hts_id, on_exec=lambda evt: ...)
        ws.start()
        # ... 장중 ...
        ws.stop()

    on_exec 콜백 시그니처: (event: dict) — EXEC_FIELDS 파싱된 dict.
    """

    def __init__(self, broker, hts_id: str,
                 on_exec: Callable[[dict], None]):
        if not hts_id:
            raise ValueError("hts_id required - set via setup")
        self.broker = broker
        self.hts_id = hts_id
        self.on_exec = on_exec
        self._tr_id = "H0STCNI9" if broker.virtual else "H0STCNI0"
        self._ws: ws_lib.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = False
        self._connected = threading.Event()
        self._approval_key: str | None = None
        self._aes_key: str | None = None
        self._aes_iv: str | None = None
        self._lock = threading.Lock()

    def _sub_msg(self, sub: bool = True) -> str:
        return json.dumps({
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if sub else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": self._tr_id, "tr_key": self.hts_id}},
        })

    def _on_open(self, ws):
        log.info("[order-ws] 연결됨 — 체결통보 구독 등록 (HTS ID=%s)", self.hts_id)
        self._connected.set()
        try:
            ws.send(self._sub_msg(sub=True))
        except Exception as e:
            log.warning("[order-ws] 구독 등록 실패: %s", e)

    def _on_message(self, ws, message: str):
        if not message:
            return
        # KIS prefix "0"=평문, "1"=암호화, 그 외=JSON ack
        if message[0] in ("0", "1"):
            self._handle_exec_message(message)
        else:
            self._handle_system_message(message)

    def _handle_system_message(self, message: str):
        """JSON 시스템 메시지 — 구독 ack에서 AES key/iv 추출."""
        try:
            d = json.loads(message)
        except Exception:
            log.debug("[order-ws] non-json: %s", message[:100])
            return
        hdr = d.get("header") or {}
        if hdr.get("tr_id") == "PINGPONG":
            # KIS application-level PINGPONG — 받은 메시지를 그대로 echo. spec: wikidocs/164066.
            try:
                self._ws.send(message)
            except Exception as e:
                log.warning("[order-ws] PINGPONG echo 실패: %s", e)
            return
        body = d.get("body") or {}
        rt_cd = body.get("rt_cd")
        msg1 = body.get("msg1", "")
        if rt_cd is not None:
            log.info("[order-ws] ack: tr_id=%s rt_cd=%s msg=%s",
                      hdr.get("tr_id"), rt_cd, msg1)
        # output에 key/iv가 있음
        output = body.get("output") or {}
        if output.get("key") and output.get("iv"):
            self._aes_key = output["key"]
            self._aes_iv = output["iv"]
            log.info("[order-ws] AES key/iv 수신 완료")

    def _handle_exec_message(self, message: str):
        """실시간 체결 통보 — 암호화 복호화 + 파싱 + on_exec 콜백."""
        parts = message.split("|")
        if len(parts) < 4:
            return
        tr_id = parts[1]
        if tr_id not in ("H0STCNI0", "H0STCNI9"):
            return
        cipher_text = parts[3]
        if not self._aes_key or not self._aes_iv:
            log.warning("[order-ws] AES key 없음 — tick 폐기")
            return
        try:
            plain = _aes_cbc_decrypt(self._aes_key, self._aes_iv, cipher_text)
        except Exception as e:
            log.exception("[order-ws] 복호화 실패: %s", e)
            return
        events = parse_exec_payload(plain)
        for evt in events:
            try:
                self.on_exec(evt)
            except Exception as e:
                log.exception("[order-ws] on_exec 콜백 오류: %s", e)

    def _on_error(self, ws, error):
        log.warning("[order-ws] error: %s", error)

    def _on_close(self, ws, code, msg):
        log.info("[order-ws] 연결 종료 (code=%s, msg=%s)", code, msg)
        self._connected.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False
        try:
            self._approval_key = self.broker.get_approval_key()
        except Exception as e:
            log.error("[order-ws] approval_key 발급 실패: %s", e)
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
                    log.exception("[order-ws] run_forever 예외: %s", e)
                if self._stop_flag:
                    break
                log.info("[order-ws] %d초 후 재연결", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                try:
                    self._approval_key = self.broker.get_approval_key()
                except Exception as e:
                    log.warning("[order-ws] approval_key 갱신 실패: %s", e)

        self._thread = threading.Thread(target=_runner, daemon=True,
                                          name="kis-order-websocket")
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
