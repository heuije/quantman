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

from .policy import CORE_FLOOR


class PClass(str, Enum):
    PRICE = "P1"          # OHLCV
    DERIVED = "P2"        # 가격 파생 기술지표
    FUNDAMENTAL = "P3"    # 펀더멘털·추정치 (PIT 필수)
    MACRO = "P4"          # 매크로·시장 브로드캐스트
    STATIC = "P5"         # 분류·정적 메타 (섹터·통화·tick·상장일)
    CORPACTION = "P6"     # 코퍼레이트액션·지수 멤버십 이력
    FLOW = "P7"           # 투자자 수급·플로우 (기관·외국인 순매수)


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
    floor: Optional[str] = None           # 정책 목표 최소 깊이(ISO). Core=CORE_FLOOR / Enrichment=
                                          # 소스 자연 floor / None=목표 미정. 실측(manifest first)과
                                          # 비교해 인벤토리가 "백필 진행중"을 정직 노출.
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
    floor=CORE_FLOOR, adjustment="split_adjusted", source="FinanceDataReader(KRX)",
    provides=["Open", "High", "Low", "Close", "Volume"],
    required_meta=_BASE_META + ["adjustment", "currency", "market"],
    downstream=["universe", "signal", "exit", "fill", "sizing.vol_inverse"],
    current_status="partial",
    notes="현재 FDR raw(분할조정 여부 미표시) — 실측 adjustment 검증·표기 필요(소n드니스 §3 핵심갭).",
))
register(DataTypeSpec(
    key="ohlcv.us", pclass=PClass.PRICE, label="미국 주식 OHLCV", frequency="daily",
    history_rule="백테스트 시작일 − 최대 지표 window 이상 연속",
    floor=CORE_FLOOR, adjustment="total_return", source="yfinance(auto_adjust)",
    provides=["Open", "High", "Low", "Close", "Volume"],
    required_meta=_BASE_META + ["adjustment", "currency", "market"],
    downstream=["universe", "signal", "exit", "fill"],
    current_status="present",
    notes="yfinance auto_adjust=True → 배당·분할 조정. excess basis 시 시장지수 필요.",
))
register(DataTypeSpec(
    key="ohlcv.futures", pclass=PClass.PRICE, label="선물·지수 OHLCV(S&P500·WTI·금 등)",
    frequency="daily", history_rule="백테스트 시작일 − 최대 window 이상 연속",
    floor=CORE_FLOOR, adjustment="total_return", source="yfinance / FinanceDataReader",
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
    label="애널 컨센서스·목표가·투자의견(증권사별 standing 집계)",
    frequency="event", history_rule="리포트 발표일별 이벤트 → 일별 ffill(신선도窓 180일)",
    floor=CORE_FLOOR, point_in_time=True,
    source="네이버 금융 리서치(finance.naver.com/research)",
    provides=["consensus_target", "consensus_target_median", "analyst_count",
              "consensus_opinion", "target_dispersion", "target_revision_pct",
              "days_since_report"],
    required_meta=_BASE_META + ["as_of"],
    downstream=["signal(컨센서스 ref)", "screener", "study.event(목표가 리비전)"],
    current_status="present",
    notes="네이버=개별 리포트 스트림(reports_kr 피드: 발표일·증권사·목표가·투자의견). 원시 리포트 전건 "
          "영구보관 + 증권사별 최신 standing(신선도窓 내 1표) 횡단집계로 일별 컨센서스 산출 — 새 리포트는 "
          "해당 증권사 슬롯만 갱신(덮어쓰기 없음). target_upside(괴리율)는 indicators에서 Close 결합 파생. "
          "옛 한경(consensus_kr) 은퇴(2026-07): 네이버가 커버리지 2배·대형 증권사 포함·이력 2007까지로 우위. "
          "목표가는 리포트 상세 fetch로 부착(chunked 백필 2010까지·진행따라 이력 채워짐).",
))

# ── P4 매크로·시장 브로드캐스트 ───────────────────────────────────────────────

