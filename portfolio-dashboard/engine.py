# -*- coding: utf-8 -*-
"""데이터 엔진: 시세 조회 → 평가금액·수익률·베타·상관계수·초과수익률·세후수익률 계산.

설계 원칙
- 입력은 holdings.csv 한 곳. 나머지는 전부 자동 계산.
- 시세 조회는 KRX 주식/ETF 모두 pykrx.get_market_ohlcv 로 통일(이 환경에서 가장 안정적).
- 조회 결과는 cache/ 에 당일 1회만 저장 → 재실행/야후 rate-limit 대비.
- 증권사 API(KIS 등)를 나중에 붙여도 이 파일의 계산 함수는 그대로 재사용.
"""
import os
import datetime as dt
import numpy as np
import pandas as pd

import config
import quant

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
os.makedirs(CACHE, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────
# 캐시 유틸: 같은 날 이미 받은 시세는 다시 안 받는다.
# ──────────────────────────────────────────────────────────────────────
def _cache_path(key: str) -> str:
    today = dt.date.today().strftime("%Y%m%d")
    return os.path.join(CACHE, f"{key}_{today}.csv")


def _read_cache(key: str):
    p = _cache_path(key)
    if os.path.exists(p):
        try:
            s = pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0]
            if len(s) > 0:
                return s
        except Exception:
            pass
    return None


def _write_cache(key: str, s: pd.Series):
    try:
        s.to_frame("close").to_csv(_cache_path(key))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# 시세 조회 (OHLCV: 고가/저가/종가/거래량 → 다요인 지표 계산용)
# ──────────────────────────────────────────────────────────────────────
def _read_ohlcv_cache(key: str):
    p = _cache_path(key)
    if os.path.exists(p):
        try:
            df = pd.read_csv(p, index_col=0, parse_dates=True)
            if len(df):
                return df
        except Exception:
            pass
    return None


def _write_ohlcv_cache(key: str, df: pd.DataFrame):
    try:
        df.to_csv(_cache_path(key))
    except Exception:
        pass


