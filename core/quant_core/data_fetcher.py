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

from .parquet_io import read_parquet_safe, write_parquet_atomic, quarantine_corrupt

warnings.filterwarnings("ignore")

# 데이터 저장 위치 — 환경변수로 덮어쓸 수 있다(로컬앱은 사용자 디렉터리를 가리킴).
DATA_DIR = Path(os.getenv("QP_CORE_DATA_DIR")
                or Path(__file__).parent.parent / "data")
FUNDAMENTALS_DIR = DATA_DIR / "fundamentals"
CONSENSUS_DIR = DATA_DIR / "consensus"      # 애널 컨센서스 패널 (consensus_kr 피드, KR 한정)
FLOW_DIR = DATA_DIR / "flow"                # 기관·외국인 수급 (flow_kr 피드, KR 한정)
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
}

FDR_SYMBOLS = {
    # ⚠ 261220은 KODEX200선물 *ETF*(주식, ~₩만대) — 실제 KOSPI200 선물 계약이 아니다.
    # 실선물(지수포인트·승수 250,000)은 "코스피200선물" 키로 CSV 백필+KIS 증분 수급(CSV_SEEDED_FUTURES).
    # 둘이 같은 키를 쓰던 충돌(ETF에 선물 승수 적용)을 분리(F1) — ETF는 ETF 키로.
    "코스피200선물ETF": "261220",
    "나스닥100선물": "304940",
    "은선물":        "144600",
}

# CSV(투자닷컴) 콜드스타트 백필 + KIS 일봉 증분으로 수급하는 실선물(연속 일봉). FDR/yfinance
# 미수집(무료 연속선물 피드 부재) — seed_from_investing_csv로 시드, append_daily_bars로 증분.
CSV_SEEDED_FUTURES = ["코스피200선물"]

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
    "미국채10년":    "^TNX",
}