register(DataTypeSpec(
    key="macro.market", pclass=PClass.MACRO, label="시장 지표(VIX·달러지수·구리·MOVE 등)",
    frequency="daily", history_rule="백테스트 기간 + ffill 가능 길이",
    source="yfinance", provides=["Close(=val)"], required_meta=_BASE_META,
    downstream=["study.label(국면 라벨)", "signal(브로드캐스트 ref)"], current_status="present",
    notes="종목과 캘린더 달라 ffill 브로드캐스트(resolve_data)로 정렬.",
))
register(DataTypeSpec(
    key="macro.fred", pclass=PClass.MACRO, label="거시 시리즈(스프레드·기대인플레·신용·환율·SOFR 일간)",
    frequency="daily", history_rule="백테스트 기간", source="FRED CSV",
    provides=["Close(=val)"], required_meta=_BASE_META,
    downstream=["study.label", "signal(브로드캐스트 ref)"], current_status="present",
))
register(DataTypeSpec(
    key="macro.bonds", pclass=PClass.MACRO,
    label="국가별 국채 수익률(미·일·유·중 전만기 커브·1M~40Y)",
    frequency="daily", history_rule="백테스트 기간",
    source="FRED(미·중)·재무성MOF(일)·ECB(유)", provides=["Close(=val)"],
    required_meta=_BASE_META,
    downstream=["study.label(국면 라벨)", "signal(브로드캐스트 ref)"], current_status="present",
    notes="국채 피드(data/feeds/bonds.py)가 만기별 명명 시계열 발행. KR 국고채는 macro.krx(KRX 일별).",
))
register(DataTypeSpec(
    key="macro.fred_lagged", pclass=PClass.MACRO,
    label="거시 월간(실업률·CPI·GDP 등 10종, 발표지연)", frequency="monthly",
    history_rule="발표분기 시계열", point_in_time=True, source="FRED CSV(15~60일 lag)",
    provides=["Close(=val)"], required_meta=_BASE_META + ["as_of"],
    downstream=["study.label"], current_status="present",
    notes="발표지연 있음 → as_of로 PIT 표기 권장.",
))
register(DataTypeSpec(
    key="macro.krx", pclass=PClass.MACRO,
    label="KR 시장지표(V-KOSPI·옵션풋콜비율·KRX채권지수·국고채3/10년·선물 미결제약정·ETF AUM/flow)",
    frequency="daily", history_rule="2010~", floor=CORE_FLOOR,
    source="공식 KRX Open API(data-dbg.krx.co.kr)",
    provides=["Close(=val)"], required_meta=_BASE_META,
    downstream=["study.label(국면 라벨)", "signal(브로드캐스트 ref)"], current_status="present",
    notes="AUTH_KEY(KRX_API_KEY) 필요·미설정 시 비활성. 매크로형 명명 시계열(MACRO_KRX_SYMBOLS).",
))
register(DataTypeSpec(
    key="macro.cot", pclass=PClass.MACRO,
    label="US 선물 COT 포지셔닝(투기순포지션·주간 미결제약정 — WTI·천연가스·금·은·구리·나스닥·S&P500·비트코인)",
    frequency="weekly", history_rule="1986~(Legacy Futures-Only)", floor=CORE_FLOOR,
    point_in_time=True,
    source="CFTC 공식 Socrata(publicreporting.cftc.gov, Legacy Futures-Only)",
    provides=["Close(=val)"], required_meta=_BASE_META + ["as_of"],
    downstream=["study.label(국면 라벨)", "signal(브로드캐스트 ref)"], current_status="present",
    notes="무키 공개 API. 화요일 보고분을 금요일 공개 — index=보고일+3일(공개일)로 PIT 반영. "
          "시장당 2시리즈(투기순포지션=비상업 롱−숏·미결제약정) — US 선물 유일 무료 OI(주간). "
          "매크로형 명명 시계열(MACRO_COT_SYMBOLS). floor는 정책 최소(실제 1986~ 초과 수집).",
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
    source="FinanceDataReader(KRX-DESC + S&P500)", provides=["sector", "industry"],
    required_meta=["source", "as_of"],
    downstream=["signal.group_rank", "signal.group_aggregate", "signal.group_neutralize"],
    current_status="present",
    notes="KR=KRX-DESC의 Industry(KSIC 업종, 그룹 기본축)·Sector(소속부). US=S&P500의 Industry(GICS 서브산업)·"
          "Sector(GICS 섹터). 한 사이드카(_classification.json·숫자=KR/알파=US) → get_symbol_group 소비. "
          "현행 스냅샷(시점 이력 없음).",
))
register(DataTypeSpec(
    key="static.market_cap", pclass=PClass.STATIC, label="시가총액·거래대금",
    frequency="daily", history_rule="시점별(스크리너·사이징 참조)", floor=CORE_FLOOR,
    source="공식 KRX Open API sto(이력 2010~, 포털 신청 필요) + FinanceDataReader(현행 스냅샷)",
    provides=["market_cap", "trade_value", "shares_listed"],
    required_meta=_BASE_META + ["as_of"], downstream=["universe.screener", "sizing"],
    current_status="partial",
    notes="현행 스냅샷=krx_cache(메모리·스크리너 전용). 이력 수집 경로(marketcap_krx 피드·"
          "종목별 parquet·PIT as_of=거래일) 가동 — `sto` 인가·라이브 응답 검증 완료(2026-07-03: "
          "KOSPI 945+KOSDAQ 1,821종목/일·ISU_CD 6자·삼성 시총=종가×상장주식수 크로스체크 일치·"
          "T+1 08시 확정). 엔진 소비(indicators attach) 배선 전까지 partial.",
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

# ── P7 투자자 수급·플로우 (기관·외국인 순매수) ────────────────────────────────

register(DataTypeSpec(
    key="flow.kr_investor", pclass=PClass.FLOW,
    label="투자자별 수급(기관·외국인 순매수)", frequency="daily",
    history_rule="백테스트 시작일 − 최대 window(예: ts_sum 20일) 이상 연속",
    floor=CORE_FLOOR, point_in_time=True,
    source="KRX 정보데이터시스템(data.krx.co.kr) — pykrx 경유(무료 KRX 계정 로그인)",
    provides=["inst_net_buy", "foreign_net_buy"],
    required_meta=_BASE_META + ["as_of"],
    downstream=["signal(수급 ref)", "screener", "study.event"],
    current_status="present",
    notes="기관·외국인 순매수(거래대금·원). 일별 dense, as_of=거래일. 'N일 연속/누적'·'급증'은 ts_sum·rolling·pct "
          "등 기존 시계열 연산자로 조합(원시 일별 컬럼만 — 직교 프리미티브). 소스=pykrx "
          "get_market_trading_value_by_date(KRX 마켓플레이스, ~2010 깊이). KRX가 2025-12-27 로그인 의무화 → "
          "무료 KRX 계정의 KRX_ID/KRX_PW env 필수(브로커리지 아님 — KIS 보안경계 무관). 미설정 시 feed 비활성"
          "(빈결과·골든 무영향). 봇차단된 웹 스크랩이라 취약(버전핀·retry/resume). 외인 보유율은 후속. "
          "cron 적재 가동중(10분 백필→floor + 16:30 일일증분, 2026-07 프로덕션 로그 검증). "
          "⚠공식 KRX Open API엔 투자자별 엔드포인트 부재(카탈로그 검증) — pykrx가 유일 무료 경로.",
))
register(DataTypeSpec(
    key="flow.us_short_volume", pclass=PClass.FLOW,
    label="US 일별 공매도 거래량(off-exchange)", frequency="daily",
    history_rule="2018-08~(FINRA consolidated 포맷 — 구포맷 병합 시 소급)",
    floor="2018-08-01", point_in_time=True,
    source="FINRA Reg SHO daily consolidated(cdn.finra.org, 무키)",
    provides=["short_volume", "short_exempt_volume", "total_volume"],
    required_meta=_BASE_META + ["as_of"],
    downstream=["signal(공매도 ref)", "screener", "study.event"],
    current_status="partial",
    notes="⚠off-exchange(TRF 보고분)만 — 시장 전체 아님·공매도 잔고(포지션)와 별개(잔고는 "
          "2010 floor 불가로 제외). as_of=거래일(당일 18:00 ET 게시 — KR 수급과 동일 규약). "
          "원시 3컬럼만 적재(비율·급증은 파생). 휴장=404 미게시. **수집 가동·엔진 소비 배선은 "
          "후속**(marketcap과 함께 — 엔진이 계산 못 하는 컬럼을 컴파일러에 노출하지 않음) → partial.",
))
register(DataTypeSpec(
    key="flow.institutional_13f", pclass=PClass.FLOW,
    label="US 기관 13F 보유(총보유가치·보유주식수·보유기관수·전분기 순증감)", frequency="quarterly",
    history_rule="2013Q2~(SEC 구조화 Form 13F Data Sets — 2013Q1↓ 원시파싱 후속)",
    floor="2013-04-01", point_in_time=True,
    source="SEC Form 13F Data Sets 분기 ZIP(구조화) + SEC FTD로 CUSIP→ticker(무키)",
    provides=["institutional_value", "institutional_shares", "institutional_holders",
              "institutional_qoq_change"],
    required_meta=_BASE_META + ["as_of"],
    downstream=["signal(기관보유 ref)", "screener", "study.event"],
    current_status="partial",
    notes="전 신고자 INFOTABLE을 CUSIP별 합산 → 종목별 분기 시계열(institutional/{ticker}.parquet). "
          "as_of=보고분기말+45일(제출기한=집계 공개확정) → 미래참조 0. PIT=(CIK,분기) 최신신고만"
          "(수정신고 supersede). 필러 오기입(값 단위·자릿수)은 CUSIP별 내재가격 중앙값 5배 밖 제외로 "
          "정제(value·shares 동시·MSFT/AAPL 실측 가격 일치 검증). 옵션(PUTCALL)/비주식 제외=주식 롱 "
          "보유만(13F 롱온리). institutional_qoq_change=institutional_shares 전분기 대비 %(소비층 파생). "
          "미매핑 CUSIP(FTD 미등재 비유동주)·holders=1 종목 value 잔여오류는 정직 한계(신호는 "
          "holders·qoq 우선). floor=구조화 자연 floor(2010 Core 미달분 정직 노출). 백필 진행중 → partial.",
))