def get_krx_gold_price():
    """KRX 국내 금현물 시세(원/g) — 네이버 마켓인덱스. 실패 시 config 폴백."""
    import urllib.request
    import json as _json
    try:
        req = urllib.request.Request(
            "https://m.stock.naver.com/front-api/marketIndex/metals?reutersCode=M04020000",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"})
        d = _json.loads(urllib.request.urlopen(req, timeout=8).read())
        for it in d["result"]["mainList"]:
            if "국내" in it.get("name", "") and "금" in it.get("name", ""):
                return float(str(it["closePrice"]).replace(",", ""))
    except Exception:
        pass
    return float(config.GOLD_PRICE_PER_G)


def _naver_close_series(code: str) -> pd.DataFrame:
    """네이버 일별 종가로 OHLCV 구성 — pykrx/fdr 미지원(ETN 등) 폴백.
    종가만 제공되어 Open/High/Low=Close로 둠.
    """
    import re
    import urllib.request
    import json as _json
    c = re.sub(r"^[A-Za-z]+", "", str(code))   # Q520037 → 520037
    try:
        req = urllib.request.Request(
            f"https://m.stock.naver.com/api/stock/{c}/trend?pageSize=60",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"})
        data = _json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception:
        return pd.DataFrame()
    rows = []
    for it in data:
        try:
            cp = float(str(it.get("closePrice", "")).replace(",", ""))
        except (ValueError, TypeError):
            continue
        dt_ = pd.to_datetime(it.get("bizdate"), errors="coerce")
        if pd.notna(dt_) and cp > 0:
            rows.append((dt_, cp))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["d", "Close"]).dropna().sort_values("d").set_index("d")
    df["Open"] = df["High"] = df["Low"] = df["Close"]
    df["Volume"] = 0.0
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_ohlcv(ticker: str, market: str) -> pd.DataFrame:
    """일별 OHLCV DataFrame[High, Low, Close, Volume] (index=날짜)."""
    key = f"ohlcv_{market}_{ticker}"
    cached = _read_ohlcv_cache(key)
    if cached is not None:
        return cached

    end = dt.datetime.now()
    start = end - dt.timedelta(days=config.LOOKBACK_DAYS + 40)
    s_str, e_str = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    if market == "CASH":
        return empty
    if market == "GOLD":
        g = get_krx_gold_price()          # KRX 국내금 실시세(원/g)
        df = pd.DataFrame({"Open": [g], "High": [g], "Low": [g], "Close": [g],
                           "Volume": [0]}, index=pd.to_datetime([end.date()]))
        _write_ohlcv_cache(key, df)
        return df

    df = None
    try:  # 1차 pykrx
        from pykrx import stock
        raw = stock.get_market_ohlcv(s_str, e_str, ticker)
        if raw is not None and len(raw):
            df = raw.rename(columns={"시가": "Open", "고가": "High", "저가": "Low",
                                     "종가": "Close", "거래량": "Volume"})
            df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            df.index = pd.to_datetime(df.index)
    except Exception:
        pass
    if df is None or len(df) == 0:   # 폴백 FinanceDataReader(신형 영숫자 티커 등)
        try:
            import FinanceDataReader as fdr
            raw = fdr.DataReader(ticker, start)
            if raw is not None and len(raw):
                have = [c for c in ["Open", "High", "Low", "Close", "Volume"]
                        if c in raw.columns]
                df = raw[have].astype(float)
                df.index = pd.to_datetime(df.index)
        except Exception as e:
            print(f"[ohlcv {ticker}] 조회 실패: {e}")
    if df is None or len(df) == 0:    # 네이버 폴백(ETN 등 pykrx/fdr 미지원)
        df = _naver_close_series(ticker)
    if df is None or len(df) == 0 or "Close" not in df:
        return empty
    df = df[df["Close"] > 0]
    if len(df):
        _write_ohlcv_cache(key, df)
    return df


def get_price_series(ticker: str, market: str) -> pd.Series:
    """일별 종가 시계열(Series). 베타·벤치마크용."""
    df = get_ohlcv(ticker, market)
    if df is None or len(df) == 0 or "Close" not in df:
        return pd.Series(dtype=float)
    return df["Close"]


def get_benchmark_series(benchmark: str) -> pd.Series:
    """벤치마크(기초지수) 일별 종가 시계열.
    지수 추종 ETF를 프록시로 쓰며, 주식/ETF와 동일하게 get_market_ohlcv로 조회한다.
    """
    spec = config.BENCHMARK_INDEX.get(benchmark)
    if not spec:
        return pd.Series(dtype=float)
    return get_price_series(spec["proxy"], "KRX")


# ──────────────────────────────────────────────────────────────────────
# 지표 계산
# ──────────────────────────────────────────────────────────────────────
def _beta_corr(asset: pd.Series, market: pd.Series):
    """베타·상관계수 — '시장'(코스피) 대비. CAPM 표준."""
    if asset is None or market is None or len(asset) < 20 or len(market) < 20:
        return np.nan, np.nan
    a = asset.pct_change().dropna()
    b = market.pct_change().dropna()
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(j) < 20:
        return np.nan, np.nan
    ar, br = j.iloc[:, 0], j.iloc[:, 1]
    var_b = br.var()
    beta = (np.cov(ar, br)[0, 1] / var_b) if var_b > 0 else np.nan
    return beta, ar.corr(br)


def _excess(asset: pd.Series, own_bench: pd.Series):
    """기초지수 대비 초과수익률 — 각 종목의 자기 기초지수 대비 기간 누적 차이."""
    if asset is None or own_bench is None or len(asset) < 20 or len(own_bench) < 20:
        return np.nan
    a = asset.pct_change().dropna()
    b = own_bench.pct_change().dropna()
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(j) < 20:
        return np.nan
    asset_cum = (1 + j.iloc[:, 0]).prod() - 1
    bench_cum = (1 + j.iloc[:, 1]).prod() - 1
    return asset_cum - bench_cum


def build_positions(holdings_csv: str = None) -> pd.DataFrame:
    """holdings.csv → 종목별 지표가 채워진 DataFrame."""
    path = holdings_csv or os.path.join(BASE, "holdings.csv")
    # Excel 저장 대응: 인코딩(cp949) 폴백
    try:
        h = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        h = pd.read_csv(path, dtype=str, encoding="cp949")

    # 숫자 컬럼 콤마 명시 제거 후 변환 — Excel이 "25,890"(따옴표)으로 저장해도 안전.
    # (pandas thousands 옵션은 따옴표로 감싼 값엔 적용되지 않아 직접 처리)
    for c in ["qty", "avg_price", "target_pct"]:
        if c in h.columns:
            h[c] = pd.to_numeric(
                h[c].astype(str).str.replace(",", "", regex=False).str.strip(),
                errors="coerce")

    market_bench = get_benchmark_series(config.MARKET_BENCHMARK)  # 베타·상관계수 기준(코스피)
    rows = []
    for _, r in h.iterrows():
        qty, avg = float(r["qty"]), float(r["avg_price"])
        # Excel이 앞자리 0을 지운 KRX 티커 자동 복원(5385→005385)
        ticker = str(r["ticker"]).strip()
        if r["market"] == "KRX" and ticker.isdigit():
            ticker = ticker.zfill(6)
        if r["market"] == "CASH":          # 예수금/현금: 평가=원금, 손익 0
            ohlcv = pd.DataFrame()
            px = pd.Series(dtype=float)
            last = avg
        else:
            ohlcv = get_ohlcv(ticker, r["market"])
            px = ohlcv["Close"] if len(ohlcv) and "Close" in ohlcv else pd.Series(dtype=float)
            last = float(px.iloc[-1]) if len(px) else np.nan
        principal = qty * avg
        eval_amt = qty * last if not np.isnan(last) else np.nan
        ret = (last / avg - 1) if (not np.isnan(last) and avg) else np.nan

        beta, corr = _beta_corr(px, market_bench)          # vs 코스피(시장)
        excess = _excess(px, get_benchmark_series(r["benchmark"]))  # vs 자기 기초지수
        ind = quant.compute_indicators(ohlcv) or {}        # 다요인 시그널

        rows.append({
            "계좌": r["account"], "상품": r["product_type"], "종목명": r["name"],
            "티커": ticker, "섹터": r["sector"], "벤치마크": r["benchmark"],
            "보유수량": qty, "평균매입가": avg, "현재가": last,
            "투자원금": principal, "평가금액": eval_amt, "수익률": ret,
            "평가손익": (eval_amt - principal) if not np.isnan(eval_amt) else np.nan,
            "베타": beta, "상관계수": corr, "초과수익률": excess,
            "목표비중": float(r["target_pct"]) if "target_pct" in r and pd.notna(r["target_pct"]) else np.nan,
            "RSI": ind.get("RSI", np.nan), "MACD히스토": ind.get("MACD히스토", np.nan),
            "ADX": ind.get("ADX", np.nan), "추세": ind.get("추세", "-"),
            "시그널점수": ind.get("시그널점수", np.nan), "사유": ind.get("사유", "-"),
            "상황해석": quant.interpret_row(ind, beta),
            "변동성": (float(px.pct_change().dropna().std() * np.sqrt(252))
                     if len(px) > 20 else np.nan),
        })
    df = pd.DataFrame(rows)
    total_eval = df["평가금액"].sum(skipna=True)
    df["비중"] = df["평가금액"] / total_eval if total_eval else np.nan
    df["퀀트목표비중"] = quant.recommend_targets(      # 순수 데이터·퀀트 산출(수동 target_pct 미사용)
        df, gold_sleeve=config.PURE_GOLD_SLEEVE, cash_floor=config.PURE_CASH_FLOOR,
        max_weight=config.PURE_MAX_WEIGHT, shift=config.PURE_SIGNAL_SHIFT)
    # 동적 투자근거(실제 계산과 연동) — 정적 문구 대체
    df["투자근거"] = [
        quant.decision_reason(
            config.ROLE.get(str(r["티커"]), r["섹터"]),
            (r["비중"] * 100) if pd.notna(r["비중"]) else 0.0,
            r["퀀트목표비중"], r["RSI"], r["MACD히스토"], r["ADX"],
            r["추세"], r["시그널점수"], r["베타"], r["섹터"])
        for _, r in df.iterrows()]
    return df


def compute_portfolio_beta(df: pd.DataFrame) -> dict:
    """포트폴리오 전체 베타(코스피 대비) — 평가금액 가중 평균.
    현금·금 등 베타 미산출 자산은 베타 0으로 간주(전체 기준).
    """
    total = df["평가금액"].sum(skipna=True)
    valid = df.dropna(subset=["베타"])
    contrib = (valid["평가금액"] * valid["베타"]).sum()
    beta_all = contrib / total if total else np.nan          # 현금 포함(현금 β=0)
    invest = df[~df["섹터"].eq("현금성자산")]["평가금액"].sum(skipna=True)
    beta_ex_cash = contrib / invest if invest else np.nan    # 현금 제외
    return {"beta_all": beta_all, "beta_ex_cash": beta_ex_cash}


# ──────────────────────────────────────────────────────────────────────
# ISA 세후 수익률 (계좌 단위)
# ──────────────────────────────────────────────────────────────────────
def compute_isa_tax(df: pd.DataFrame) -> dict:
    """ISA 계좌 전체를 손익통산하여 '지금 전량 매도 가정' 세후 순이익 계산."""
    isa = df[df["계좌"] == "ISA"]
    principal = isa["투자원금"].sum(skipna=True)
    eval_amt = isa["평가금액"].sum(skipna=True)
    net_profit = eval_amt - principal                       # 손익통산 순이익

    limit = config.ISA_TAX_FREE_LIMIT[config.ISA_TYPE]
    taxable = max(0.0, net_profit - limit)                  # 비과세 한도 초과분
    tax = taxable * config.ISA_SEPARATE_TAX_RATE
    after_tax_profit = net_profit - tax

    return {
        "계좌": "ISA",
        "투자원금": principal,
        "평가금액": eval_amt,
        "세전손익": net_profit,
        "세전수익률": net_profit / principal if principal else np.nan,
        "비과세한도": limit,
        "과세대상": taxable,
        "예상세금": tax,
        "세후손익": after_tax_profit,
        "세후수익률": after_tax_profit / principal if principal else np.nan,
        "실효세율": tax / net_profit if net_profit > 0 else 0.0,
    }


def compute_gold_summary(df: pd.DataFrame) -> dict:
    g = df[df["계좌"] == "금현물"]
    if len(g) == 0:
        return None
    principal = g["투자원금"].sum(skipna=True)
    eval_amt = g["평가금액"].sum(skipna=True)
    net = eval_amt - principal
    return {
        "계좌": "금현물", "투자원금": principal, "평가금액": eval_amt,
        "세전손익": net, "세전수익률": net / principal if principal else np.nan,
        "예상세금": net * config.GOLD_TAX_RATE,  # 금현물 매매차익 비과세 → 0
        "세후수익률": net / principal if principal else np.nan,
    }


def sector_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    s = df.groupby("섹터")["평가금액"].sum().sort_values(ascending=False)
    out = s.to_frame("평가금액")
    out["비중"] = out["평가금액"] / out["평가금액"].sum()
    return out.reset_index()


if __name__ == "__main__":
    pd.set_option("display.unicode.east_asian_width", True)
    df = build_positions()
    print(df[["종목명", "현재가", "투자원금", "평가금액", "수익률",
              "베타", "상관계수", "초과수익률", "비중"]].to_string(index=False))
    print("\n[ISA 세후]", compute_isa_tax(df))
    print("[금현물]", compute_gold_summary(df))
    print("\n[섹터별]\n", sector_breakdown(df).to_string(index=False))
