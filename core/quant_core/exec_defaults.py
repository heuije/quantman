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
    # Q7: time-in-force = DAY (업계 표준). KIS가 정규장 마감(15:30) 시 미체결 주문을
    # 자동 cancel하므로 로컬에서 별도 timeout cancel 없음. 5분 timeout이 비표준적
    # 으로 짧다는 결론(2026-05-23 리뷰) — Alpaca/IB/Fidelity/KIS 모두 DAY 기본.
    # 일중 limit 도달 시 자연 체결을 허용 (이전 정책에선 폐기됐던 케이스).
    #
    # _wait_pending이 cycle 끝에 짧게 폴링하는 윈도우(시초가 동시호가 직후 체결을
    # 잡기 위함). 60초면 시초가 체결은 거의 다 잡힌다.
    "post_submit_wait_sec": 60,
    # 폴링 간격 (초). _resolve_pending이 N초마다 KIS order_status 조회.
    "poll_interval_sec": 20,

    # 갭 필터: 진입 시 전일 종가 vs 현재가 갭이 이 임계값 초과면 그 신호 폐기
    "gap_filter_pct": 2.5,

    # 사이징 모드 (Phase 47 — 4지 통합):
    #   "fixed_amount": 한 종목당 amount_krw 원 (정액)
    #   "pct_cash":     자본의 amount_pct % (정률, default)
    #   "equal_weight": 자본을 screener_limit 종목에 균등 분배
    #   "atr_risk":     트레이드당 atr_risk_pct% 위험, 손절폭 ATR×atr_mult
    # default를 atr_risk → pct_cash로 변경 (ATR은 진입 장벽이 큼).
    "sizing_mode": "pct_cash",
    # fixed_amount 모드: 한 종목당 원 단위 금액. 0이면 발주 차단.
    "amount_krw": 0,
    # atr_risk 모드: 자본의 X%만 1트레이드에 위험
    "atr_risk_pct": 1.0,
    # ATR × 이 배수 = 1주당 손절폭(원). 수량 = (자본×risk%) ÷ (ATR×mult)
    "atr_mult": 2.0,
    # 단일 종목 비중 상한 (자본 대비 %). 모든 사이징 결과가 이 한도 초과 시 클램프.
    "max_position_pct": 10.0,

    # 일일 손실 한도 (자본 대비 %). 도달 시 kill switch 발동.
    "daily_loss_limit_pct": 3.0,
    # 누적 손실 한도 (자본 고점 대비 %). 도달 시 신규 진입 차단 + 알림.
    "max_drawdown_pct": 20.0,

    # 백테스트 비용 가정 (C-01 — 한국 매도세를 commission과 분리; CM-02 — 주석 정정)
    #
    # 한국 시장의 비용 구조: 위탁수수료(편도, 매수·매도 모두) + 거래세(매도 단방향).
    # 이전 모델은 'commission'에 둘을 통합해 양방향 적용 → 매수에도 세금이 붙는
    # 잘못된 비용. 거래세는 별도 'sell_tax'로 분리하고 매도 시에만 적용.
    #
    # 코스피·코스닥 차등(코스피 0.23% / 코스닥 0.18%, 모두 농특세 포함)은 1단계엔
    # 보수적 단일값(코스피 기준 0.23%)으로 적용. 종목→시장 매핑은 후속 단계.
    "bt_commission_bps": 3,            # 편도 0.03% (KIS 위탁수수료, 매수·매도 모두)
    "bt_sell_tax_bps": 23,             # 매도 0.23% (거래세 + 농특세, 매도 단방향)
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
    """가격대별 호가단위 반환 (KRW)."""
    for upper, tick in _TICK_TABLE:
        if price < upper:
            return tick
    return 1_000


def round_to_tick(price: float, direction: str = "nearest",
                  currency: str = "KRW") -> float:
    """호가단위로 라운딩. direction: up | down | nearest.

    KRW: KIS 국내 호가단위(가격대별), 정수 반환.
    USD: 미국 NMS 기본 $0.01 (1달러 이상). 소수 2자리 float 반환.
    통화 미국이면 정수 절삭이 가격을 망가뜨리므로 반드시 float를 유지한다.
    """
    if price <= 0:
        return 0
    if currency == "USD":
        # $1 미만은 $0.0001 틱이나, S&P500 대형주는 모두 $1 이상 → $0.01 고정.
        import math
        c = round(price * 100, 6)         # 부동소수 오차 흡수
        if direction == "up":
            cents = math.ceil(c)
        elif direction == "down":
            cents = math.floor(c)
        else:
            cents = round(c)
        return round(cents / 100.0, 2)
    t = tick_size(price)
    if direction == "up":
        return int(((price + t - 1) // t) * t)
    if direction == "down":
        return int((price // t) * t)
    return int(round(price / t) * t)


# ── ±30% 가격제한폭 cap (KOSPI/KOSDAQ 일반 종목) ─────────────────────────────

KRW_DAILY_LIMIT_PCT = 30.0


def apply_daily_price_limit(price: float, prev_close: float, side: str,
                             currency: str = "KRW") -> float:
    """한국 주식 ±30% 가격제한폭 사전 클램프.

    KIS 서버가 거부하기 전에 클라이언트에서 미리 cap → API 거부 누적 방지.
    side='buy'  → 상한가(prev_close ×1.30) 위 limit는 상한가로.
    side='sell' → 하한가(prev_close ×0.70) 아래 limit는 하한가로.
    USD/그 외 통화는 무가공 (미국 NMS는 일일 한도 다름).

    예외 (이 함수가 못 잡는 케이스 → KIS 서버 거부에 fallback):
      - 신규 상장일 ±60% (KRX 규정)
      - 일부 ETF/ETN/레버리지 종목의 다른 한도
      - VI(변동성완화장치) 단일가
    """
    if currency != "KRW" or prev_close <= 0 or price <= 0:
        return price
    if side == "buy":
        ceiling = round_to_tick(prev_close * (1 + KRW_DAILY_LIMIT_PCT / 100.0),
                                direction="down", currency="KRW")
        return min(price, ceiling)
    if side == "sell":
        floor_p = round_to_tick(prev_close * (1 - KRW_DAILY_LIMIT_PCT / 100.0),
                                direction="up", currency="KRW")
        return max(price, floor_p)
    return price