# 매크로 지표 — FRED (https://fred.stlouisfed.org, API 키 불필요)
# 일간/주간 시리즈 — 당일~익일 공시라 발표지연/룩어헤드 보정 불필요
MACRO_FRED_SYMBOLS = {
    "장단기금리차10Y2Y": "T10Y2Y",
    "장단기금리차10Y3M": "T10Y3M",
    "하이일드스프레드":   "BAMLH0A0HYM2",
    "투자등급스프레드":   "BAMLC0A0CM",
    "금융여건지수":       "NFCI",
    # 금리·신용 일간 시리즈
    "미국채2년":          "DGS2",
    "미국채30년":         "DGS30",
    "기대인플레이션10년":  "T10YIE",
    "실효기준금리":        "DFF",
    "회사채AAA금리":       "DAAA",
    "회사채BAA금리":       "DBAA",
    # 그룹 A-1 — 추가 금리·환율 일간 시리즈
    "미국채3개월":        "DGS3MO",
    "미국채5년":          "DGS5",
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

ASSET_SYMBOLS = list(YFINANCE_SYMBOLS) + list(FDR_SYMBOLS) + ["비트코인"] + CSV_SEEDED_FUTURES
MACRO_SYMBOLS = (list(MACRO_YF_SYMBOLS) + list(MACRO_FRED_SYMBOLS)
                 + list(MACRO_FRED_LAGGED) + MACRO_OTHER + MACRO_DERIVED)
ALL_SYMBOLS = ASSET_SYMBOLS + MACRO_SYMBOLS

# 종목 카테고리 — 조건 빌더 UI에서 종목 목록을 그룹화하기 위한 분류.
# 의미 기준 분류(수집 소스와 무관). 미등재 종목(사용자 추가)은 "개별종목".
SYMBOL_CATEGORY: dict[str, str] = {
    # 자산
    "S&P500": "자산", "원유선물": "자산", "천연가스선물": "자산", "금선물": "자산",
    "코스피200선물": "자산", "미니코스피200선물": "자산", "코스피200선물ETF": "자산",
    "나스닥100선물": "자산", "은선물": "자산",
    "구리선물": "자산", "비트코인": "자산",
    "나스닥선물": "자산", "은선물(COMEX)": "자산", "비트코인선물": "자산",
    # 변동성
    "VIX": "변동성", "VIX 3개월": "변동성", "VIX 9일": "변동성", "VVIX": "변동성",
    "MOVE 지수": "변동성", "SKEW 지수": "변동성", "나스닥변동성": "변동성",
    "VIX 기간구조": "변동성",
    # 금리·환율
    "미국채2년": "금리·환율", "미국채3개월": "금리·환율", "미국채5년": "금리·환율",
    "미국채10년": "금리·환율", "미국채30년": "금리·환율",
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


# ── 실선물 일봉 수급: CSV 콜드스타트 백필 + 증분 append (KOSPI200 선물 등) ──────────
# KOSPI200은 무료 연속선물 피드가 없어, 투자닷컴 과거데이터 CSV로 깊은 과거(2010+)를 1회
# 시드하고, 이후 일봉은 KIS(FHKIF03020100, 모의 OK)가 단일 소스로 증분 append한다.
# 날짜로 소스를 분할(CSV=과거·KIS=신규, 중복은 KIS 우선) → "데이터포인트당 소스 1개" 준수.

def _parse_investing_volume(v) -> float:
    """investing.com 거래량 '18.45K'·'175.97K'·'2.3M' → 숫자. 빈값/'-'/NaN → 0."""
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "nan", "NaN", "None"):
        return 0.0
    mult = 1.0
    if s[-1] in "KkMmBb":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def clean_investing_csv(csv_path) -> pd.DataFrame:
    """investing.com 과거데이터 CSV → 정제 OHLCV(오름차순 DatetimeIndex).

    내보내기 특이점 처리: 날짜 내부 공백('2026- 06- 02'), 천단위 콤마('1,434.95'),
    거래량 K/M 접미사, 최신→과거 역순. 컬럼: 날짜·종가·시가·고가·저가·거래량(·변동%).
    """
    raw = pd.read_csv(csv_path, encoding="utf-8-sig")
    raw = raw.rename(columns={"날짜": "date", "종가": "Close", "시가": "Open",
                              "고가": "High", "저가": "Low", "거래량": "Volume"})
    # raw는 RangeIndex, 새 index는 DatetimeIndex라 Series 그대로 넣으면 라벨정렬로 전부 NaN이
    # 된다 → 전부 .to_numpy()로 위치기반 배정·필터(인덱스 비정렬 오류도 회피).
    idx = pd.to_datetime(raw["date"].astype(str).str.replace(" ", "", regex=False),
                         errors="coerce").to_numpy()

    def _num(col):
        return pd.to_numeric(raw[col].astype(str).str.replace(",", "", regex=False),
                             errors="coerce").to_numpy()

    close = _num("Close")
    vol = (raw["Volume"].map(_parse_investing_volume).to_numpy()
           if "Volume" in raw.columns else [0.0] * len(raw))
    df = pd.DataFrame({"Open": _num("Open"), "High": _num("High"), "Low": _num("Low"),
                       "Close": close, "Volume": vol}, index=idx)
    df = df[(~pd.isna(idx)) & (~pd.isna(close))].sort_index()
    df.index.name = None
    return df


def seed_from_investing_csv(symbol: str, csv_path) -> pd.DataFrame:
    """investing.com CSV로 콜드스타트 백필(1회). 기존 parquet을 **덮어쓴다**(프록시 데이터 교체).

    실선물 연속 일봉이라 엔진이 그대로 소비(별도 롤 데이터 불필요 — 롤은 시리즈에 내재).
    이후 일봉은 append_daily_bars(KIS 증분)가 단일 소스.
    """
    df = clean_investing_csv(csv_path)
    if df.empty:
        raise ValueError(f"CSV 정제 결과가 비어 있음: {csv_path}")
    _save(symbol, df)            # 덮어쓰기 — 기존(프록시 ETF) 데이터를 실선물로 교체
    mark_data_dirty()
    return df


def read_clean_csv(csv_path) -> pd.DataFrame:
    """표준화 OHLCV CSV(date,open,high,low,close[,volume]; 오름차순) → 정제 DataFrame.

    시드(seed_from_clean_csv)와 공백 점검(csv_coverage_gap)이 *동일 파싱*을 쓰도록 분리.
    clean_investing_csv는 *원시* 투자닷컴 포맷(한글헤더·날짜공백·K접미사)용 — 이 함수는 *이미
    표준화된* CSV(번들 server/app/data/static/kospi200_futures.csv)용.
    """
    raw = pd.read_csv(csv_path)
    raw = raw.rename(columns={"date": "d", "open": "Open", "high": "High",
                              "low": "Low", "close": "Close", "volume": "Volume"})
    # raw는 RangeIndex, 새 index는 DatetimeIndex라 Series 그대로 넣으면 라벨정렬로 NaN → .to_numpy() 위치배정.
    idx = pd.to_datetime(raw["d"].astype(str), errors="coerce").to_numpy()

    def _num(col):
        return (pd.to_numeric(raw[col], errors="coerce").to_numpy()
                if col in raw.columns else [0.0] * len(raw))

    close = _num("Close")
    df = pd.DataFrame({"Open": _num("Open"), "High": _num("High"), "Low": _num("Low"),
                       "Close": close, "Volume": _num("Volume")}, index=idx)
    df = df[(~pd.isna(idx)) & (~pd.isna(close))].sort_index()
    df["Volume"] = df["Volume"].fillna(0.0)
    df.index.name = None
    return df


def seed_from_clean_csv(symbol: str, csv_path) -> pd.DataFrame:
    """정제완료 OHLCV CSV → parquet **덮어쓰기** 시드. 번들 정적 CSV(투자닷컴 KOSPI200 연속선물
    2010+ 스냅샷)를 깊은 base로 적재. 이후 최근분은 fetch_kis_futures_daily(KIS 증분)가 이 base
    위에 append → "데이터포인트당 소스 1개"(과거=CSV·신규=KIS).
    """
    df = read_clean_csv(csv_path)
    if df.empty:
        raise ValueError(f"정제 CSV 결과가 비어 있음: {csv_path}")
    _save(symbol, df)            # 덮어쓰기 — 기존(얕은 KIS·프록시 ETF·혼합·구멍난) 데이터를 깊은 연속물로 교체
    mark_data_dirty()
    return df


def csv_coverage_gap(existing: pd.DataFrame, csv_path, *, tol: float = 0.99) -> bool:
    """parquet이 번들 CSV 대비 **내부 공백**(결손)이 있나 — 재시드 트리거 판정.

    _refresh의 needs_deep가 min-date(깊이)만 보면 '깊지만 구멍난' parquet(예: 2021/2023/2024
    구간 결손)이 영구 미복구된다 — 선물분석 탭은 CSV를 직독해 멀쩡한데 인사이트 엔진만 거래0.
    CSV 구간[min,max] 안에서 parquet의 유효(non-NaN Close) 행이 CSV의 tol배 미만이면 True →
    CSV 재시드 필요. KIS 증분은 CSV 구간 *이후*만 append하므로, 정상이면 [csv.min,csv.max]
    안은 parquet=CSV로 정확히 일치(tol 여유는 휴장일 미세차 흡수).
    """
    if existing is None or existing.empty or "Close" not in existing.columns:
        return True
    csv_df = read_clean_csv(csv_path)
    overlap = csv_df.index[csv_df.index <= existing.index.max()]
    if len(overlap) == 0:
        return False
    have = int(existing["Close"].reindex(overlap).notna().sum())
    return have < len(overlap) * tol


def append_daily_bars(symbol: str, new: pd.DataFrame) -> pd.DataFrame:
    """신규 일봉을 기존 parquet에 증분 머지(중복 날짜는 new=최신 소스 우선). KIS 증분 진입점.

    new = OHLCV(+Volume) DatetimeIndex DataFrame(예: KIS FHKIF03020100 일봉을 대문자 OHLCV로
    매핑한 것). 스플라이스 정합(예: |new 첫 바 ≈ 기존 마지막 바|)은 호출자가 검증해 점프를 차단한다.
    """
    if new is None or new.empty:
        return _load_existing(symbol)
    new = new.copy()
    new.index = pd.to_datetime(new.index).tz_localize(None)
    merged = _merge(_load_existing(symbol), new)
    _save(symbol, merged)
    mark_data_dirty()
    return merged


def kis_futures_daily_to_ohlcv(output2) -> pd.DataFrame:
    """KIS FHKIF03020100 output2[](기간별 조회데이터) → OHLCV(오름차순 DatetimeIndex).

    필드(KIS 공식 spec, 국내선물옵션_기본시세.xlsx): stck_bsop_date(YYYYMMDD)·futs_oprc(시)·
    futs_hgpr(고)·futs_lwpr(저)·futs_prpr(현재가=종가)·acml_vol(거래량). 예: futs_prpr "344.70".
    """
    rows = []
    for r in output2 or []:
        d = str(r.get("stck_bsop_date", "")).strip()
        if len(d) != 8 or not d.isdigit():
            continue
        try:
            rows.append((pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]}"),
                         float(r["futs_oprc"]), float(r["futs_hgpr"]),
                         float(r["futs_lwpr"]), float(r["futs_prpr"]),
                         float(r.get("acml_vol", 0) or 0)))
        except (KeyError, ValueError, TypeError):
            continue
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = (pd.DataFrame(rows, columns=["d"] + cols)
          .set_index("d").sort_index())
    df.index.name = None
    return df


