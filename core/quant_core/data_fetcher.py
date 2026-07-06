"""
Data fetcher — 가격·매크로 시계열(parquet). 펀더멘털·섹터·상폐일은 data/feeds/ 피드가 담당.
  - yfinance   : S&P500, 원유선물, 천연가스선물, 금선물, 개별종목(US/KR)
  - FinanceDataReader : 코스피200선물ETF(261220), 나스닥100선물ETF(304940), 은선물ETF(144600)
  - Binance REST: 비트코인
  (펀더멘털 = SEC/OpenDART 피드, 섹터·상폐일 = FDR 피드 — core/quant_core/data/feeds/ 참조)
"""

import io
import json
import os
import tempfile
import time
import requests
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
import FinanceDataReader as fdr

from pathlib import Path
from datetime import datetime, timedelta, date

from .data.policy import CORE_FLOOR, CORE_FLOOR_COMPACT  # noqa: F401 (compact은 서버 cron이 소비)
from .parquet_io import read_parquet_safe, write_parquet_atomic, quarantine_corrupt

warnings.filterwarnings("ignore")

# 데이터 저장 위치 — 환경변수로 덮어쓸 수 있다(로컬앱은 사용자 디렉터리를 가리킴).
DATA_DIR = Path(os.getenv("QP_CORE_DATA_DIR")
                or Path(__file__).parent.parent / "data")
FUNDAMENTALS_DIR = DATA_DIR / "fundamentals"
CONSENSUS_DIR = DATA_DIR / "consensus"      # 애널 컨센서스 패널 (reports_kr[네이버] 피드가 산출, KR 한정)
FLOW_DIR = DATA_DIR / "flow"                # 기관·외국인 수급 (flow_kr 피드, KR 한정)
FUTURES_PANEL_DIR = DATA_DIR / "futures_panel"  # 선물 만기물별 일봉 패널 (krx_openapi 피드)
REPORTS_DIR = DATA_DIR / "reports"          # 애널 리포트 목록 아카이브 (reports_kr 피드, 네이버, KR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

USER_STOCKS_PATH = DATA_DIR / "user_stocks.json"

# 자동 관리되는 한국 거래 가능 종목 코드 리스트 (KIS 마스터 KOSPI/KOSDAQ + 등록 전략 union)
# 형식: ["005930", "000660", ...] — 코드 그대로 parquet 파일명·load 키로 사용
MANAGED_KR_PATH = DATA_DIR / "managed_kr_stocks.json"

# 사용자 등록으로 자동 추가된 해외 종목 — on-demand fetch + 영구 캐시
# 형식: [{"code":"AAPL", "name":"Apple Inc."}, ...]
MANAGED_OVERSEAS_PATH = DATA_DIR / "managed_overseas_stocks.json"

# ── 기본 종목 정의 ────────────────────────────────────────────────────────────

# 자산 (가격 시계열)
YFINANCE_SYMBOLS = {
    "S&P500":       "^GSPC",
    "원유선물":      "CL=F",
    "천연가스선물":  "NG=F",
    "금선물":        "GC=F",
    # USD 실선물 — 선물 분석 대시보드용(기존 KRW ETF/현물 프록시와 별도 키).
    "나스닥선물":     "NQ=F",
    "은선물(COMEX)":  "SI=F",
    "비트코인선물":   "BTC=F",
    # 세계 주요 지수 — GlobalMarket 탭 표시 + 챗 크로스에셋 참조(서빙 일원화 Phase 6a·표시=챗 사용가능).
    "나스닥지수":     "^IXIC",
    "다우지수":       "^DJI",
    "코스피지수":     "^KS11",
    "코스닥지수":     "^KQ11",
    "닛케이225":      "^N225",
    "항셍":          "^HSI",
    "FTSE100":       "^FTSE",
    "DAX":           "^GDAXI",
    "유로스톡스50":   "^STOXX50E",
    # 원자재 선물 — GlobalMarket 탭 표시 + 챗 참조(에너지·귀금속·산업금속·철강 밸류체인).
    "Brent원유":      "BZ=F",
    "가솔린":         "RB=F",
    "난방유":         "HO=F",
    "백금":          "PL=F",
    "팔라듐":         "PA=F",
    "알루미늄":       "ALI=F",
    "철광석":         "TIO=F",
    "철강":          "HRC=F",
}

FDR_SYMBOLS = {
    # ⚠ 261220은 KODEX200선물 *ETF*(주식, ~₩만대) — 실제 KOSPI200 선물 계약이 아니다.
    # 실선물(지수포인트·승수 250,000)은 "코스피200선물" 키로 KRX 만기물 패널서 파생(KRX_PANEL_FUTURES).
    # 둘이 같은 키를 쓰던 충돌(ETF에 선물 승수 적용)을 분리(F1) — ETF는 ETF 키로.
    "코스피200선물ETF": "261220",
    "나스닥100선물": "304940",
    "은선물":        "144600",
}

# 공식 KRX API(fut_bydd_trd) 만기물 패널에서 연속물을 파생하는 실선물(S4). 진실원천=패널,
# 서빙뷰=연속물("코스피200선물"). 롤/조정은 백테스트 파라미터(data.futures_roll·엔진 E2).
KRX_PANEL_FUTURES = ["코스피200선물"]

# 미니 코스피200선물 = 정규와 동일 KOSPI200 지수(동일 호가 포인트). 가격 데이터는 정규
# 시리즈를 공유한다(별도 수급 안 함) — 손익차는 엔진 승수(50k vs 250k)에서만 발생.
PRICE_ALIAS = {"미니코스피200선물": "코스피200선물"}

# 매크로 지표 — yfinance
MACRO_YF_SYMBOLS = {
    "VIX":          "^VIX",
    "VIX 3개월":    "^VIX3M",
    "VIX 9일":      "^VIX9D",
    "MOVE 지수":    "^MOVE",
    "SKEW 지수":    "^SKEW",
    "VVIX":         "^VVIX",
    "나스닥변동성":  "^VXN",
    "달러지수":      "DX-Y.NYB",
    "구리선물":      "HG=F",
    # 미국채10년은 국채 피드(data/feeds/bonds.py·전만기 커브)가 소유 → MACRO_BONDS_SYMBOLS (Phase 6a 단일 SSOT).
}

# 매크로 지표 — FRED (https://fred.stlouisfed.org, API 키 불필요)
# 일간/주간 시리즈 — 당일~익일 공시라 발표지연/룩어헤드 보정 불필요
MACRO_FRED_SYMBOLS = {
    "장단기금리차10Y2Y": "T10Y2Y",
    "장단기금리차10Y3M": "T10Y3M",
    "하이일드스프레드":   "BAMLH0A0HYM2",
    "투자등급스프레드":   "BAMLC0A0CM",
    "금융여건지수":       "NFCI",
    # 금리·신용 일간 시리즈 (국채 만기물 DGS*는 국채 피드로 이관 → MACRO_BONDS_SYMBOLS·단일 SSOT)
    "기대인플레이션10년":  "T10YIE",
    "실효기준금리":        "DFF",
    "회사채AAA금리":       "DAAA",
    "회사채BAA금리":       "DBAA",
    # 그룹 A-1 — 추가 금리·환율 일간 시리즈
    "기대인플레이션5년":   "T5YIE",
    "SOFR금리":           "SOFR",
    "무역가중달러지수":    "DTWEXBGS",
    "원달러환율":         "DEXKOUS",
}

# 월간 거시지표 — 발표지연이 커서 인덱스를 뒤로 밀어 룩어헤드를 방지한다.
# {표시명: (FRED 시리즈ID, 지연일수)}
MACRO_FRED_LAGGED = {
    "실업률":            ("UNRATE", 35),
    "비농업고용":        ("PAYEMS", 35),
    "CPI":               ("CPIAUCSL", 45),
    "코어CPI":           ("CPILFESL", 45),
    "산업생산":          ("INDPRO", 45),
    "M2통화량":          ("M2SL", 30),
    "미시간소비심리":     ("UMCSENT", 15),
    "시카고연준활동지수":  ("CFNAI", 35),
    "침체확률":          ("RECPROUSM156N", 60),
    "GDP":               ("GDP", 30),     # 파생(버핏지수) 계산용 + 자체 지표
}

