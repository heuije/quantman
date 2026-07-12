"""체결 정책 글로벌 default + 병합 헬퍼.

ExecutionPolicy의 각 필드가 None이면 이 default로 채워진다.
백테스트·모의투자·실전이 모두 같은 default를 공유한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── 글로벌 default ─────────────────────────────────────────────────────────────

DEFAULT_EXECUTION: dict[str, Any] = {
    # 발주 방식은 시장이 결정(시스템): 국내(주식·선물)=시장가 단일(동시호가 단일가
    # 체결, 지정가 대비 슬리피지 손해 없음). 미국주식=지정가(KIS가 미국 연속장
    # 시장가 미지원). 아래 tolerance는 **미국 지정가 버퍼 전용**(국내는 시장가라 미사용).
    # = 라이브가 백테스트의 시가/종가 체결을 재현하기 위한 시장가-근사 버퍼. 미국 갭이
    # 커서 default ±3%(과거 1%/2%는 갭에 미체결 유발). 전략 execution으로 유저 override 가능.
    # 매수: 신선한 현재가 × (1 + tol%) 까지 허용. 그 이상 갭상승 시 미체결.
    "buy_tolerance_pct": 3.0,
    # 매도: 신선한 현재가 × (1 − tol%) 까지 허용(미체결=오버나이트 carry 회피).
    "sell_tolerance_pct": 3.0,
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
    # 단일 종목 비중 상한 (자본 대비 %). None이면 한도 없음(OFF).
    # 모든 사이징 결과가 이 한도 초과 시 클램프 — 한도 설정된 경우만.
    "max_position_pct": None,

    # 누적 손실 한도 (자본 고점 대비 %). None이면 한도 없음(OFF).
    # 도달 시 신규 진입 차단 + 알림. (일일 손실 한도는 제거됨 — 실시간 현재가 매도로 대체.)
    "max_drawdown_pct": None,

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


# ── 상품 계약명세 카탈로그 (백테스트 회계의 단일 출처) ─────────────────────────
# 백테스트가 "현금주식 vs 선물" 회계를 분기하는 근거. IR 스키마엔 이 정보를 넣지 않는다
# — 승수·증거금·만기는 *상품의 사실*이지 전략 선택이 아니므로 심볼로 조회한다.
#   equity(현금주식·ETF·지수): multiplier=1·margin=1·만기없음 → 기존 현금모델 그대로.
#   futures: 손익=ΔP×multiplier×계약수, 자본=증거금(notional×init_margin_rate), 만기마다 롤.
# ⚠ 통합 메모: server/app/futures_config.py(선물분석 대시보드)도 tick·multiplier를 따로 들고
#   있다(레거시 oil_futures 경로). core는 server를 import 못 하므로 여기를 단일 출처로 삼고,
#   추후 futures_config가 이 카탈로그를 읽도록 통합한다(중복 드리프트 제거).

# 원/달러 환율 dataset 키 — data_fetcher.MACRO_FRED_SYMBOLS["원달러환율"](FRED DEXKOUS)과
# 같은 리터럴(core/tests/test_sizing_fx.py가 동일성 고정). fixed_amount(₩정액)를 USD 상품
# 예산으로 환산할 때 백테스트(_budget)·라이브(event_buy_qty)가 이 시계열 하나만 본다
# (데이터포인트당 소스 1개 — 브로커 환율 등 다른 소스 fallback 금지).
FX_USDKRW_SYMBOL = "원달러환율"


@dataclass(frozen=True)
class InstrumentSpec:
    """상품 계약명세. equity면 multiplier=1·margin=1·만기없음.

    엔진 소비(현재): asset_class·multiplier·tick·currency·init_margin_rate.
    default_roll = 만기물 패널 보유 선물의 연속물 기본 롤(S4/E2 — 데이터 계층·엔진이 소비).
    예약(미소비): maint_margin_rate(E1b 변동증거금) · expiry_rule(만기는 패널 마지막 존재일로
    데이터 주도 도출 → 달력 규칙 미사용). *검증된 계약 사실*이라 보관한다.
    """
    asset_class: str          # [소비] "equity" | "futures"
    multiplier: float         # [소비] point value: 선물 1pt = multiplier 통화단위. equity=1.0
    tick: float               # [소비] 최소 호가단위 — round_to_tick(tick=)로 선물 체결가 라운딩. equity=0.0(주식은 tick_size 표)
    currency: str             # [소비] "KRW" | "USD"
    init_margin_rate: float   # [소비] 개시증거금률(notional 대비). equity=1.0(전액)
    maint_margin_rate: float  # [예약·E1b] 유지증거금률. 현재 엔진은 SimSpec.maintenance_margin_pct(사용자값) 사용
    expiry_rule: str          # [예약] 만기 캘린더 키 — 국내선물은 패널 마지막 존재일로 대체(미사용). equity=""
    default_roll: str         # [소비·S4/E2] 연속물 기본 롤 "at_expiry"|"days_before:N"|"volume_cross"|"oi_cross". equity=""


# 거래소 표준 승수·틱(server/app/futures_config.py와 정렬). 증거금률·만기·롤은 본 카탈로그 신규.
# 키 = data_fetcher 캐시/dataset 심볼 키. (선물은 한글 상품명, 주식은 종목코드)
_INSTRUMENTS: dict[str, InstrumentSpec] = {
    # 실 KOSPI200 선물 연속 일봉(지수포인트) — KRX 공식 API fut_bydd_trd 만기물 패널(2010+)에서
    # default_roll(at_expiry)로 "코스피200선물" 연속물 서빙뷰를 파생(S4). ETF(261220)는
    # "코스피200선물ETF"로 분리 — 이 키엔 ETF 안 들어와 승수 충돌(F0 equity 후퇴) 해소.
    # 증거금률 = KRX 위탁증거금 공표값(myasset margin_rate.pdf 2026.7.6 정기변경 — 2026.6.1
    # 19.5/13.0 실측치(LS CFOAQ10100 교차)에서 KRX 정기조정으로 이동): KR 지수선물 3종 공통
    # 개시 19.8%·유지 13.2%. ⚠ KRX가 변동성 따라 주기적 조정 → 카탈로그는 대표값.
    # 라이브는 모델 A(브로커 주문가능수량)가 실시간 반영.
    "코스피200선물":  InstrumentSpec("futures", 250_000.0, 0.05, "KRW", 0.198, 0.132, "kospi200_2nd_thu", "at_expiry"),
    "미니코스피200선물": InstrumentSpec("futures",  50_000.0, 0.05, "KRW", 0.198, 0.132, "kospi200_2nd_thu", "at_expiry"),
    # 코스닥150선물 — 승수 1만원/pt·틱 0.10pt(KRX 상품스펙·수집 패널 호가 0.1 granularity 실측).
    # 이력=상장 2015-11-23~(만기물 패널). 라이브 발주 배선은 별도(로컬앱 미매핑 — 데이터/백테스트 전용).
    "코스닥150선물":  InstrumentSpec("futures",  10_000.0, 0.10, "KRW", 0.198, 0.132, "kosdaq150_2nd_thu", "at_expiry"),
    "원유선물":      InstrumentSpec("futures",   1_000.0, 0.01,  "USD", 0.10, 0.08,  "cme_cl",  "days_before:5"),
    "천연가스선물":   InstrumentSpec("futures",  10_000.0, 0.001, "USD", 0.10, 0.08,  "cme_ng",  "days_before:5"),
    "금선물":        InstrumentSpec("futures",     100.0, 0.10,  "USD", 0.08, 0.06,  "cme_gc",  "days_before:5"),
    "은선물(COMEX)":  InstrumentSpec("futures",   5_000.0, 0.005, "USD", 0.10, 0.08,  "cme_si",  "days_before:5"),
    "나스닥선물":     InstrumentSpec("futures",      20.0, 0.25,  "USD", 0.05, 0.04,  "cme_nq",  "volume_cross"),
    "비트코인선물":   InstrumentSpec("futures",       5.0, 5.0,   "USD", 0.50, 0.40,  "cme_btc", "volume_cross"),
}


def instrument_spec(symbol: str) -> InstrumentSpec:
    """심볼 → 계약명세. 미등록(현금주식·ETF·지수)이면 equity 기본.

    엔진은 반환의 asset_class로 현금/선물 회계를 분기한다. equity 통화는 KR 숫자코드=KRW,
    그 외=USD 휴리스틱(엔진 곳곳의 sym.isdigit() 추정을 한 곳으로 모음)."""
    spec = _INSTRUMENTS.get(symbol)
    if spec is not None:
        return spec
    return InstrumentSpec("equity", 1.0, 0.0,
                          "KRW" if symbol.isdigit() else "USD", 1.0, 1.0, "", "")


def is_futures(symbol: str) -> bool:
    """심볼이 선물 상품인지(카탈로그 등록 여부)."""
    return instrument_spec(symbol).asset_class == "futures"


def instrument_region(symbol: str) -> str:
    """거래 지역(세션 그룹) — 'KRX'(국내) | 'US'(해외).

    국내주식·국내선물=KRX, 해외주식·해외선물=US. instrument_spec.currency(KRW→KRX,
    USD→US)로 판별 — 우리 유니버스에서 통화↔거래지역이 1:1(KRW 상품은 KRX, USD 상품은
    US/CME 세션)이라 currency가 곧 세션 그룹의 SSOT다. sym.isdigit() 류의 임시 판별이
    한글 선물 심볼('코스피200선물')을 비-KR로 오분류하던 부류 버그를 한 곳으로 모은다."""
    return "KRX" if instrument_spec(symbol).currency == "KRW" else "US"


def instrument_category(symbol: str) -> str:
    """4분류 — 'kr_equity' | 'kr_futures' | 'us_equity' | 'us_futures'.

    asset_class(equity/futures) × region(KRW/USD)의 곱. 국장/미장 2분류로는 못 가르는
    국내주식·국내선물·해외주식·해외선물을 정확히 구분(표시·집계용 SSOT)."""
    sp = instrument_spec(symbol)
    region = "kr" if sp.currency == "KRW" else "us"
    return f"{region}_{sp.asset_class}"


def margin_rate(symbol: str) -> float:
    """레버리지 백테스트용 개시증거금률(0~1). 선물=부분증거금, 그 외=1.0(전액).

    계약 카탈로그(instrument_spec)가 단일 출처 — 종목별 증거금률을 그대로 반영.
    거래소 선물은 노티오널의 일부만 증거금으로 묶고 carry는 연속선물 가격에 이미 반영되어
    레버리지 펀딩(현금 차입이자)을 부과하지 않는다(engine.py 펀딩 항)."""
    return instrument_spec(symbol).init_margin_rate


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
                  currency: str = "KRW", tick: float = 0.0) -> float:
    """호가단위로 라운딩. direction: up | down | nearest.

    tick>0: 명시 호가단위(선물 계약 틱 — InstrumentSpec.tick)로 라운딩(통화 무관, float 유지).
            주식은 tick=0(기본) → 아래 통화별 표를 그대로 사용(완전 무영향).
    KRW: KIS 국내 호가단위(가격대별), 정수 반환.
    USD: 미국 NMS 기본 $0.01 (1달러 이상). 소수 2자리 float 반환.
    통화 미국이면 정수 절삭이 가격을 망가뜨리므로 반드시 float를 유지한다.
    """
    if price <= 0:
        return 0
    if tick and tick > 0:                 # 선물 등 명시 계약 틱 — 그 배수로 라운딩
        import math
        q = round(price / tick, 9)        # 부동소수 오차 흡수
        if direction == "up":
            n = math.ceil(q)
        elif direction == "down":
            n = math.floor(q)
        else:
            n = round(q)
        return round(n * tick, 10)
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