def fetch_kis_futures_daily(symbol: str, request_fn, *, iscd: str,
                            start: str, end: str, market: str = "F") -> pd.DataFrame:
    """KIS FHKIF03020100(국내선물옵션 일봉, 모의 OK)로 일봉 fetch → 증분 append.

    request_fn(tr_id, params)->dict(JSON)을 주입한다 — KIS 자격증명·인증·HTTP는 호출자(로컬 KIS
    환경)가 제공하고, core는 KIS 클라이언트에 비의존. iscd=선물 종목코드(지수선물 6자리, 현행 최근
    월물), start/end=YYYYMMDD. 반환 = append 후 머지된 전체 시리즈.

    GET /uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice
    ⚠ 검증범위: 요청 파라미터·output2 매핑은 KIS 공식 spec 기준 + 모의 응답으로 단위검증됨.
    실제 KIS HTTP 호출(인증·현행 최근월물 종목코드 결정)은 KIS 연결 시점에 검증 필요.
    """
    resp = request_fn("FHKIF03020100", {
        "FID_COND_MRKT_DIV_CODE": market,   # F: 지수선물, O: 지수옵션
        "FID_INPUT_ISCD": iscd,
        "FID_INPUT_DATE_1": start,          # 조회 시작일 (YYYYMMDD)
        "FID_INPUT_DATE_2": end,            # 조회 종료일 (YYYYMMDD)
        "FID_PERIOD_DIV_CODE": "D",         # D:일봉
    })
    df = kis_futures_daily_to_ohlcv((resp or {}).get("output2") or [])
    # PIT 불변식 — 호출자가 정한 end(확정 세션)까지만. KIS가 범위 밖(형성 중 당일 봉)을
    # 돌려줘도 canonical dataset에 넣지 않는다 — 미확정 봉이 들어가면 '전일 종가'(iloc[-1])
    # 의미가 깨져 preview 기준가·백테스트가 장중 값을 본다(2026-06-11 만기일 인시던트 F1).
    if not df.empty:
        df = df[df.index <= pd.Timestamp(end)]
    if df.empty:
        return _load_existing(symbol)
    return append_daily_bars(symbol, df)


