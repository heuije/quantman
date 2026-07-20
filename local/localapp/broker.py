"""브로커 추상화.

Trader는 Broker 인터페이스(Protocol)에만 의존한다. 현재 구현체는 KisBroker 1개
(실전·모의투자 둘 다 base URL 차이로 처리). Phase 38.3에서 MockBroker 제거 —
KIS 자격증명 없는 자동 fallback이 사용자에게 "내가 모의투자 중인 줄 알았는데
사실 로컬 시뮬레이션이었다"는 신뢰 위험을 초래해서 명시적 에러로 전환.
"""

from __future__ import annotations

from typing import Protocol


class Broker(Protocol):
    def account_snapshot(self, overseas: bool = True) -> dict: ...
    def price(self, symbol: str) -> float: ...
    def today_open(self, symbol: str) -> float: ...   # catch-up용 — 당일 시가
    def buy(self, symbol: str, qty: int) -> dict: ...
    def sell(self, symbol: str, qty: int) -> dict: ...
    def buy_limit(self, symbol: str, qty: int, limit_price: int) -> dict: ...
    def sell_limit(self, symbol: str, qty: int, limit_price: int) -> dict: ...
    def buy_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict: ...   # 미국 예약 매수(지정가)
    def sell_resv_limit(self, symbol: str, qty: int, limit_price: float) -> dict: ...  # 미국 예약 매도(지정가)
    # partial: True면 잔량 중 qty만 취소(부분취소) — 동시호가 가드의 초과분 트리밍.
    # KIS 계열은 잔량전부 플래그(QTY_ALL_ORD_YN·RMN_QTY_YN)로 의사를 전송하고,
    # LS 계열은 취소수량 필드만 있어 인자를 받되 쓰지 않는다(구현 docstring 참조).
    def cancel(self, order_no: str, symbol: str, qty: int, *,
               partial: bool = False) -> dict: ...
    # hint: 해외(미국) 체결 매칭 보조({side,qty,reserved,submitted_ts,exclude_odnos}).
    # 예약주문 접수번호≠체결번호공간(실측 2026-06-11) 해소용 — 비해외 구현은 무시.
    def order_status(self, order_no: str, symbol: str | None = None,
                     hint: dict | None = None) -> dict: ...
    def pending_orders(self) -> list[dict]: ...
    # 종가창 급등/상한가 스캔 — 자동매매 템플릿 limit_up_close_v1의 장중 실시간 관측
    # (15:2x 마감 동시호가 중 예상체결가 랭킹). 반환 row:
    #   {symbol, name, price(예상체결가), change_pct(전일 대비 %), is_limit_up(예상체결가≥상한가),
    #    ask_rem(총 매도잔량 — 계측용)}
    # 시장(KOSPI/KOSDAQ) 필터는 호출자(runner)가 ticker_db로 수행한다 — 브로커 중립.
    # 미지원 구현은 NotImplementedError를 던진다(조용한 빈 리스트 금지 — 커버리지 표면화).
    def scan_close_surge(self, min_change_pct: float) -> list[dict]: ...
