"""
Data fetcher
  - yfinance   : S&P500, 원유선물, 천연가스선물, 금선물, 개별종목(US/KR)
  - FinanceDataReader : 코스피200선물ETF(261220), 나스닥100선물ETF(304940), 은선물ETF(144600)
  - Binance REST: 비트코인
  - yfinance quarterly financials : 개별종목 펀더멘털
"""

import io
import json
import os
import time
import requests
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
import FinanceDataReader as fdr

from pathlib import Path
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

# 데이터 저장 위치 — 환경변수로 덮어쓸 수 있다(로컬앱은 사용자 디렉터리를 가리킴).
DATA_DIR = Path(os.getenv("QP_CORE_DATA_DIR")
                or Path(__file__).parent.parent / "data")
FUNDAMENTALS_DIR = DATA_DIR / "fundamentals"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

USER_STOCKS_PATH = DATA_DIR / "user_stocks.json"

# ── 기본 종목 정의 ────────────────────────────────────────────────────────────

# 자산 (가격 시계열)
YFINANCE_SYMBOLS = {
    "S&P500":      "^GSPC",
    "원유선물":     "CL=F",
    "천연가스선물": "NG=F",
    "금선물":       "GC=F",
}

FDR_SYMBOLS = {
    "코스피200선물": "261220",
    "나스닥100선물": "304940",
    "은선물":        "144600",
}

# 매크로 지표 — yfinance
MACRO_YF_SYMBOLS = {
    "VIX":        "^VIX",
    "VIX 3개월":  "^VIX3M",
    "MOVE 지수":  "^MOVE",
    "SKEW 지수":  "^SKEW",
    "VVIX":       "^VVIX",
    "달러지수":    "DX-Y.NYB",
    "구리선물":    "HG=F",
    "미국채10년":  "^TNX",
}

# 매크로 지표 — FRED (https://fred.stlouisfed.org, API 키 불필요)
MACRO_FRED_SYMBOLS = {
    "장단기금리차10Y2Y": "T10Y2Y",
    "장단기금리차10Y3M": "T10Y3M",
    "하이일드스프레드":   "BAMLH0A0HYM2",
    "투자등급스프레드":   "BAMLC0A0CM",
    "금융여건지수":       "NFCI",
    # 금리·신용 일간(daily) 시리즈 — 발표지연/룩어헤드 없음
    "미국채2년":          "DGS2",
    "미국채30년":         "DGS30",
    "기대인플레이션10년":  "T10YIE",
    "실효기준금리":        "DFF",
    "회사채AAA금리":       "DAAA",
    "회사채BAA금리":       "DBAA",
}

# 매크로 파생 지표 (수집한 시리즈로 계산)
MACRO_DERIVED = ["VIX 기간구조", "구리금비율", "회사채신용스프레드"]

ASSET_SYMBOLS = list(YFINANCE_SYMBOLS) + list(FDR_SYMBOLS) + ["비트코인"]
MACRO_SYMBOLS = list(MACRO_YF_SYMBOLS) + list(MACRO_FRED_SYMBOLS) + MACRO_DERIVED
ALL_SYMBOLS = ASSET_SYMBOLS + MACRO_SYMBOLS


# ── 공통 유틸 ────────────────────────────────────────────────────────────────

def _parquet_path(symbol: str) -> Path:
    return DATA_DIR / f"{symbol.replace('/', '_')}.parquet"

def _fund_path(name: str) -> Path:
    return FUNDAMENTALS_DIR / f"{name.replace('/', '_')}.parquet"

def _load_existing(symbol: str) -> pd.DataFrame:
    p = _parquet_path(symbol)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()

def _save(symbol: str, df: pd.DataFrame):
    if df.empty:
        return
    df = df.sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.to_parquet(_parquet_path(symbol))

def _merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return new
    if new.empty:
        return existing
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


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

