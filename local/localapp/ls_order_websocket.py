"""LS증권 실시간 체결 통보 WebSocket — 주문 체결 즉시 push 수신.

KisOrderWebSocket 대칭. 체결 이벤트를 **KIS H0STCNI0와 동일한 evt dict 키**로
정규화해 intraday_loop._on_exec_event를 무수정으로 재사용한다(KIS 경로 byte-identical).

LS(비KIS) 활성 시 KIS 체결통보 WS는 skip되고 REST 폴백이 체결을 *사이클 경계에서만*
인지해 지연됐다(2026-06-24 실측: 주식 시가매수 체결이 종가청산까지 6.5h 미인지).
이 WS가 그 갭을 닫는다.

공유 플럼빙(연결·재연결·JSON 라우팅·토큰 인증)은 _LsWsBase. 여기선 구독 TR과
체결 이벤트 정규화만 정의한다.

LS WS spec — 모의 연결 프로브 실측 2026-06-25 (docs/ls-api/GOTCHAS G-WS1~3):
  구독 {"header":{"token","tr_type":"1"},"body":{"tr_cd":"SC1"|"C01","tr_key":""}}
  SC1 body: ordno·shtnIsuno(A+코드)·execqty·execprc·exectime·gubun
  C01 body: ordno·expcode·cheprice·chevol·chetime·dosugb
"""
from __future__ import annotations

import logging
from typing import Callable

from .ls_ws_base import _LsWsBase

log = logging.getLogger("localapp.ls_order_ws")

# 체결통보 TR: 주식 SC1 · 국내선물 C01. tr_key="" → 내 주문 전체.
# (KRX 한 시장 창에서 주식+선물 전략이 함께 도는 구조라 둘 다 구독해 양쪽 체결을 받는다.)
_EXEC_TRS = ("SC1", "C01")


def _norm_sc1(body: dict) -> dict:
    """주식주문체결(SC1) body → KIS H0STCNI0 evt 키로 정규화.

    SC1은 *체결* 이벤트(접수=SC0·거부=SC4 별도)라 CNTG_YN은 항상 '2'.
    _on_exec_event 매칭은 ODER_NO로 하므로 종목·사이드는 로깅용(매칭 비load).
    """
    return {
        "CNTG_YN": "2",
        "ODER_NO": str(body.get("ordno", "")),
        "STCK_SHRN_ISCD": str(body.get("shtnIsuno", "")).lstrip("A"),  # A005930 → 005930
        "CNTG_QTY": str(body.get("execqty", "0")),
        "CNTG_UNPR": str(body.get("execprc", "0")),
        "STCK_CNTG_HOUR": str(body.get("exectime", "")),
        # gubun "B"=매수 → KIS SELN_BYOV_CLS "02"매수/"01"매도
        "SELN_BYOV_CLS": "02" if str(body.get("gubun", "")).upper().startswith("B") else "01",
        "RFUS_YN": "N",
    }


def _norm_c01(body: dict) -> dict:
    """선물주문체결(C01) body → KIS H0STCNI0 evt 키로 정규화."""
    return {
        "CNTG_YN": "2",
        "ODER_NO": str(body.get("ordno", "")),
        "STCK_SHRN_ISCD": str(body.get("expcode", "")),
        "CNTG_QTY": str(body.get("chevol", "0")),
        "CNTG_UNPR": str(body.get("cheprice", "0")),
        "STCK_CNTG_HOUR": str(body.get("chetime", "")),
        "SELN_BYOV_CLS": str(body.get("dosugb", "")),   # 도수구분 — 로깅용
        "RFUS_YN": "N",
    }


_NORMALIZERS = {"SC1": _norm_sc1, "C01": _norm_c01}


class LsOrderWebSocket(_LsWsBase):
    """LS 실시간 체결 통보 WebSocket 클라이언트.

    사용:
        ws = LsOrderWebSocket(broker, on_exec=lambda evt: ...)
        ws.start()
        # ... 장중 ...
        ws.stop()

    on_exec 콜백 시그니처: (evt: dict) — KIS H0STCNI0 키로 정규화된 dict.
    """

    _tag = "ls-order-ws"

    def __init__(self, broker, on_exec: Callable[[dict], None], market: str = "KR"):
        super().__init__(broker)
        if market not in ("KR", "US"):
            raise ValueError(f"market must be 'KR' or 'US', got {market!r}")
        self.on_exec = on_exec
        self._market = market

    def _initial_subs(self, ws) -> None:
        for tr in _EXEC_TRS:
            try:
                ws.send(self._sub_msg(tr, sub=True))
            except Exception as e:
                log.warning("[ls-order-ws] 구독 등록 실패 %s: %s", tr, e)

    def _on_data(self, tr_cd, tr_key, body: dict) -> None:
        norm = _NORMALIZERS.get(tr_cd)
        if norm is None:
            log.debug("[ls-order-ws] 미처리 tr=%s", tr_cd)
            return
        self.on_exec(norm(body))
