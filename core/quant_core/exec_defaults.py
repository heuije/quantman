"""체결 정책 글로벌 default + 병합 헬퍼.

ExecutionPolicy의 각 필드가 None이면 이 default로 채워진다.
백테스트·모의투자·실전이 모두 같은 default를 공유한다.
"""

from __future__ import annotations

from typing import Any

# ── 글로벌 default ─────────────────────────────────────────────────────────────

DEFAULT_EXECUTION: dict[str, Any] = {
    # 주문 유형: 지정가 + tolerance (시장가는 시초가 갭에 무방비)
    "use_limit": True,
    # 매수: 어제 종가 × (1 + tol%) 까지 허용. 그 이상 갭상승 시 미체결 → 신호 폐기.
    "buy_tolerance_pct": 1.0,
    # 매도 (Phase 38.9 — sell/exit 통합): 어제 종가 × (1 − tol%) 까지 허용.
    # 신호 기반 매도(매도조건·보유기간)와 청산(익절·손절·트레일)이 같은 값을 사용.
    # 위험 관리는 잡혀야 하므로 매수 tol보다 공격적인 default.
    "sell_tolerance_pct": 2.0,
    # 미체결 주문 자동 취소 대기시간 (초). 5분.
    "unfilled_timeout_sec": 300,
    # 폴링 간격 (초)
    "poll_interval_sec": 20,

    # 갭 필터: 진입 시 전일 종가 vs 현재가 갭이 이 임계값 초과면 그 신호 폐기
    "gap_filter_pct": 2.5,

    # 사이징 모드: "pct_cash" (자본 비율) | "atr_risk" (ATR 변동성 보정)
    "sizing_mode": "atr_risk",
    # atr_risk 모드: 자본의 X%만 1트레이드에 위험
    "atr_risk_pct": 1.0,
    # ATR × 이 배수 = 1주당 손절폭(원). 수량 = (자본×risk%) ÷ (ATR×mult)
    "atr_mult": 2.0,
    # 단일 종목 비중 상한 (자본 대비 %). atr_risk 결과가 이 한도 초과 시 클램프.
    "max_position_pct": 10.0,

    # 일일 손실 한도 (자본 대비 %). 도달 시 kill switch 발동.
    "daily_loss_limit_pct": 3.0,
    # 누적 손실 한도 (자본 고점 대비 %). 도달 시 신규 진입 차단 + 알림.
    "max_drawdown_pct": 20.0,

    # 백테스트 비용 가정
    "bt_commission_bps": 25,           # 편도 0.25% (KIS 위탁수수료 + 거래세 평균)
    "bt_slippage_bps": 10,             # 편도 0.10% 기본 슬리피지
    "bt_gap_extra_cost": True,         # 갭일에 갭의 절반을 추가 비용으로 산입
    "bt_gap_threshold_pct": 1.0,       # 이 이상 갭이면 추가 비용 발생
}


def merged_execution(strategy_exec: dict | None) -> dict:
    """전략별 ExecutionPolicy를 글로벌 default와 병합. None 필드는 default로 채움.

    Phase 38.9 — 구버전 exit_tolerance_pct 키가 들어오면 sell_tolerance_pct로 흡수.
    """
    out = dict(DEFAULT_EXECUTION)
    if strategy_exec:
        # Legacy 키 변환
        if (strategy_exec.get("exit_tolerance_pct") is not None
                and strategy_exec.get("sell_tolerance_pct") is None):
            strategy_exec = dict(strategy_exec)
            strategy_exec["sell_tolerance_pct"] = strategy_exec.pop(
                "exit_tolerance_pct")
        for k, v in strategy_exec.items():
            if v is not None:
                out[k] = v
    # 옛 코드가 exit_tolerance_pct를 읽는 경우를 위해 alias 채워둠
    out["exit_tolerance_pct"] = out["sell_tolerance_pct"]
    return out


# ── KIS 호가 단위 (KOSPI/KOSDAQ 공통, 2023년 기준) ─────────────────────────────

_TICK_TABLE = [
    (2_000,    1),
    (5_000,    5),
    (20_000,   10),
    (50_000,   50),
    (200_000,  100),
    (500_000,  500),
    (float("inf"), 1_000),
]


def tick_size(price: float) -> int:
    """가격대별 호가단위 반환."""
    for upper, tick in _TICK_TABLE:
        if price < upper:
            return tick
    return 1_000


def round_to_tick(price: float, direction: str = "nearest") -> int:
    """KIS 호가단위로 라운딩. direction: up | down | nearest."""
    if price <= 0:
        return 0
    t = tick_size(price)
    if direction == "up":
        return int(((price + t - 1) // t) * t)
    if direction == "down":
        return int((price // t) * t)
    return int(round(price / t) * t)