# 전용 API로 수집하는 기타 매크로 지표
MACRO_OTHER = ["암호화폐공포탐욕"]

# 매크로 파생 지표 (수집한 시리즈로 계산)
MACRO_DERIVED = ["VIX 기간구조", "구리금비율", "회사채신용스프레드",
                 "버핏지수", "실질기준금리"]

# KR 시장지표 — 공식 KRX Open API(data/feeds/krx_openapi.py)가 수집. fetch_all 아님.
MACRO_KRX_SYMBOLS = ["코스피200변동성지수", "옵션풋콜비율", "KRX채권지수",
                     "국고채3년", "국고채10년",
                     "코스피200선물미결제약정", "코스닥150선물미결제약정",
                     "KRETF순자산총액", "KRETF순자금유입"]

# US 선물 COT 포지셔닝·주간 OI — CFTC 공식 Socrata(data/feeds/cot_cftc.py)가 수집. fetch_all 아님.
# 시장당 2시리즈({시장}투기순포지션·{시장}미결제약정). 피드 _MARKETS와의 정합은 가드가 잠근다.
MACRO_COT_SYMBOLS = [m + s
                     for m in ("원유선물", "천연가스선물", "금선물", "은선물", "구리선물",
                               "나스닥선물", "S&P500선물", "비트코인선물")
                     for s in ("투기순포지션", "미결제약정")]

# 국가별 국채 수익률 만기물 — 국채 피드(data/feeds/bonds.py)가 발행(US/JP/EU/CN 전만기·KR은
# KRX 국고채가 매크로 SSOT라 제외). {접두}{만기} 명명. 피드 bonds.macro_symbols()와의 정합은
# 드리프트 가드가 잠근다(test_bonds_macro_catalog_matches_feed).
_BOND_TENORS = {
    "미국채": ["1개월", "3개월", "6개월", "1년", "2년", "3년", "5년", "7년", "10년", "20년", "30년"],
    "일본국채": ["1년", "2년", "3년", "4년", "5년", "6년", "7년", "8년", "9년", "10년",
                 "15년", "20년", "25년", "30년", "40년"],
    "유로존국채": ["3개월", "6개월", "1년", "2년", "3년", "5년", "7년", "10년", "20년", "30년"],
    "중국국채": ["3개월"],
}
MACRO_BONDS_SYMBOLS = [pre + t for pre, ts in _BOND_TENORS.items() for t in ts]

ASSET_SYMBOLS = list(YFINANCE_SYMBOLS) + list(FDR_SYMBOLS) + ["비트코인"] + KRX_PANEL_FUTURES
MACRO_SYMBOLS = (list(MACRO_YF_SYMBOLS) + list(MACRO_FRED_SYMBOLS)
                 + list(MACRO_FRED_LAGGED) + MACRO_OTHER + MACRO_DERIVED
                 + MACRO_KRX_SYMBOLS + MACRO_COT_SYMBOLS + MACRO_BONDS_SYMBOLS)
ALL_SYMBOLS = ASSET_SYMBOLS + MACRO_SYMBOLS


def data_type_symbols() -> dict[str, list[str]]:
    """내장 심볼 → DataSpec 유형키 매핑(SSOT). 위 그룹 상수만으로 구성 — 커버리지 인벤토리·
    드리프트 가드의 공용 진실원천이다(하드코딩 신규 목록 금지).

    유형키는 data/spec.py REGISTRY 키와 일치한다. 주식(KR 숫자코드·US 티커)은 유니버스가
    동적(사용자 등록)이라 여기 열거하지 않고 매니페스트 per-symbol을 소비층이 KR/US로 집계한다.
    """
    return {
        # 가격형(P1) — feeds.classify_price_feed 분류와 일치.
        "ohlcv.crypto": ["비트코인"],
        "ohlcv.futures": (list(YFINANCE_SYMBOLS) + list(FDR_SYMBOLS) + KRX_PANEL_FUTURES),
        # 매크로 브로드캐스트(P4) — 수집 소스별 그룹.
        "macro.market": (list(MACRO_YF_SYMBOLS) + MACRO_OTHER + MACRO_DERIVED),
        "macro.fred": list(MACRO_FRED_SYMBOLS),
        "macro.fred_lagged": list(MACRO_FRED_LAGGED),
        "macro.krx": list(MACRO_KRX_SYMBOLS),
        "macro.cot": list(MACRO_COT_SYMBOLS),
        "macro.bonds": list(MACRO_BONDS_SYMBOLS),
    }

# 종목 카테고리 — 조건 빌더 UI에서 종목 목록을 그룹화하기 위한 분류.
# 의미 기준 분류(수집 소스와 무관). 미등재 종목(사용자 추가)은 "개별종목".
SYMBOL_CATEGORY: dict[str, str] = {
    # 자산
    "S&P500": "자산", "원유선물": "자산", "천연가스선물": "자산", "금선물": "자산",
    "코스피200선물": "자산", "미니코스피200선물": "자산", "코스피200선물ETF": "자산",
    "나스닥100선물": "자산", "은선물": "자산",
    "구리선물": "자산", "비트코인": "자산",
    "나스닥선물": "자산", "은선물(COMEX)": "자산", "비트코인선물": "자산",
    # 세계 지수 (Phase 6a — GlobalMarket 표시 + 챗 참조)
    "나스닥지수": "지수", "다우지수": "지수", "코스피지수": "지수", "코스닥지수": "지수",
    "닛케이225": "지수", "항셍": "지수", "FTSE100": "지수", "DAX": "지수", "유로스톡스50": "지수",
    # 원자재 (Phase 6a — 에너지·귀금속·산업금속·철강)
    "Brent원유": "자산", "가솔린": "자산", "난방유": "자산", "백금": "자산",
    "팔라듐": "자산", "알루미늄": "자산", "철광석": "자산", "철강": "자산",
    # 변동성
    "VIX": "변동성", "VIX 3개월": "변동성", "VIX 9일": "변동성", "VVIX": "변동성",
    "MOVE 지수": "변동성", "SKEW 지수": "변동성", "나스닥변동성": "변동성",
    "VIX 기간구조": "변동성",
    # 금리·환율 (국채 만기물은 아래 MACRO_BONDS_SYMBOLS comprehension으로 일괄)
    "기대인플레이션5년": "금리·환율", "기대인플레이션10년": "금리·환율",
    "SOFR금리": "금리·환율", "실효기준금리": "금리·환율", "실질기준금리": "금리·환율",
    "달러지수": "금리·환율", "무역가중달러지수": "금리·환율", "원달러환율": "금리·환율",
    "장단기금리차10Y2Y": "금리·환율", "장단기금리차10Y3M": "금리·환율",
    # 신용
    "하이일드스프레드": "신용", "투자등급스프레드": "신용", "금융여건지수": "신용",
    "회사채AAA금리": "신용", "회사채BAA금리": "신용", "회사채신용스프레드": "신용",
    # 거시지표
    "실업률": "거시지표", "비농업고용": "거시지표", "CPI": "거시지표",
    "코어CPI": "거시지표", "산업생산": "거시지표", "M2통화량": "거시지표",
    "미시간소비심리": "거시지표", "시카고연준활동지수": "거시지표",
    "침체확률": "거시지표", "GDP": "거시지표",
    "구리금비율": "거시지표", "버핏지수": "거시지표",
    # 심리
    "암호화폐공포탐욕": "심리",
    "옵션풋콜비율": "심리",
    "코스피200선물미결제약정": "심리", "코스닥150선물미결제약정": "심리",
    "KRETF순자금유입": "심리",
    # KR 시장지표 (공식 KRX API)
    "코스피200변동성지수": "변동성",
    "KRX채권지수": "금리·환율", "국고채3년": "금리·환율", "국고채10년": "금리·환율",
    "KRETF순자산총액": "거시지표",
    # US 선물 COT 포지셔닝 (CFTC) — 선물 OI 선례(심리)를 따름.
    **{s: "심리" for m in ("원유선물", "천연가스선물", "금선물", "은선물", "구리선물",
                          "나스닥선물", "S&P500선물", "비트코인선물")
       for s in (m + "투기순포지션", m + "미결제약정")},
    # 국채 만기물 (Phase 6a — 국채 피드 발행·US/JP/EU/CN 전만기)
    **{s: "금리·환율" for s in MACRO_BONDS_SYMBOLS},
}


