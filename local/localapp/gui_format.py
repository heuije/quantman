"""로컬앱 GUI 표시용 순수 포맷 로직 — Tkinter 위젯과 분리해 단위 테스트 가능하게 둔다.

주문 상태 한글화·진입/청산 판정·주문 예정 창(시각) 계산 등 표시 전용 변환만 담는다.
트레이딩·발주 경로와 무관(읽기 전용)."""

from __future__ import annotations

# 주문 이벤트(orders.jsonl event) → 한글 라벨. 미지의 상태는 원문 유지(디버깅 가능).
_ORDER_STATUS_KO = {
    "submitted": "접수",
    "filled": "체결",
    "partial": "부분체결",
    "cancelled": "취소",
    "rejected": "거부",
    "timeout": "시간초과",
}

# 구버전 주문 기록(kind 필드 없음) 폴백 판정용 청산 사유 키워드. 진입 사유("매수신호"·
# "숏진입"·신호명)는 여기 안 걸려 진입으로 떨어진다.
_EXIT_REASON_HINTS = (
    "청산", "보유기간", "매도조건", "익절", "손절", "트레일",
    "환매", "롤오버", "만기", "킬스위치", "drawdown",
)


def order_status_ko(event: str) -> str:
    """주문 이벤트 상태를 한글로. 미지 상태는 원문 그대로(은폐 대신 노출)."""
    return _ORDER_STATUS_KO.get(str(event or ""), str(event or ""))


def order_kind_ko(order: dict) -> str:
    """주문의 진입/청산 판정. 신규 기록은 구조적 `kind`(발주 메서드가 명시), 구버전은 사유 폴백.

    반환: "진입" | "청산". side(매수/매도)만으론 숏 진입=매도·롱 청산=매도라 구분 불가하므로
    kind 필드가 1차 진실원천. kind 부재(구버전 orders.jsonl)면 사유 키워드로 추론(청산 키워드
    없으면 진입)."""
    k = order.get("kind")
    if k in ("진입", "청산"):
        return k
    reason = str(order.get("reason") or "")
    if any(h in reason for h in _EXIT_REASON_HINTS):
        return "청산"
    return "진입"


def order_window(fill: str, instrument_class: str) -> tuple[str, int, int]:
    """전략의 체결설정(fill)·상품클래스로 KRX 주문 발주 창 결정 → (창이름, 시, 분).

    fill=next_open → 시가창 08:55, fill=close/typical → 종가창(선물 15:40·주식 15:25).
    (미국은 동적 스케줄이라 호출자가 region으로 분기 — 이 함수는 KRX 고정 시각 전용.)"""
    if fill in ("close", "typical"):
        if instrument_class == "futures":
            return ("종가", 15, 40)
        return ("종가", 15, 25)
    return ("시가", 8, 55)