def fetch_fred(symbol_name: str, series_id: str, start: str = "2010-01-01") -> pd.DataFrame:
    """FRED 시계열을 CSV로 직접 수집 (API 키 불필요). OHLCV 형식으로 저장."""
    existing = _load_existing(symbol_name)
    if not existing.empty:
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
        merged = _merge(existing, df)
        _save(symbol_name, merged)
        return merged
    except Exception as e:
        print(f"  [오류] {symbol_name} (FRED {series_id}): {e}")
        return existing


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

    _ratio("VIX 기간구조", "VIX", "VIX 3개월")   # >1 = 백워데이션(스트레스)
    _ratio("구리금비율", "구리선물", "금선물")     # 상승 = 리플레이션
    # 신용 스프레드 = BAA(중간등급) - AAA(최우량) 회사채 금리차. 확대 = 신용경색
    _diff("회사채신용스프레드", "회사채BAA금리", "회사채AAA금리")
    return results


# ── 개별종목 펀더멘털 ─────────────────────────────────────────────────────────

def fetch_stock_fundamentals(name: str, ticker: str) -> pd.DataFrame:
    """
    yfinance 분기 재무제표로부터 펀더멘털 지표를 계산합니다.
    분기 데이터에 45일 지연을 적용해 look-ahead bias를 최소화합니다.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        try:    inc = t.quarterly_income_stmt
        except: inc = pd.DataFrame()
        try:    bal = t.quarterly_balance_sheet
        except: bal = pd.DataFrame()
        try:    cf  = t.quarterly_cashflow
        except: cf  = pd.DataFrame()

        if inc.empty and bal.empty:
            return pd.DataFrame()

        def get_row(df: pd.DataFrame, *keys) -> pd.Series:
            for k in keys:
                if k in df.index:
                    return df.loc[k]
            return pd.Series(dtype=float)

        def qval(series: pd.Series, date) -> float:
            if series.empty or date not in series.index:
                return np.nan
            v = series[date]
            return float(v) if not pd.isna(v) else np.nan

        def ttm_sum(series: pd.Series, prior_dates: list) -> float:
            """TTM 합계 (가능한 분기 수로 연율화)."""
            vals = []
            for d in prior_dates:
                if d in series.index:
                    v = series[d]
                    if not pd.isna(v):
                        vals.append(float(v))
            if not vals:
                return np.nan
            return sum(vals) * (4.0 / len(vals))   # 분기수로 연율화

        # 각 재무 항목 시리즈 추출
        rev_s    = get_row(inc, "Total Revenue", "Revenue")
        gp_s     = get_row(inc, "Gross Profit")
        ebit_s   = get_row(inc, "EBIT", "Operating Income")
        ebitda_s = get_row(inc, "EBITDA", "Normalized EBITDA")
        ni_s     = get_row(inc, "Net Income")

        ta_s   = get_row(bal, "Total Assets")
        tl_s   = get_row(bal, "Total Liabilities Net Minority Interest", "Total Liabilities")
        ca_s   = get_row(bal, "Current Assets")
        cl_s   = get_row(bal, "Current Liabilities")
        cash_s = get_row(bal, "Cash And Cash Equivalents",
                         "Cash Cash Equivalents And Short Term Investments")
        re_s   = get_row(bal, "Retained Earnings")
        td_s   = get_row(bal, "Total Debt", "Long Term Debt")
        eq_s   = get_row(bal, "Common Stock Equity", "Stockholders Equity",
                         "Total Stockholders Equity", "Total Equity Gross Minority Interest")

        fcf_s = get_row(cf, "Free Cash Flow")

        shares_out = float(info.get("sharesOutstanding") or np.nan)

        # 모든 분기 날짜 수집 (오름차순)
        all_dates: list = []
        for stmt in [inc, bal, cf]:
            if not stmt.empty:
                all_dates.extend(stmt.columns.tolist())
        all_dates = sorted(set(all_dates))

        if not all_dates:
            return pd.DataFrame()

        def safe_div(a, b):
            if pd.isna(a) or pd.isna(b) or b == 0:
                return np.nan
            return a / b

        rows = []
        for i, qdate in enumerate(all_dates):
            prior = all_dates[max(0, i - 3): i + 1]   # 최대 4분기 (TTM)

            rev  = qval(rev_s,  qdate)
            gp   = qval(gp_s,   qdate)
            ebit = qval(ebit_s, qdate)
            ta   = qval(ta_s,   qdate)
            tl   = qval(tl_s,   qdate)
            ca   = qval(ca_s,   qdate)
            cl   = qval(cl_s,   qdate)
            cash = qval(cash_s, qdate)
            re   = qval(re_s,   qdate)
            td   = qval(td_s,   qdate)
            eq   = qval(eq_s,   qdate)

            t_rev   = ttm_sum(rev_s,    prior)
            t_ebit  = ttm_sum(ebit_s,   prior)
            t_ebitda = ttm_sum(ebitda_s, prior)
            t_ni    = ttm_sum(ni_s,     prior)
            t_fcf   = ttm_sum(fcf_s,    prior)

            eff_ebitda = t_ebitda if not pd.isna(t_ebitda) else t_ebit
            wc       = (ca - cl) if not (pd.isna(ca) or pd.isna(cl)) else np.nan
            net_debt = (td - cash) if not (pd.isna(td) or pd.isna(cash)) else np.nan
            td_safe  = 0.0 if pd.isna(td) else td
            cash_safe = 0.0 if pd.isna(cash) else cash
            ic       = (eq + td_safe - cash_safe) if not pd.isna(eq) else np.nan
            nopat    = t_ebit * 0.80 if not pd.isna(t_ebit) else np.nan

            rows.append({
                "date":             qdate,
                "gross_margin":     safe_div(gp, rev) * 100,
                "op_margin":        safe_div(ebit, rev) * 100,
                "net_debt_ebitda":  safe_div(net_debt, eff_ebitda),
                "roic":             safe_div(nopat, ic) * 100,
                "ttm_rev":          t_rev,
                "ttm_ebit":         t_ebit,
                "ttm_ebitda":       eff_ebitda,
                "ttm_ni":           t_ni,
                "ttm_fcf":          t_fcf,
                "total_debt":       td,
                "cash":             cash,
                "total_assets":     ta,
                "total_liabilities": tl,
                "working_capital":  wc,
                "retained_earnings": re,
                "stockholders_equity": eq,
                "shares_outstanding": shares_out,
                # Altman Z 구성 요소 (시가총액은 가격 데이터와 결합 시 계산)
                "z_wc_ta":   safe_div(wc,    ta),
                "z_re_ta":   safe_div(re,    ta),
                "z_ebit_ta": safe_div(t_ebit, ta),
                "z_tl":      tl,
                "z_rev_ta":  safe_div(t_rev, ta),
            })

        if not rows:
            return pd.DataFrame()

        fund_df = pd.DataFrame(rows).set_index("date")
        fund_df.index = pd.to_datetime(fund_df.index).tz_localize(None)
        # 45일 공시 지연 적용 (look-ahead bias 방지)
        fund_df.index = fund_df.index + pd.Timedelta(days=45)
        fund_df = fund_df.sort_index()

        fund_df.to_parquet(_fund_path(name))
        return fund_df

    except Exception as e:
        print(f"  [펀더멘털 오류] {name} ({ticker}): {e}")
        return pd.DataFrame()


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
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def fetch_user_stock(name: str, ticker: str, verbose: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """개별종목 가격 + 펀더멘털 수집."""
    if verbose:
        print(f"수집 중: {name} ({ticker})")
    price = fetch_stock_price(name, ticker)
    if verbose:
        print(f"  → 가격: {len(price)}행")
    fund = fetch_stock_fundamentals(name, ticker)
    if verbose:
        if not fund.empty:
            print(f"  → 펀더멘털: {len(fund)}분기")
        else:
            print(f"  → 펀더멘털: 없음 (ETF·코인·데이터 부족)")
    return price, fund


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

    return results


def load_all() -> dict[str, pd.DataFrame]:
    """저장된 parquet에서 전체 심볼(기본+사용자 종목) 로드."""
    result = {}
    for symbol in ALL_SYMBOLS:
        p = _parquet_path(symbol)
        if p.exists():
            result[symbol] = pd.read_parquet(p)
    for stock in load_user_stocks():
        p = _parquet_path(stock["name"])
        if p.exists():
            result[stock["name"]] = pd.read_parquet(p)
    return result


def load_fund_all() -> dict[str, pd.DataFrame]:
    """사용자 종목의 펀더멘털 parquet 전부 로드."""
    result = {}
    for stock in load_user_stocks():
        df = load_stock_fundamentals(stock["name"])
        if not df.empty:
            result[stock["name"]] = df
    return result


if __name__ == "__main__":
    fetch_all()