def seed_kis_futures_full(symbol: str, request_fn, iscd: str, *, floor: str = "20100101",
                          market: str = "F", max_calls: int = 60) -> pd.DataFrame:
    """KIS FHKIF03020100을 과거로 거슬러 페이지네이션해 최대 깊이 일봉을 모아 **덮어쓰기** 시드.

    KIS 일봉은 호출당 제한(~100건)이라 날짜 윈도우를 거슬러 반복(floor까지/더 받을 게 없을 때까지).
    기존 parquet(ETF orphan·스케일 혼합 등)을 *통째 교체* → 혼합 오염 원천 차단. KIS 보관 깊이만큼
    획득(얕으면 그만큼만; 깊은 과거는 별도 CSV 시드로 보완). request_fn은 호출자(KIS 자격증명 보유)가 주입.
    """
    from datetime import datetime, timedelta
    frames: list[pd.DataFrame] = []
    seen: set = set()
    end_dt = datetime.now()
    for _ in range(max_calls):
        start = max((end_dt - timedelta(days=140)).strftime("%Y%m%d"), floor)
        end = end_dt.strftime("%Y%m%d")
        if start > end:
            break
        resp = request_fn("FHKIF03020100", {
            "FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": iscd,
            "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"})
        df = kis_futures_daily_to_ohlcv((resp or {}).get("output2") or [])
        new = df[~df.index.isin(seen)]
        if new.empty:                       # 더 받을 과거 없음 → 종료
            break
        frames.append(new)
        seen.update(new.index)
        earliest = new.index.min()
        if earliest.strftime("%Y%m%d") <= floor:
            break
        end_dt = earliest - timedelta(days=1)
    if not frames:
        return _load_existing(symbol)
    full = pd.concat(frames)
    full = full[~full.index.duplicated(keep="first")].sort_index()
    _save(symbol, full)                     # 덮어쓰기 — orphan/혼합 제거
    mark_data_dirty()
    return full


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