def symbol_category(name: str) -> str:
    """종목이 속한 카테고리명을 반환. 미등재(사용자 종목)는 '개별종목'."""
    return SYMBOL_CATEGORY.get(name, "개별종목")


# ── 공통 유틸 ────────────────────────────────────────────────────────────────

def _parquet_path(symbol: str) -> Path:
    symbol = PRICE_ALIAS.get(symbol, symbol)   # 미니→정규 시리즈 공유
    return DATA_DIR / f"{symbol.replace('/', '_')}.parquet"

def _fund_path(name: str) -> Path:
    return FUNDAMENTALS_DIR / f"{name.replace('/', '_')}.parquet"

def _load_existing(symbol: str) -> pd.DataFrame:
    p = _parquet_path(symbol)
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)          # 손상 시 격리+None → 빈 DF(전체 재수급 유도)
    return df if df is not None else pd.DataFrame()

def _save(symbol: str, df: pd.DataFrame):
    if df.empty:
        return
    df = df.sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    write_parquet_atomic(df, _parquet_path(symbol))

def _merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return new
    if new.empty:
        return existing
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


# ── 선물 만기물 패널: 진실원천(만기물별 일봉) + 연속물 서빙뷰 파생 ─────────────────
# KRX fut_bydd_trd 만기물 패널을 진실원천으로 저장하고, 백테스트용 단일 연속 시계열은
# futures_roll.build_continuous로 파생한다(롤·조정은 데이터에 굽지 않고 백테스트 파라미터).
# 패널은 하루에 여러 만기물 → 날짜 중복. dedup 키 = (날짜, contract)이라 _merge(index 전용)와
# 다른 병합이 필요하다.

def _futures_panel_path(symbol: str) -> Path:
    symbol = PRICE_ALIAS.get(symbol, symbol)   # 미니→정규 패널 공유(동일 KOSPI200 지수)
    return FUTURES_PANEL_DIR / f"{symbol.replace('/', '_')}.parquet"

def has_futures_panel(symbol: str) -> bool:
    """만기물 패널 보유 여부 — 롤/조정이 백테스트에 실제 반영되는 선물인지(explain 정직성)."""
    return _futures_panel_path(symbol).exists()

def load_futures_panel(symbol: str) -> pd.DataFrame:
    p = _futures_panel_path(symbol)
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)
    return df if df is not None else pd.DataFrame()

def save_futures_panel(symbol: str, df: pd.DataFrame):
    if df.empty:
        return
    FUTURES_PANEL_DIR.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    write_parquet_atomic(df.sort_index(), _futures_panel_path(symbol))

def merge_futures_panel(symbol: str, new: pd.DataFrame) -> int:
    """신규 패널 행을 기존과 병합(중복 (날짜,contract)=new 우선). 반환 총 행수."""
    if new is None or new.empty:
        return len(load_futures_panel(symbol))
    new = new.copy()
    new.index = pd.to_datetime(new.index).tz_localize(None)
    existing = load_futures_panel(symbol)
    combined = pd.concat([existing, new]) if not existing.empty else new
    combined = (combined.reset_index(names="_d")
                .drop_duplicates(subset=["_d", "contract"], keep="last")
                .set_index("_d").sort_index())
    combined.index.name = None
    save_futures_panel(symbol, combined)
    return len(combined)

def rebuild_futures_continuous(symbol: str, roll_method: str | None = None,
                               series_adjust: str | None = None) -> int:
    """만기물 패널 → 기본 연속물 서빙뷰를 재구성해 symbol parquet 덮어쓰기.

    롤·조정 미지정 시 상품 카탈로그(exec_defaults) 기본값 — SSOT. 백테스트가 다른 롤을
    쓰면 엔진이 패널에서 재-stitch(E2). 반환 연속물 행수(패널 없으면 0).

    ⚠ 마이그레이션 안전: 기존 깊은 시리즈(예: 옛 CSV 시드 2010~)가 있는데 패널이 아직 얕으면
    (백필 진행 중) **덮어쓰지 않는다** — 얕은 연속물로 교체하면 백필이 2010까지 차오르기 전까지
    선물 백테스트가 얕은 데이터로 회귀한다. 패널이 기존 깊이에 도달한 뒤 단일출처로 클린 교체
    (얕은 창 회귀·소스 혼합 동시 회피). "증분은 앞으로만" 교훈의 데이터-소급판.
    """
    from .data.futures_roll import build_continuous
    from .exec_defaults import instrument_spec
    panel = load_futures_panel(symbol)
    if panel.empty:
        return 0
    spec = instrument_spec(symbol)
    cont = build_continuous(panel, roll_method or spec.default_roll or "at_expiry",
                            series_adjust or "none")
    if cont.empty:
        return 0
    existing = _load_existing(symbol)
    if (not existing.empty
            and cont.index.min() > existing.index.min() + pd.Timedelta(days=90)):
        return 0                    # 패널이 기존보다 얕음 — 백필 대기(기존 깊은 서빙뷰 유지)
    _save(symbol, cont)
    mark_data_dirty()
    return len(cont)


# ── 데이터셋 세대 마커 (캐시 일관성) ──────────────────────────────────────────
# 인메모리 캐시(서버 data_cache)가 디스크/다른 프로세스의 변경을 감지하도록, 모든
# 벌크 변경이 이 토큰을 갱신한다. 서버는 읽기 시 토큰을 싸게 확인해 바뀌었으면 리로드.
# manage·백필·cron이 별도 프로세스라도 공유 파일이라 라이브 서버가 자가 치유한다.
# (수동 파일 편집처럼 이 경로를 우회한 변경은 admin invalidate로 강제.)
_GENERATION_PATH = DATA_DIR / "_generation"


def data_generation() -> int:
    """현재 데이터셋 세대 토큰(쓰기마다 갱신). 파일 없으면 0."""
    try:
        return int(_GENERATION_PATH.read_text().strip() or "0")
    except (FileNotFoundError, ValueError, OSError):
        return 0


