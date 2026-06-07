"""DataSpec — 백테스트가 요구하는 데이터의 **단일 출처 정의**.

블록 카탈로그(catalog_spec)와 동형의 자기서술 레지스트리. 각 엔트리는 하나의
**피드(feed = 수급/cron 단위)**를 기술하며, "무엇을·얼마나 깊게·어떤 메타와 함께·
어떻게 가공해" 받아야 하는지를 선언한다. 이 정의로부터:
  - cron 설계 = source/adjustment/frequency/required_meta → 소스선정·가공 도출
  - 무결성 머신 = required_meta/adjustment/point_in_time/xs_completeness → 평가규칙
  - 문서/프론트 = data_spec() 직렬화 소비

의존성은 pydantic뿐 — 메타 정의 모듈이라 pandas/서버 DB에 의존하지 않는다.

용어:
  adjustment       = 건전한 백테스트가 **요구하는** 조정 수준(실측은 manifest가 기록,
                     게이트가 요구↔실측 비교). split_adjusted=분할조정, total_return=배당재투자.
  current_status   = 인벤토리 스냅샷(로드맵용): present/partial/absent.
  derivation       = sourced(외부수급) | computed(가격에서 자체산출, computed_from 참조).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PClass(str, Enum):
    PRICE = "P1"          # OHLCV
    DERIVED = "P2"        # 가격 파생 기술지표
    FUNDAMENTAL = "P3"    # 펀더멘털·추정치 (PIT 필수)
    MACRO = "P4"          # 매크로·시장 브로드캐스트
    STATIC = "P5"         # 분류·정적 메타 (섹터·통화·tick·상장일)
    CORPACTION = "P6"     # 코퍼레이트액션·지수 멤버십 이력


Frequency = Literal["daily", "intraday", "weekly", "monthly", "quarterly", "event", "static"]
Adjustment = Literal["raw", "split_adjusted", "total_return", "not_applicable"]
Derivation = Literal["sourced", "computed"]
Status = Literal["present", "partial", "absent"]

# 시계열 수급 피드가 manifest에 반드시 병기해야 하는 공통 메타 키.
_BASE_META = ["source", "fetched_at", "calendar", "coverage"]


class DataTypeSpec(BaseModel):
    """하나의 데이터 피드(수급/cron 단위)의 요구 정의."""

    key: str                              # 피드 식별자 — 예: "ohlcv.kr"
    pclass: PClass
    label: str
    frequency: Frequency
    # ── 깊이(depth) ──
    history_rule: str                     # 룩백 규약(사람이 읽는 규칙). 예: "백테스트 시작일 − 최대 window"
    point_in_time: bool = False           # as_of(발표/효력 시점) 병기 필수 — 미래누출 방지
    xs_completeness: bool = False         # 횡단 완전성 요구(그날 유니버스 전 종목 값 존재)
    adjustment: Adjustment = "not_applicable"   # 요구 조정 수준
    # ── 가공·출처 ──
    derivation: Derivation = "sourced"
    computed_from: list[str] = Field(default_factory=list)   # derivation=computed일 때 입력 피드 키
    source: Optional[str] = None          # 현재/권장 소스
    provides: list[str] = Field(default_factory=list)        # 제공 컬럼/필드(참조). 지표군은 INDICATOR_META 키
    required_meta: list[str] = Field(default_factory=list)   # manifest 필수 병기 메타 키
    # ── 연결·상태 ──
    downstream: list[str] = Field(default_factory=list)      # 의존 전략요소(M1.2가 정밀화)
    current_status: Status = "present"    # 인벤토리 스냅샷(로드맵용)
    notes: str = ""


REGISTRY: dict[str, DataTypeSpec] = {}


def register(spec: DataTypeSpec) -> DataTypeSpec:
    if spec.key in REGISTRY:
        raise ValueError(f"중복 DataSpec 등록: {spec.key}")
    REGISTRY[spec.key] = spec
    return spec


def get(key: str) -> Optional[DataTypeSpec]:
    return REGISTRY.get(key)


def data_spec() -> list[dict]:
    """전체 DataSpec 직렬화 — cron·무결성·문서·프론트가 소비하는 단일 출처."""
    return [s.model_dump() for s in REGISTRY.values()]


# ── P1 가격 시계열 (OHLCV) ────────────────────────────────────────────────────
# 조정정책이 소스별로 혼재(인벤토리) — adjustment는 "요구" 수준, manifest가 실측 기록.

register(DataTypeSpec(
    key="ohlcv.kr", pclass=PClass.PRICE, label="한국 주식 OHLCV", frequency="daily",
    history_rule="백테스트 시작일 − 최대 지표 window(예: 200MA→200영업일) 이상 연속",
    adjustment="split_adjusted", source="FinanceDataReader(KRX)",
    provides=["Open", "High", "Low", "Close", "Volume"],
    required_meta=_BASE_META + ["adjustment", "currency", "market"],
    downstream=["universe", "signal", "exit", "fill", "sizing.vol_inverse"],
    current_status="partial",
    notes="현재 FDR raw(분할조정 여부 미표시) — 실측 adjustment 검증·표기 필요(소n드니스 §3 핵심갭).",
))
register(DataTypeSpec(
    key="ohlcv.us", pclass=PClass.PRICE, label="미국 주식 OHLCV", frequency="daily",
    history_rule="백테스트 시작일 − 최대 지표 window 이상 연속",
    adjustment="total_return", source="yfinance(auto_adjust)",
    provides=["Open", "High", "Low", "Close", "Volume"],
    required_meta=_BASE_META + ["adjustment", "currency", "market"],
    downstream=["universe", "signal", "exit", "fill"],
    current_status="present",
    notes="yfinance auto_adjust=True → 배당·분할 조정. excess basis 시 시장지수 필요.",
))
register(DataTypeSpec(
    key="ohlcv.futures", pclass=PClass.PRICE, label="선물·지수 OHLCV(S&P500·WTI·금 등)",
    frequency="daily", history_rule="백테스트 시작일 − 최대 window 이상 연속",
    adjustment="total_return", source="yfinance / FinanceDataReader",
    provides=["Open", "High", "Low", "Close", "Volume"],
    required_meta=_BASE_META + ["adjustment"],
    downstream=["universe", "signal", "study.event"], current_status="present",
))
register(DataTypeSpec(
    key="ohlcv.crypto", pclass=PClass.PRICE, label="암호화폐 OHLCV", frequency="daily",
    history_rule="백테스트 시작일 − 최대 window 이상 연속(24/7 캘린더)",
    adjustment="raw", source="Binance REST",
    provides=["Open", "High", "Low", "Close", "Volume"],
    required_meta=_BASE_META, downstream=["universe", "signal"], current_status="present",
    notes="24/7 — 별도 캘린더. 분할/배당 없음 → raw.",
))

# ── P2 가격 파생 기술지표 (자체산출) ──────────────────────────────────────────

register(DataTypeSpec(
    key="indicator.derived", pclass=PClass.DERIVED,
    label="가격 파생 지표(수익률·MA괴리·RSI·ATR·변동성·모멘텀 등 24종)",
    frequency="daily", history_rule="입력 OHLCV의 룩백을 따름(자체산출)",
    derivation="computed", computed_from=["ohlcv.kr", "ohlcv.us", "ohlcv.futures", "ohlcv.crypto"],
    provides=["INDICATOR_META.BASE_INDICATOR_COLS 참조"],
    required_meta=["computed_from_adjustment"],
    downstream=["signal.ts_*", "signal.compare", "sizing", "label.bucket"],
    current_status="present",
    notes="compute_all()이 OHLCV에서 산출 — 외부수급 불필요. 단 입력 조정수준에 결과가 종속.",
))

# ── P3 펀더멘털·추정치 (PIT 필수) ─────────────────────────────────────────────

register(DataTypeSpec(
    key="fundamental.equity", pclass=PClass.FUNDAMENTAL,
    label="개별주 펀더멘털(마진·ROIC·EV/EBITDA·PER·PBR 등 13종)",
    frequency="quarterly", history_rule="발표분기 시계열(룩백 수년)", point_in_time=True,
    xs_completeness=True, source="SEC Company Facts(US) + OpenDART(KR)",
    provides=["INDICATOR_META.FUND_INDICATOR_COLS 참조"],
    required_meta=_BASE_META + ["as_of", "period"],
    downstream=["signal(펀더멘털 ref)", "screener"], current_status="present",
    notes="US=SEC(us-gaap, YTD 차분 분기복원), KR=OpenDART(분기, IS 3M·CF YTD차분·Q4=연간−누적). "
          "as_of=실 제출일(filed/접수일)=진짜 PIT — yfinance 45일 고정lag 대체. KR 발행주식수는 FDR 'Stocks'.",
))
register(DataTypeSpec(
    key="estimate.consensus", pclass=PClass.FUNDAMENTAL,
    label="애널리스트 추정치·리비전(EPS surprise·estimate revision)",
    frequency="event", history_rule="추정 스냅샷별 timestamp 시계열", point_in_time=True,
    source="(미연동)", required_meta=_BASE_META + ["as_of", "estimate_date"],
    downstream=["signal(추정치 ref)", "study.event(이벤트)"], current_status="absent",
    notes="task2(실적서프라이즈·리비전 지속성)에 필요 — 현재 미연동.",
))

# ── P4 매크로·시장 브로드캐스트 ───────────────────────────────────────────────

register(DataTypeSpec(
    key="macro.market", pclass=PClass.MACRO, label="시장 지표(VIX·달러지수·^TNX·MOVE 등)",
    frequency="daily", history_rule="백테스트 기간 + ffill 가능 길이",
    source="yfinance", provides=["Close(=val)"], required_meta=_BASE_META,
    downstream=["study.label(국면 라벨)", "signal(브로드캐스트 ref)"], current_status="present",
    notes="종목과 캘린더 달라 ffill 브로드캐스트(resolve_data)로 정렬.",
))
register(DataTypeSpec(
    key="macro.fred", pclass=PClass.MACRO, label="거시 시리즈(금리·신용·환율 일간 16종)",
    frequency="daily", history_rule="백테스트 기간", source="FRED CSV",
    provides=["Close(=val)"], required_meta=_BASE_META,
    downstream=["study.label", "signal(브로드캐스트 ref)"], current_status="present",
))
register(DataTypeSpec(
    key="macro.fred_lagged", pclass=PClass.MACRO,
    label="거시 월간(실업률·CPI·GDP 등 10종, 발표지연)", frequency="monthly",
    history_rule="발표분기 시계열", point_in_time=True, source="FRED CSV(15~60일 lag)",
    provides=["Close(=val)"], required_meta=_BASE_META + ["as_of"],
    downstream=["study.label"], current_status="present",
    notes="발표지연 있음 → as_of로 PIT 표기 권장.",
))

# ── P5 분류·정적 메타 ─────────────────────────────────────────────────────────

register(DataTypeSpec(
    key="static.symbol_master", pclass=PClass.STATIC,
    label="종목 마스터(통화·시장·종목구분·종목명)", frequency="static",
    history_rule="현행 스냅샷(일 1~2회 갱신)", source="KIS .mst/.cod",
    provides=["currency", "market", "kind", "name"], required_meta=["source", "fetched_at"],
    downstream=["fill(round_to_tick)", "universe.exclude_macro"],
    current_status="partial",
    notes="권위 메타가 메모리 캐시에만 존재 — dataset 미부착. currency를 sym.isdigit() 휴리스틱 대체.",
))
register(DataTypeSpec(
    key="static.tick_cost", pclass=PClass.STATIC, label="호가단위·거래비용 규칙",
    frequency="static", history_rule="시장·가격대별 규칙 테이블",
    source="exec_defaults(KRX 호가표·bps)", provides=["tick_table", "commission_bps", "sell_tax_bps"],
    required_meta=["source"], downstream=["fill(round_to_tick)", "simulation.commission/sell_tax"],
    current_status="present",
))
register(DataTypeSpec(
    key="static.classification", pclass=PClass.STATIC, label="섹터·산업 분류",
    frequency="static", history_rule="시점 변경 가능 → interval 권장(P6 성격)",
    source="FinanceDataReader(KRX-DESC)", provides=["sector", "industry"],
    required_meta=["source", "as_of"],
    downstream=["signal.group_rank", "signal.group_aggregate", "signal.group_neutralize"],
    current_status="present",
    notes="KRX-DESC의 Industry(업종, 그룹 기본축)·Sector(소속부). KR 전종목 사이드카(_classification.json) → "
          "get_symbol_group 소비. US 섹터는 후속(yfinance .info). 현행 스냅샷(시점 이력 없음).",
))
register(DataTypeSpec(
    key="static.market_cap", pclass=PClass.STATIC, label="시가총액·거래대금",
    frequency="daily", history_rule="시점별(스크리너·사이징 참조)",
    source="FinanceDataReader(KRX 스냅샷)", provides=["market_cap", "trade_value"],
    required_meta=_BASE_META, downstream=["universe.screener", "sizing"], current_status="partial",
    notes="krx_cache(메모리·스크리너 전용) — 백테스트 store 미부착.",
))
register(DataTypeSpec(
    key="static.calendar", pclass=PClass.STATIC, label="시장별 거래 캘린더",
    frequency="static", history_rule="시장별 세션·휴장일", source="exchange_calendars(KR/US)",
    provides=["sessions"], required_meta=["source", "fetched_at"],
    downstream=["entry.rebalance", "label.calendar", "평가 컨텍스트 스코핑"], current_status="present",
    notes="혼합시장 유니버스의 합집합 달력 희소화 방지에 핵심.",
))
register(DataTypeSpec(
    key="static.listing", pclass=PClass.STATIC, label="상장·폐지일",
    frequency="static", history_rule="종목별 first/last trade date",
    source="FinanceDataReader(KRX-DESC/KRX-DELISTING)",
    provides=["listing_date", "delisting_date"], required_meta=["source"],
    downstream=["universe(생존편향)", "워밍업 충분성"], current_status="present",
    notes="KR 상장일(KRX-DESC)+폐지일(KRX-DELISTING, 1956~) 사이드카(_listing.json) → 매니페스트 종목별 흡수. "
          "US 상폐 이력은 무료 소스 부재 — 후속. 멤버십 이력(시점 유니버스)은 별도(membership.index, Stage 4).",
))

# ── P6 코퍼레이트액션·지수 멤버십 ─────────────────────────────────────────────

register(DataTypeSpec(
    key="corpaction.adjustment", pclass=PClass.CORPACTION, label="배당·액면분할(가격 조정)",
    frequency="event", history_rule="이벤트(ex-date·비율·금액)", source="(부분: yfinance auto_adjust)",
    provides=["split", "dividend"], required_meta=["source", "ex_date"],
    downstream=["ohlcv.* adjustment 정합", "total_return 전략"], current_status="partial",
    notes="yfinance만 조정 내장, FDR/Binance raw — 소스별 조정정책 혼재(핵심 무결성 갭).",
))
register(DataTypeSpec(
    key="membership.index", pclass=PClass.CORPACTION, label="지수 구성 이력(편입/편출 구간)",
    frequency="event", history_rule="시점 구간(interval) — point-in-time 유니버스",
    point_in_time=True, source="US: fja05680/sp500 (1996~). KR: 무료 수급 불가",
    provides=["index_membership_intervals"], required_meta=["source", "effective_date"],
    downstream=["universe(US 시점별 지수 구성)", "생존편향 게이트(D-surv)"], current_status="partial",
    notes="US=fja05680(다년 PIT, Stage 4 예정). KR=무료 시점이력 소스 부재(pykrx KRX로그인·KRX오픈API "
          "12개월·FDR 미제공 — 수급 불가) → 스크리너(market_cap 상위 N)로 대형주 유니버스 근사 대체. "
          "별도 KR 지수-멤버십 universe kind는 미구현·미지원. D-surv는 KR all/screener에 정직히 경고 유지.",
))