def fetch_yfinance(symbol_name: str, ticker: str, start: str = "2010-01-01") -> pd.DataFrame:
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
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name}: {e}")
        return existing


def fetch_stock_price(name: str, ticker: str, start: str = "2000-01-01") -> pd.DataFrame:
    """개별종목 가격 데이터 수집 (yfinance 래퍼)."""
    return fetch_yfinance(name, ticker, start)


# ── FinanceDataReader (KRX ETF) ───────────────────────────────────────────────

def fetch_fdr(symbol_name: str, ticker: str, start: str = "2010-01-01") -> pd.DataFrame:
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
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name}: {e}")
        return existing


def fetch_korean_stocks(codes: list[str], start: str = "2015-01-01",
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
        start: 새 종목 첫 fetch 시 시작일. 기존 parquet 있으면 무시되고 이어받음.
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
                           batch: int = 200, backfill_start: str = "2015-01-01") -> int:
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
        else int(datetime(2015, 1, 1).timestamp() * 1000)
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
    return merged


# ── FRED (매크로 지표) ────────────────────────────────────────────────────────

def fetch_fred(symbol_name: str, series_id: str, start: str = "2010-01-01",
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
    """저장된 컨센서스 패널 parquet 로드 (consensus_kr 피드). 없으면 빈 DataFrame."""
    p = CONSENSUS_DIR / f"{name.replace('/', '_')}.parquet"
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
    """컨센서스 패널 parquet 전체 로드 (KR 종목 한정 — 한경 소스). 키=dataset 키.

    해외(US)는 한경 컨센서스 없음 → managed_kr + user_stocks만 순회(파일 없으면 자연 skip).
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
