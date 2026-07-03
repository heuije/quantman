"""macro.cot 피드 — CFTC COT(Commitments of Traders) 포지셔닝 + US 선물 주간 OI.

소스: CFTC 공식 Socrata API(publicreporting.cftc.gov) **Legacy Futures-Only**(dataset
6dca-aqww) — 무키·무로그인 공개, 이력 1986~, 주간(화요일 마감 포지션을 금요일 15:30 ET 공개).
Legacy를 쓰는 이유: 상품(WTI·금)·금융(S&P·나스닥·비트코인) 선물이 **단일 스키마**
(noncomm/comm/open_interest)로 통일돼 있고 깊이가 최대(Disaggregated·TFF는 2006~).

시장당 1콜이면 전체 이력(~2,000 주간행)이 오므로 커서 불요 — **매번 전체 재수집·멱등
merge**(주 8콜, 무시 가능 비용). CFTC 정정(revision)도 재수집이 자연 흡수한다.

**PIT: index = 보고기준일(화) + 3일 = 공개일(금).** 화요일 포지션은 금요일까지 미공개라
보고일 인덱스 그대로 쓰면 3일 룩어헤드 — MACRO_FRED_LAGGED와 동일한 지연 반영 규약.

명명 시계열(코스피200선물미결제약정 패턴·시장당 2종 — 원시 직교 프리미티브):
  {시장}투기순포지션 = 비상업(투기) 롱 − 숏 (계약수. 상업순포지션≈−투기순포지션이라 별도 미적재)
  {시장}미결제약정   = open_interest_all (US 선물 유일 무료 OI — 일별 장기이력은 유료뿐)
'급증·전환·극단치'는 ts_delta·zscore 등 기존 시계열 연산자로 조합. 실패는 시장 단위
skip(부분 저장 없음·재시도), 무예외 빈 응답도 실패 취급(전체이력 쿼리가 비면 글리치).
"""

from __future__ import annotations

import pandas as pd
import requests

from ... import data_fetcher as _df

_BASE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
_RELEASE_LAG_DAYS = 3          # 화(보고기준) → 금(공개) — PIT 지연 반영

# 우리 US 선물 ↔ CFTC 계약코드 (2026-07-03 Socrata 라이브 검증 — 최신 보고 존재 확인)
_MARKETS: dict[str, str] = {
    "원유선물":     "067651",   # WTI-PHYSICAL - NYMEX
    "천연가스선물": "023651",   # NAT GAS NYME - NYMEX (Henry Hub)
    "금선물":       "088691",   # GOLD - COMEX
    "은선물":       "084691",   # SILVER - COMEX (가격심볼 '은선물(COMEX)'와 동일 기초자산)
    "구리선물":     "085692",   # COPPER- #1 - COMEX
    "나스닥선물":   "209742",   # NASDAQ MINI(E-mini NASDAQ-100) - CME
    "S&P500선물":   "13874A",   # E-MINI S&P 500 - CME (가격 프록시 ^GSPC의 선물 포지셔닝)
    "비트코인선물": "133741",   # BITCOIN - CME
}

_SPEC_SUFFIX = "투기순포지션"
_OI_SUFFIX = "미결제약정"

# 챗 카탈로그·spec·매니페스트가 소비하는 심볼 목록 (data_fetcher.MACRO_COT_SYMBOLS의 원천)
COT_SYMBOLS: list[str] = [m + s for m in _MARKETS for s in (_SPEC_SUFFIX, _OI_SUFFIX)]

_SELECT = ("report_date_as_yyyy_mm_dd,open_interest_all,"
           "noncomm_positions_long_all,noncomm_positions_short_all")


def _fetch_market(code: str) -> list[dict] | None:
    """한 시장의 전체 주간 이력. None=네트워크/HTTP 실패(호출자 skip·재시도)."""
    try:
        r = requests.get(_BASE, params={
            "$select": _SELECT, "cftc_contract_market_code": code,
            "$order": "report_date_as_yyyy_mm_dd ASC", "$limit": "50000",
        }, timeout=45)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        rows = r.json()
    except Exception:
        return None
    return rows if isinstance(rows, list) else None


def parse_rows(rows: list[dict]) -> pd.DataFrame:
    """Socrata 행 → [spec_net, oi] DataFrame(index=공개일=보고일+3d). 순수함수(픽스처 테스트).

    결측·비수치 행은 drop(가짜 0 금지). 중복 보고일은 마지막 값(정정 반영).
    """
    recs = []
    for r in rows:
        try:
            d = pd.to_datetime(str(r["report_date_as_yyyy_mm_dd"])[:10])
            long_ = float(r["noncomm_positions_long_all"])
            short = float(r["noncomm_positions_short_all"])
            oi = float(r["open_interest_all"])
        except (KeyError, TypeError, ValueError):
            continue
        recs.append({"date": d, "spec_net": long_ - short, "oi": oi})
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index = df.index + pd.Timedelta(days=_RELEASE_LAG_DAYS)   # PIT: 공개일로 지연
    return df


def _save_series(name: str, val: pd.Series) -> int:
    """값 시리즈 → 명명 parquet(OHLCV-format·매크로 패턴) merge-save. krx_openapi와 동형."""
    val = val.dropna().astype("float64")
    if val.empty:
        return 0
    df = pd.DataFrame({"Open": val, "High": val, "Low": val, "Close": val, "Volume": 0.0})
    df.index = pd.to_datetime(df.index).tz_localize(None)
    merged = _df._merge(_df._load_existing(name), df)
    _df._save(name, merged)
    return len(val)


def fetch_all(markets: dict[str, str] | None = None, fetch=_fetch_market) -> dict:
    """전 시장 COT 수집·merge-save. 시장 단위 원자성(실패 시장은 두 시리즈 다 skip).

    반환 {"ok", "markets", "fail", "saved": {심볼: n}}. fetch 주입 가능(테스트).
    """
    markets = _MARKETS if markets is None else markets
    saved: dict[str, int] = {}
    fail = 0
    for name, code in markets.items():
        rows = fetch(code)
        df = parse_rows(rows) if rows else pd.DataFrame()
        if df.empty:                       # 실패·빈 응답 = 시장 skip(전체이력 쿼리가 비면 글리치)
            fail += 1
            continue
        saved[name + _SPEC_SUFFIX] = _save_series(name + _SPEC_SUFFIX, df["spec_net"])
        saved[name + _OI_SUFFIX] = _save_series(name + _OI_SUFFIX, df["oi"])
    if any(saved.values()):
        _df.mark_data_dirty()
    return {"ok": fail == 0, "markets": len(markets) - fail, "fail": fail, "saved": saved}