def mark_data_dirty() -> int:
    """데이터셋 변경 기록 — 세대 토큰을 현재 ns로 원자적 갱신. 벌크 변경 완료점·
    레지스트리 저장·서버 invalidate가 호출(per-file 호출 금지 — churn). 반환=새 토큰."""
    token = time.time_ns()
    fd, tmp = tempfile.mkstemp(dir=str(_GENERATION_PATH.parent), prefix="._gen")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(token))
        os.replace(tmp, _GENERATION_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return token


# ── 사용자 종목 관리 ──────────────────────────────────────────────────────────

def load_user_stocks() -> list[dict]:
    """사용자가 추가한 개별종목 목록 반환. [{name, ticker}, ...]"""
    if USER_STOCKS_PATH.exists():
        try:
            return json.loads(USER_STOCKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_user_stocks(stocks: list[dict]):
    USER_STOCKS_PATH.write_text(
        json.dumps(stocks, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ── yfinance (지수/선물/개별종목) ─────────────────────────────────────────────

def fetch_yfinance(symbol_name: str, ticker: str, start: str = CORE_FLOOR) -> pd.DataFrame:
    existing = _load_existing(symbol_name)
    if not existing.empty:
        last_date = existing.index[-1].date()
        from datetime import timezone
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.date()
        # 이미 오늘 데이터(UTC)까지 다 있다면 yfinance 호출 스킵
        if last_date >= today_utc:
            return existing
        # 마지막 데이터 날짜가 어제인데, 오늘 US 장 마감(20:00 UTC / 16:00 EDT) 전이라면 스킵
        # (장 마감 전에는 오늘자 신규 일봉이 없거나 미완성이며, 무엇보다 yfinance timezone 버그를 완벽히 차단함)
        if last_date >= today_utc - timedelta(days=1) and now_utc.hour < 20:
            return existing
        start = (existing.index[-1] + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.Ticker(ticker).history(start=start, auto_adjust=True)
        if df.empty:
            return existing
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[cols].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        merged = _merge(existing, df)
        _save(symbol_name, merged)
        mark_data_dirty()       # 단독 호출 시에도 라이브 캐시가 stale 유지 않도록(세대 신호)
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name}: {e}")
        return existing


def fetch_stock_price(name: str, ticker: str, start: str = CORE_FLOOR) -> pd.DataFrame:
    """개별종목 가격 데이터 수집 (yfinance 래퍼)."""
    return fetch_yfinance(name, ticker, start)


# ── FinanceDataReader (KRX ETF) ───────────────────────────────────────────────

def fetch_fdr(symbol_name: str, ticker: str, start: str = CORE_FLOOR) -> pd.DataFrame:
    existing = _load_existing(symbol_name)
    if not existing.empty:
        start = (existing.index[-1] + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = fdr.DataReader(ticker, start)
        if df.empty:
            return existing
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[cols].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        merged = _merge(existing, df)
        _save(symbol_name, merged)
        mark_data_dirty()       # 세대 신호 — 단독 호출 시 stale 캐시 방지
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name}: {e}")
        return existing


def fetch_korean_stocks(codes: list[str], start: str = CORE_FLOOR,
                         verbose: bool = False) -> dict[str, int]:
    """한국 거래소 종목 OHLC 일괄 수집 (FinanceDataReader, KRX 직접 소스).

    각 코드(예: "005930")로 fdr.DataReader 호출 → parquet incremental append.
    실패한 종목은 skip하고 로그 — 한 종목 실패가 전체를 막지 않는다.
    호출자(서버 cron)가 한국 거래 가능 종목 ~4,300개를 매일 1회 호출.

    **컬럼 의미** — FDR(NAVER 백엔드)의 OHLC는 모두 정규장(09:00~15:30) 기준:
      Open/High/Low/Close = 정규장 시초가/고가/저가/마감가
      Volume              = 정규장 거래량 (시간외 거래량 미포함)
      Change              = 정규장 종가 전일 대비 등락률
    시간외 단일가(16:00~18:00)는 별도 endpoint이며 본 fetch에 포함되지 않음.

    저장은 종목별 parquet에 직접 — in-memory aggregation은 하지 않는다(메모리
    누적이 4,000+ 종목 × ~2,500행 DataFrame으로 ~2 GB까지 dead allocation 발생).
    호출자는 결과 DataFrame을 받지 않고, count 통계만 받는다.

    Args:
        codes: KRX 종목 코드 리스트 (6자리)
        start: 새 종목 첫 fetch 시 시작일(기본 2010 — 깊은 이력). 기존 parquet 있으면
            무시되고 *앞으로만* 이어받음 → 기존 종목의 floor 이전 과거 소급은
            backfill_korean_stocks_depth가 담당(증분은 과거를 못 채우므로).
    Returns:
        {"ok": int, "skip": int, "fail": int} — count 통계만
    """
    import gc
    from datetime import timezone

    # KST 기준 마지막 마감된 거래일 구하기
    tz_kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(tz_kst)
    today_kst = now_kst.date()
    market_closed = now_kst.time() >= datetime.strptime("15:40:00", "%H:%M:%S").time()

    if today_kst.weekday() < 5:  # 월~금
        if market_closed:
            last_closed_market_date = today_kst
        else:
            days_to_subtract = 3 if today_kst.weekday() == 0 else 1
            last_closed_market_date = today_kst - timedelta(days=days_to_subtract)
    elif today_kst.weekday() == 5:  # 토요일
        last_closed_market_date = today_kst - timedelta(days=1)  # 금요일
    else:  # 일요일
        last_closed_market_date = today_kst - timedelta(days=2)  # 금요일

    n_ok = n_skip = n_fail = 0
    for i, code in enumerate(codes):
        if i > 0 and i % 100 == 0:
            gc.collect()

        existing = _load_existing(code)

        # 지능형 최신 상태 체크 (이미 마지막 마감장 데이터까지 다 갖고 있다면 fdr 호출 스킵)
        if not existing.empty:
            last_date = existing.index[-1].date()
            if last_date >= last_closed_market_date:
                n_skip += 1
                del existing
                continue

        s = (existing.index[-1] + timedelta(days=1)).strftime("%Y-%m-%d") \
            if not existing.empty else start
        try:
            df = fdr.DataReader(code, s)
        except Exception as e:
            if verbose:
                print(f"  [{i+1}/{len(codes)}] {code}: 오류 {e}")
            n_fail += 1
            del existing
            continue
        if df.empty:
            n_skip += 1
            del existing, df
            continue
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[cols].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        merged = _merge(existing, df)
        _save(code, merged)
        n_ok += 1
        del existing, df, merged

        if verbose and (i + 1) % 200 == 0:
            print(f"  진행: {i+1}/{len(codes)} (성공 {n_ok} · 신규없음 {n_skip} · 실패 {n_fail})")

    print(f"한국 종목 fetch 완료: 총 {len(codes)} → 성공 {n_ok} · 신규없음 {n_skip} · 실패 {n_fail}")
    if n_ok:
        mark_data_dirty()       # 데이터 변경 — 라이브 캐시 자가 리로드 신호
    return {"ok": n_ok, "skip": n_skip, "fail": n_fail}


# ── KR OHLCV 깊이 백필: 기존 종목을 floor(2010)까지 소급 prepend ────────────────
# 일일 fetch_korean_stocks는 기존 parquet을 *앞으로만* 증분 append하므로(증분 시작점=
# 마지막 보유일+1) 이미 데이터가 있는 종목의 floor 이전 과거는 영영 안 채워진다. 이 백필이
# 그 갭만 메운다. depth-done 마커로 완료 종목을 영구 skip → 완주 시 네트워크 0비용
# (펀더멘털·flow의 freshness skip, 컨센서스 cursor와 같은 '완료=무비용' 성질).

def _kr_ohlcv_depth_marker() -> Path:
    return DATA_DIR / "_kr_ohlcv_depth_done.json"


def _us_ohlcv_depth_marker() -> Path:
    return DATA_DIR / "_us_ohlcv_depth_done.json"


def _load_marker_set(p: Path) -> set[str]:
    """완료 마커 집합(JSON 배열) 로드 — KR/US 깊이 백필이 공유하는 단일 규약."""
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_marker_set(p: Path, done: set[str]) -> None:
    p.write_text(json.dumps(sorted(done), ensure_ascii=False), encoding="utf-8")


def _load_depth_done() -> set[str]:
    return _load_marker_set(_kr_ohlcv_depth_marker())


def _save_depth_done(done: set[str]) -> None:
    _save_marker_set(_kr_ohlcv_depth_marker(), done)


def backfill_korean_stocks_depth(codes: list[str], floor: str = CORE_FLOOR,
                                 budget_symbols: int = 150,
                                 verbose: bool = False) -> dict[str, int]:
    """기존 KR 종목 OHLCV 깊이를 floor까지 **소급(prepend)**.

    종목별: 최저일 <= floor면 이미 깊음 / 없으면 floor~(최저일-1)을 1회 받아 merge·prepend.
    빈 결과 = 상장이 floor 이후(genuine, 더 깊이 불가) → 마커. 네트워크 예외 = 마커 안 함(재시도).
    budget_symbols = 한 청크에서 실제 fetch 시도 종목 수 상한(*/10분 청크 분할·재개).
    """
    floor_ts = pd.to_datetime(floor)
    done = _load_depth_done()
    n_deepened = n_young = n_fail = 0
    attempted = 0
    for code in codes:
        if attempted >= budget_symbols:
            break
        if code in done:
            continue
        existing = _load_existing(code)
        if existing.empty:
            done.add(code)                      # 신규 — 일일 fetch가 floor부터 직접 받음
            continue
        if existing.index.min() <= floor_ts:
            done.add(code)                      # 이미 충분히 깊음
            continue
        attempted += 1
        end = (existing.index.min() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            df = fdr.DataReader(code, floor, end)
        except Exception as e:
            n_fail += 1
            if verbose:
                print(f"  {code}: 깊이백필 오류 {e}")
            continue                            # 마커 안 함 → 다음 청크 재시도
        if df is None or df.empty:
            done.add(code)                      # 상장이 floor 이후 — 더 깊이 불가
            n_young += 1
            continue
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[cols].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        merged = _merge(existing, df)
        _save(code, merged)
        n_deepened += 1
        if merged.index.min() <= floor_ts:
            done.add(code)                      # floor 도달
        # else: FDR이 한 번에 floor까지 못 줌(드묾) — 마커 보류, 다음 청크 계속
    _save_depth_done(done)
    if n_deepened:
        mark_data_dirty()
    return {"deepened": n_deepened, "young": n_young, "fail": n_fail,
            "done_total": len(done)}


def backfill_overseas_depth(codes: list[str] | None = None, floor: str = CORE_FLOOR,
                            budget_symbols: int = 200, batch: int = 100,
                            verbose: bool = False) -> dict[str, int]:
    """기존 해외(US) 종목 OHLCV 깊이를 floor까지 **소급(prepend)** — KR 깊이 백필의 US 미러.

    과거 fetch_managed_overseas가 backfill_start=2015로 수집한 코호트의 floor 이전 갭을
    메운다(실측: US 2010 도달 3%·2015 캡 36%). 일일 fetch는 앞으로만 append하므로 이
    백필 없이는 그 갭이 영영 안 채워진다 — KR과 동일한 이유·동일한 마커 규약.

    KR과의 차이 = 효율: 같은 min_date 그룹을 yf.download **배치 1콜**로 받는다(2015 캡
    코호트가 min_date를 공유 → 수천 종목이 소수 배치로 처리). 완료 판정은 KR과 동일:
    merged.min <= floor → done / 빈 결과 → 상장이 floor 이후(young, done) / 부분 소급 →
    마커 보류(다음 청크가 더 이른 창을 요청 → 빈 결과 → young 수렴).

    ⚠ 빈 응답의 신뢰 규약 = KR(FDR)과 동일: **예외 = 실패(마커 금지·재시도) / 무예외
    빈 결과 = young(floor 이전 상장 없음)**. yfinance가 무예외로 빈 프레임을 주는 글리치면
    false-young이 가능하나(외부 API 한계), 그 결과는 해당 종목이 기존 깊이에 머무는 것
    (오염 아님)이고, 반대로 빈 응답을 불신하면 young 그룹이 영원히 재시도돼 백필이
    수렴하지 않는다 — 수렴을 택한다.
    """
    floor_ts = pd.to_datetime(floor)
    if codes is None:
        codes = [s.get("code", "") for s in load_managed_overseas() if s.get("code")]
    done = _load_marker_set(_us_ohlcv_depth_marker())
    n_deepened = n_young = n_fail = 0

    # 1) 대상 선별 — 미완료·기존 보유·floor 미달 종목을 min_date별로 그룹(배치 효율).
    pending: dict[str, list[str]] = {}
    attempted = 0
    for code in codes:
        if attempted >= budget_symbols:
            break
        if code in done:
            continue
        existing = _load_existing(code)
        if existing.empty:
            done.add(code)                      # 신규 — 일일 fetch가 floor부터 직접 받음
            continue
        if existing.index.min() <= floor_ts:
            done.add(code)                      # 이미 충분히 깊음
            continue
        min_iso = existing.index.min().strftime("%Y-%m-%d")
        pending.setdefault(min_iso, []).append(code)
        attempted += 1

    # 2) 그룹별 배치 fetch — floor ~ (min_date 직전). yf.download의 end는 exclusive.
    for min_iso, group in pending.items():
        for i in range(0, len(group), batch):
            chunk = group[i:i + batch]
            try:
                data = yf.download(chunk, start=floor, end=min_iso, auto_adjust=True,
                                   group_by="ticker", threads=True, progress=False)
            except Exception as e:
                n_fail += len(chunk)
                if verbose:
                    print(f"  [US 깊이백필 배치 오류] {chunk[0]}…{chunk[-1]}: {e}")
                continue                        # 마커 안 함 → 다음 청크 재시도
            if data is None or data.empty:
                for code in chunk:              # 무예외 빈 응답 = young(KR/FDR와 동일 신뢰 규약)
                    done.add(code)
                n_young += len(chunk)
                time.sleep(1.0)
                continue
            multi = isinstance(data.columns, pd.MultiIndex)
            for code in chunk:
                try:
                    if multi:
                        if code not in data.columns.get_level_values(0):
                            done.add(code); n_young += 1
                            continue
                        df = data[code].copy()
                    else:
                        df = data.copy()
                    df = df[[c for c in _OHLCV_COLS if c in df.columns]].dropna(how="all")
                    if df.empty:
                        done.add(code)          # 상장이 floor 이후 — 더 깊이 불가
                        n_young += 1
                        continue
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    merged = _merge(_load_existing(code), df)
                    _save(code, merged)
                    n_deepened += 1
                    if merged.index.min() <= floor_ts:
                        done.add(code)          # floor 도달
                    # else: 부분 소급 — 마커 보류, 다음 청크가 더 이른 창 요청 → young 수렴
                except Exception as e:
                    n_fail += 1
                    if verbose:
                        print(f"  [US 깊이백필 오류] {code}: {e}")
            time.sleep(1.0)                     # 배치 간 rate-limit 완화
    _save_marker_set(_us_ohlcv_depth_marker(), done)
    if n_deepened:
        mark_data_dirty()
    return {"deepened": n_deepened, "young": n_young, "fail": n_fail,
            "done_total": len(done)}


# ── 자동 관리 종목 목록 ───────────────────────────────────────────────────────

def load_managed_kr_codes() -> list[str]:
    """현재 자동 갱신 대상에 등록된 한국 종목 코드 목록."""
    if MANAGED_KR_PATH.exists():
        try:
            return json.loads(MANAGED_KR_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_managed_kr_codes(codes: list[str]) -> None:
    """자동 갱신 대상 코드 목록 저장. 중복 제거 + 정렬."""
    unique = sorted(set(codes))
    MANAGED_KR_PATH.write_text(
        json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8")


def load_managed_overseas() -> list[dict]:
    """on-demand 등록된 해외 종목 목록. [{"code", "name"}, ...]"""
    if MANAGED_OVERSEAS_PATH.exists():
        try:
            return json.loads(MANAGED_OVERSEAS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_managed_overseas(stocks: list[dict]) -> None:
    """on-demand 해외 종목 목록 저장. code 기준 dedupe + yfinance 미수집 티커 제외.

    KIS 미국 마스터엔 우선주·유닛·클래스주가 '/' 표기(JPM/D·RAC/UN)로 섞여 들어오는데
    yfinance는 '/'를 쓰지 않아(클래스주=BRK-B, 우선주=JPM-PD) **항상 fetch 실패**한다
    (실측: '/' 티커 457개 중 parquet 생성 0개). 이들을 두면 매 수집마다 "Failed to get
    ticker" 폭주 + Yahoo throttle + 로그 500/sec 초과만 유발하므로 **유일한 write 경로인
    여기서 원천 차단**한다 — 기존 항목도 다음 저장(시드 cron) 때 함께 정리된다. 정당한
    클래스주는 _seed_sp500_overseas가 대시 형식(BRK-B)으로 별도 보존(code dedupe로 중복 제거).
    """
    seen, uniq = set(), []
    for s in stocks:
        c = s.get("code", "").strip()
        if not c or "/" in c:           # 빈 코드·yfinance 미수집('/') 제외
            continue
        if c not in seen:
            seen.add(c)
            uniq.append({"code": c, "name": s.get("name", "")})
    MANAGED_OVERSEAS_PATH.write_text(
        json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")


# 패키지에 동봉된 S&P500 큐레이션 유니버스 (gen_sp500.py 생성)
_SP500_PATH = Path(__file__).parent / "universe" / "sp500.json"


def load_sp500() -> list[dict]:
    """S&P500 구성종목 [{symbol(점형식), name}, ...]. 미국 자동선택 유니버스(스테이지1).

    파일 없으면 빈 리스트(미국 스크리너 비활성, 그래도 수동 거래는 가능).
    """
    if not _SP500_PATH.exists():
        return []
    try:
        return json.loads(_SP500_PATH.read_text(encoding="utf-8")).get(
            "constituents", [])
    except Exception:
        return []


def sp500_yf_codes() -> list[str]:
    """S&P500 종목을 yfinance/dataset 코드(대시 형식: BRK-B)로 반환.

    클래스주 점(.)을 yfinance 표기 대시(-)로 변환. 그 외는 그대로.
    데이터 수집(managed_overseas)·dataset 키로 사용한다.
    """
    return [c["symbol"].replace(".", "-") for c in load_sp500() if c.get("symbol")]


_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _last_closed_us_date() -> date | None:
    """마지막으로 마감된 US 정규장 거래일(America/New_York, 16:00 마감 기준, 주말 보정).

    공휴일은 보정하지 않는다 — 휴일이면 그 날짜로 과대평가될 수 있으나, 그 경우 해당
    종목을 '스킵'하지 않고 재fetch할 뿐(무해). 항상 실제 마지막 거래일 이상이라 과소평가
    (=신선도 오판으로 stale 종목을 스킵해 데이터 누락)는 발생하지 않는다. 시간대 확인
    불가 시 None → 신선도 스킵 비활성(전량 fetch, 보수적). fetch_korean_stocks의 KST
    휴리스틱을 US/Eastern으로 옮긴 동형 정책."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:               # tzdata 부재 등 — 스킵 비활성(전량 fetch)
        return None
    d, wd = now_et.date(), now_et.weekday()
    closed = now_et.time() >= datetime.strptime("16:00:00", "%H:%M:%S").time()
    if wd < 5:                       # 월~금
        return d if closed else d - timedelta(days=3 if wd == 0 else 1)
    return d - timedelta(days=1 if wd == 5 else 2)   # 토→금, 일→금


def fetch_managed_overseas(limit: int | None = None, verbose: bool = False,
                           batch: int = 200, backfill_start: str = CORE_FLOOR) -> int:
    """managed_overseas 해외 종목 OHLCV를 yfinance 배치(yf.download)로 일괄 수집.

    종목당 1콜 루프는 미국 마스터 전체(~1만+) 규모에 부적합(수 시간) → 배치로
    수백 종목을 1콜에 받는다. 신규(parquet 없음)는 backfill_start부터 백필,
    기존은 최근창만 incremental. 데이터 없는 티커는 자동 skip → /symbols 비노출
    (§4.8 "데이터 보유분만"). _merge/_save로 단일수집과 동일하게 병합·저장한다.

    글로벌 cron과 수동 갱신(manage)이 공유. limit=N이면 앞 N개만(개발/검증용).
    Returns: 데이터를 저장한 종목 수.
    """
    codes = [s.get("code", "") for s in load_managed_overseas() if s.get("code")]
    seen: set[str] = set()
    codes = [c for c in codes if not (c in seen or seen.add(c))]   # 순서보존 dedupe
    if limit is not None:
        codes = codes[:limit]

    new_codes = [c for c in codes if not _parquet_path(c).exists()]
    upd_codes = [c for c in codes if _parquet_path(c).exists()]

    # 신선도 게이트 — 마지막 마감 US 거래일까지 이미 보유한 종목은 fetch 제외
    # (fetch_korean_stocks와 동일 정책). 과거엔 매 실행마다 기존 전 종목의 최근창을
    # 무조건 재다운로드해 재시작·cron마다 불필요한 네트워크 호출이 쌓였다(콜드스타트 지연).
    last_closed = _last_closed_us_date()
    if last_closed is not None:
        stale = []
        for c in upd_codes:
            ex = _load_existing(c)
            if not ex.empty and ex.index[-1].date() >= last_closed:
                continue                     # 이미 최신 — 스킵
            stale.append(c)
        upd_codes = stale

    # 기존: 최근 ~10일만(과대수집은 _merge가 중복 제거 — 저렴). 신규: 전체 백필.
    recent_start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    done = _fetch_overseas_batched(upd_codes, recent_start, batch, verbose)
    done += _fetch_overseas_batched(new_codes, backfill_start, batch, verbose)
    if done:
        mark_data_dirty()       # 데이터 변경 — 라이브 캐시 자가 리로드 신호
    return done


def _fetch_overseas_batched(codes: list[str], start: str, batch: int,
                            verbose: bool = False) -> int:
    """yf.download 배치 수집 — chunk별 1콜, 티커별로 분리해 merge·save. 저장 수 반환."""
    import time
    done = 0
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        try:
            data = yf.download(chunk, start=start, auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"  [배치 오류] {chunk[0]}…{chunk[-1]}: {e}")
            continue
        if data is None or data.empty:
            if verbose:
                print(f"  해외 배치 {min(i + batch, len(codes))}/{len(codes)} (빈 응답)")
            time.sleep(1.0)
            continue
        multi = isinstance(data.columns, pd.MultiIndex)
        for code in chunk:
            try:
                if multi:
                    if code not in data.columns.get_level_values(0):
                        continue
                    df = data[code].copy()
                else:
                    df = data.copy()
                df = df[[c for c in _OHLCV_COLS if c in df.columns]].dropna(how="all")
                if df.empty:
                    continue
                df.index = pd.to_datetime(df.index).tz_localize(None)
                merged = _merge(_load_existing(code), df)
                _save(code, merged)
                done += 1
            except Exception as e:
                print(f"  [오류] {code}: {e}")
        if verbose:
            print(f"  해외 배치 {min(i + batch, len(codes))}/{len(codes)} (저장 {done})")
        time.sleep(1.0)        # 배치 간 rate-limit 완화
    return done


# ── Binance REST (비트코인) ───────────────────────────────────────────────────

def fetch_bitcoin() -> pd.DataFrame:
    symbol_name = "비트코인"
    existing = _load_existing(symbol_name)
    start_ts = (
        int((existing.index[-1] + timedelta(days=1)).timestamp() * 1000)
        if not existing.empty
        # CORE_FLOOR부터 요청 — Binance BTCUSDT는 2017-08~라 실제 시작은 소스 floor(정직).
        else int(datetime.strptime(CORE_FLOOR, "%Y-%m-%d").timestamp() * 1000)
    )

    url = "https://api.binance.com/api/v3/klines"
    rows, limit = [], 1000

    while True:
        try:
            data = requests.get(url, params={
                "symbol": "BTCUSDT", "interval": "1d",
                "startTime": start_ts, "limit": limit,
            }, timeout=15).json()
        except Exception as e:
            print(f"  [오류] 비트코인: {e}")
            break
        if not data or isinstance(data, dict):
            break
        for k in data:
            rows.append({
                "Date": pd.to_datetime(k[0], unit="ms"),
                "Open": float(k[1]), "High": float(k[2]),
                "Low":  float(k[3]), "Close": float(k[4]),
                "Volume": float(k[5]),
            })
        if len(data) < limit:
            break
        start_ts = data[-1][0] + 86_400_000
        time.sleep(0.2)

    if not rows:
        return existing
    new_df = pd.DataFrame(rows).set_index("Date")
    new_df.index = new_df.index.tz_localize(None)
    merged = _merge(existing, new_df)
    _save(symbol_name, merged)
    mark_data_dirty()           # 세대 신호 — 단독 호출 시 stale 캐시 방지
    return merged


# ── FRED (매크로 지표) ────────────────────────────────────────────────────────

def fetch_fred(symbol_name: str, series_id: str, start: str = CORE_FLOOR,
               lag_days: int = 0) -> pd.DataFrame:
    """FRED 시계열을 CSV로 직접 수집 (API 키 불필요). OHLCV 형식으로 저장.

    lag_days>0이면 발표지연만큼 인덱스를 뒤로 민다(월간 거시지표 룩어헤드 방지).
    지연 적용 시리즈는 증분 수집이 부정확하므로 매번 전체 수집한다(월간이라 가벼움).
    """
    existing = _load_existing(symbol_name)
    if not existing.empty and lag_days == 0:
        start = (existing.index[-1] + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
        resp = requests.get(url, timeout=20)
        raw = pd.read_csv(io.StringIO(resp.text))
        raw.columns = ["Date", "val"]
        raw["Date"] = pd.to_datetime(raw["Date"], errors="coerce")
        raw["val"]  = pd.to_numeric(raw["val"], errors="coerce")
        raw = raw.dropna().set_index("Date")
        if raw.empty:
            return existing
        val = raw["val"]
        df = pd.DataFrame({"Open": val, "High": val, "Low": val,
                           "Close": val, "Volume": 0.0})
        df.index = pd.to_datetime(df.index).tz_localize(None)
        if lag_days:
            df.index = df.index + pd.Timedelta(days=lag_days)
        merged = _merge(existing, df)
        _save(symbol_name, merged)
        mark_data_dirty()       # 세대 신호 — 단독 호출 시 stale 캐시 방지
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name} (FRED {series_id}): {e}")
        return existing


# ── 암호화폐 공포·탐욕지수 (alternative.me, API 키 불필요) ─────────────────────

def fetch_crypto_fng() -> pd.DataFrame:
    """alternative.me 암호화폐 공포·탐욕지수(0=극공포 ~ 100=극탐욕). 일간."""
    symbol_name = "암호화폐공포탐욕"
    existing = _load_existing(symbol_name)
    try:
        data = requests.get("https://api.alternative.me/fng/?limit=0&format=json",
                             timeout=20).json()
        rows = data.get("data", []) if isinstance(data, dict) else []
        recs = []
        for r in rows:
            try:
                ts = pd.to_datetime(int(r["timestamp"]), unit="s")
                recs.append({"Date": ts, "val": float(r["value"])})
            except (KeyError, ValueError, TypeError):
                continue
        if not recs:
            return existing
        raw = pd.DataFrame(recs).set_index("Date").sort_index()
        val = raw["val"]
        df = pd.DataFrame({"Open": val, "High": val, "Low": val,
                           "Close": val, "Volume": 0.0})
        df.index = pd.to_datetime(df.index).tz_localize(None)
        merged = _merge(existing, df)
        _save(symbol_name, merged)
        mark_data_dirty()       # 세대 신호 — 단독 호출 시 stale 캐시 방지
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name} (alternative.me): {e}")
        return existing


# ── 매크로 파생 지표 ──────────────────────────────────────────────────────────

def _build_derived(results: dict) -> dict:
    """수집된 시리즈로 매크로 파생 지표(비율)를 계산해 results에 추가·저장."""
    def _ratio(name: str, num: str, den: str):
        a, b = results.get(num), results.get(den)
        if a is None or b is None or a.empty or b.empty:
            return
        idx = a.index.intersection(b.index)
        if idx.empty:
            return
        r = (a.loc[idx, "Close"] / b.loc[idx, "Close"].replace(0, np.nan)).dropna()
        if r.empty:
            return
        df = pd.DataFrame({"Open": r, "High": r, "Low": r, "Close": r, "Volume": 0.0})
        _save(name, df)
        results[name] = df

    def _diff(name: str, a_name: str, b_name: str):
        """두 시리즈의 차이(a - b)를 OHLCV 형식으로 저장."""
        a, b = results.get(a_name), results.get(b_name)
        if a is None or b is None or a.empty or b.empty:
            return
        idx = a.index.intersection(b.index)
        if idx.empty:
            return
        d = (a.loc[idx, "Close"] - b.loc[idx, "Close"]).dropna()
        if d.empty:
            return
        df = pd.DataFrame({"Open": d, "High": d, "Low": d, "Close": d, "Volume": 0.0})
        _save(name, df)
        results[name] = df

    def _combine(name, a_name, b_name, op, pre_b=None):
        """주기가 다른 두 시리즈를 a의 인덱스에 ffill로 맞춰 결합한다.
        op: 'ratio'(a/b) 또는 'diff'(a-b). pre_b: b 시리즈 사전 변환 함수."""
        a, b = results.get(a_name), results.get(b_name)
        if a is None or b is None or a.empty or b.empty:
            return
        a_s = a["Close"]
        b_s = pre_b(b["Close"]) if pre_b else b["Close"]
        # b를 두 인덱스의 합집합에 reindex → ffill → a의 인덱스만 추출
        b_d = b_s.reindex(a_s.index.union(b_s.index)).ffill().reindex(a_s.index)
        if op == "ratio":
            r = (a_s / b_d.replace(0, np.nan)).dropna()
        else:
            r = (a_s - b_d).dropna()
        if r.empty:
            return
        df = pd.DataFrame({"Open": r, "High": r, "Low": r, "Close": r, "Volume": 0.0})
        _save(name, df)
        results[name] = df

    _ratio("VIX 기간구조", "VIX", "VIX 3개월")   # >1 = 백워데이션(스트레스)
    _ratio("구리금비율", "구리선물", "금선물")     # 상승 = 리플레이션
    # 신용 스프레드 = BAA(중간등급) - AAA(최우량) 회사채 금리차. 확대 = 신용경색
    _diff("회사채신용스프레드", "회사채BAA금리", "회사채AAA금리")
    # 버핏지수 = S&P500 ÷ GDP (시장 과열도 프록시; 윌셔5000이 FRED에서
    # 폐지돼 S&P500을 시장 대용으로 사용. GDP는 분기→일별 ffill)
    _combine("버핏지수", "S&P500", "GDP", "ratio")
    # 실질기준금리 = 실효기준금리 − CPI 전년동월비(%)
    _combine("실질기준금리", "실효기준금리", "CPI", "diff",
             pre_b=lambda s: s.pct_change(12) * 100)
    return results


def search_tickers(query: str, max_results: int = 8) -> list[dict]:
    """
    yfinance.Search로 티커를 검색합니다.
    한국 주식은 영문명 또는 종목코드(005930)로 검색.
    반환: [{ticker, name, exchange, type}, ...]
    """
    try:
        s = yf.Search(query.strip(), max_results=max_results)
        results = []
        for q in s.quotes:
            ticker = q.get("symbol", "")
            if not ticker:
                continue
            name = q.get("longname") or q.get("shortname") or ticker
            results.append({
                "ticker": ticker,
                "name":   name,
                "exchange": q.get("exchange", ""),
                "type":   q.get("quoteType", ""),
            })
        return results
    except Exception as e:
        print(f"  [검색 오류] {e}")
        return []


def load_stock_fundamentals(name: str) -> pd.DataFrame:
    """저장된 펀더멘털 parquet 로드."""
    p = _fund_path(name)
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)          # 손상 펀더멘털도 격리·skip → load_fund_all 전체 보호
    return df if df is not None else pd.DataFrame()


def load_stock_consensus(name: str) -> pd.DataFrame:
    """저장된 컨센서스 패널 parquet 로드 (reports_kr[네이버] 피드가 산출). 없으면 빈 DataFrame."""
    p = CONSENSUS_DIR / f"{name.replace('/', '_')}.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)
    return df if df is not None else pd.DataFrame()


def load_stock_reports(name: str) -> pd.DataFrame:
    """저장된 애널 리포트 목록 아카이브 parquet 로드 (reports_kr 피드, 네이버). 없으면 빈 DataFrame.

    컬럼: [nid, as_of(ISO), code, broker, title, url]. HOME 리포트 목록 서빙용(nid dedup 원시).
    """
    p = REPORTS_DIR / f"{name.replace('/', '_')}.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)
    return df if df is not None else pd.DataFrame()


def load_stock_flow(name: str) -> pd.DataFrame:
    """저장된 기관·외국인 수급 parquet 로드 (flow_kr 피드). 없으면 빈 DataFrame."""
    p = FLOW_DIR / f"{name.replace('/', '_')}.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)
    return df if df is not None else pd.DataFrame()


def fetch_user_stock(name: str, ticker: str, verbose: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """개별종목 가격 수집. 펀더멘털은 전용 피드(SEC US·OpenDART KR) cron이 담당한다."""
    if verbose:
        print(f"수집 중: {name} ({ticker})")
    price = fetch_stock_price(name, ticker)
    if verbose:
        print(f"  → 가격: {len(price)}행")
    return price, pd.DataFrame()


# ── 전체 수집 ────────────────────────────────────────────────────────────────

def fetch_all(verbose: bool = True) -> dict[str, pd.DataFrame]:
    results = {}

    for name, ticker in YFINANCE_SYMBOLS.items():
        if verbose: print(f"수집 중: {name} ({ticker})")
        results[name] = fetch_yfinance(name, ticker)
        time.sleep(0.3)

    for name, ticker in FDR_SYMBOLS.items():
        if verbose: print(f"수집 중: {name} ({ticker}, KRX ETF)")
        results[name] = fetch_fdr(name, ticker)
        time.sleep(0.3)

    if verbose: print("수집 중: 비트코인 (Binance)")
    results["비트코인"] = fetch_bitcoin()

    # ── 매크로 지표 ──────────────────────────────────────────────────────────
    for name, ticker in MACRO_YF_SYMBOLS.items():
        if verbose: print(f"수집 중: {name} ({ticker})")
        results[name] = fetch_yfinance(name, ticker)
        time.sleep(0.3)

    for name, series_id in MACRO_FRED_SYMBOLS.items():
        if verbose: print(f"수집 중: {name} (FRED {series_id})")
        results[name] = fetch_fred(name, series_id)
        time.sleep(0.2)

    for name, (series_id, lag) in MACRO_FRED_LAGGED.items():
        if verbose: print(f"수집 중: {name} (FRED {series_id}, 지연 {lag}일)")
        results[name] = fetch_fred(name, series_id, lag_days=lag)
        time.sleep(0.2)

    if verbose: print("수집 중: 암호화폐공포탐욕 (alternative.me)")
    results["암호화폐공포탐욕"] = fetch_crypto_fng()

    _build_derived(results)

    # 사용자 추가 종목 가격도 함께 업데이트
    for stock in load_user_stocks():
        results[stock["name"]] = fetch_stock_price(stock["name"], stock["ticker"])
        time.sleep(0.3)

    if verbose:
        print()
        for name, df in results.items():
            if not df.empty:
                print(f"  {name}: {len(df):,}행  {df.index[0].date()} ~ {df.index[-1].date()}")
            else:
                print(f"  {name}: 데이터 없음")

    mark_data_dirty()           # 데이터 변경 — 라이브 캐시 자가 리로드 신호
    return results


def load_all() -> dict[str, pd.DataFrame]:
    """저장된 parquet에서 전체 심볼 로드. 매크로/자산 + 사용자 종목 + 자동 관리 한국·해외 종목."""
    result = {}

    def _add(sym: str) -> None:
        p = _parquet_path(sym)
        if not p.exists():
            return
        df = read_parquet_safe(p)      # 손상 파일은 격리·skip → 한 종목이 전체 로드를 못 죽임
        if df is not None:
            result[sym] = df

    for symbol in ALL_SYMBOLS:
        _add(symbol)
    for symbol in PRICE_ALIAS:        # 미니 등 alias 심볼 — _add가 _parquet_path로 정규 시리즈 공유(I-1)
        _add(symbol)
    for stock in load_user_stocks():
        _add(stock["name"])
    # Phase 29: 자동 관리 한국 종목 (KIS 마스터 KOSPI/KOSDAQ union)
    for code in load_managed_kr_codes():
        _add(code)
    # Phase 29: on-demand 등록된 해외 종목
    for stock in load_managed_overseas():
        _add(stock["code"])
    return result


def load_fund_all() -> dict[str, pd.DataFrame]:
    """펀더멘털 parquet 로드. 키=dataset 키(사용자 이름 / 자동관리 KR 코드 / 해외 코드).

    펀더멘털은 전용 피드(SEC US·OpenDART KR)가 FUNDAMENTALS_DIR/{키}.parquet로 수급.
    """
    result = {}
    for stock in load_user_stocks():
        df = load_stock_fundamentals(stock["name"])
        if not df.empty:
            result[stock["name"]] = df
    for code in load_managed_kr_codes():
        df = load_stock_fundamentals(code)
        if not df.empty:
            result[code] = df
    for stock in load_managed_overseas():
        df = load_stock_fundamentals(stock["code"])
        if not df.empty:
            result[stock["code"]] = df
    return result


def load_consensus_all() -> dict[str, pd.DataFrame]:
    """컨센서스 패널 parquet 전체 로드 (KR 종목 한정 — 네이버 리포트 집계·reports_kr). 키=dataset 키.

    해외(US)는 컨센서스 없음 → managed_kr + user_stocks만 순회(파일 없으면 자연 skip).
    """
    result = {}
    for code in load_managed_kr_codes():
        df = load_stock_consensus(code)
        if not df.empty:
            result[code] = df
    for stock in load_user_stocks():
        df = load_stock_consensus(stock["name"])
        if not df.empty:
            result[stock["name"]] = df
    return result


def load_flow_all() -> dict[str, pd.DataFrame]:
    """기관·외국인 수급 parquet 전체 로드 (KR 종목 한정 — KRX 소스). 키=dataset 키."""
    result = {}
    for code in load_managed_kr_codes():
        df = load_stock_flow(code)
        if not df.empty:
            result[code] = df
    for stock in load_user_stocks():
        df = load_stock_flow(stock["name"])
        if not df.empty:
            result[stock["name"]] = df
    return result


def dataset_symbol_index() -> dict[str, dict]:
    """load_all과 동일한 심볼 집합을 **데이터 로드·지표계산 없이** parquet 메타(footer)만
    읽어 {sym: {"rows", "has_ohlc"}}로 반환.

    종목 목록 응답(/symbols)·참조 검증(/ir/validate)이 전체 지표계산(load_dataset,
    22k×compute_all ≈ 8.5분)에 묶이지 않도록 분리한 경량 인덱스. footer만 읽어
    DataFrame을 메모리에 적재하지 않는다(저메모리·고속)."""
    import pyarrow.parquet as pq
    names = (list(ALL_SYMBOLS)
             + list(PRICE_ALIAS)
             + [s["name"] for s in load_user_stocks()]
             + load_managed_kr_codes()
             + [s["code"] for s in load_managed_overseas()])
    out: dict[str, dict] = {}
    for sym in dict.fromkeys(names):           # 순서보존 dedupe
        p = _parquet_path(sym)
        if not p.exists():
            continue
        try:
            md = pq.read_metadata(p)
            cols = set(md.schema.names)
        except Exception as e:                  # noqa: BLE001 — 손상 parquet 격리+로그 후 skip
            quarantine_corrupt(p, e)
            continue
        out[sym] = {"rows": md.num_rows,
                    "has_ohlc": "Open" in cols and "Close" in cols}
    return out


if __name__ == "__main__":
    fetch_all()
