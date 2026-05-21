"""
퀀트 지표 계산 모듈.
입력: OHLCV DataFrame (인덱스=날짜)
출력: 지표 컬럼이 추가된 DataFrame
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional

# ── 기본 수익률 ──────────────────────────────────────────────────────────────

def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["price_level"]     = df["Close"]   # 가격 레벨 자체를 조건으로 쓰기 위함 (예: VIX > 30)
    df["pct_change_1d"]   = df["Close"].pct_change(1) * 100
    df["pct_change_5d"]   = df["Close"].pct_change(5) * 100
    df["pct_change_20d"]  = df["Close"].pct_change(20) * 100
    df["pct_change_252d"] = df["Close"].pct_change(252) * 100   # 1년(약 252 거래일)
    df["log_return_1d"]   = np.log(df["Close"] / df["Close"].shift(1)) * 100
    return df


# ── 이동평균 괴리율 ──────────────────────────────────────────────────────────

def add_ma_deviation(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for w in [20, 60, 200]:
        ma = df["Close"].rolling(w).mean()
        df[f"ma_dev_{w}d"] = (df["Close"] - ma) / ma * 100
    return df


# ── 볼린저밴드 폭 · %b ───────────────────────────────────────────────────────

def add_bb_width(df: pd.DataFrame, window: int = 20, k: float = 2.0) -> pd.DataFrame:
    df = df.copy()
    ma    = df["Close"].rolling(window).mean()
    std   = df["Close"].rolling(window).std()
    upper = ma + k * std
    lower = ma - k * std
    df["bb_width"] = (2 * k * std) / ma * 100
    # %b: 밴드 내 위치. 1.0=상단, 0.5=중심, 0.0=하단. >1 상단 돌파(과매수)
    df["bb_pct"] = (df["Close"] - lower) / (upper - lower).replace(0, np.nan)
    return df


# ── 이동평균 크로스 (20일 vs 60일) ───────────────────────────────────────────

def add_ma_cross(df: pd.DataFrame) -> pd.DataFrame:
    """단기(20일)·중기(60일) MA 갭. 음수 전환 = 데드크로스(추세 이탈)."""
    df = df.copy()
    ma20 = df["Close"].rolling(20).mean()
    ma60 = df["Close"].rolling(60).mean()
    df["ma_gap_20_60"] = (ma20 - ma60) / ma60.replace(0, np.nan) * 100
    return df


# ── 최근 고점 대비 괴리율 ────────────────────────────────────────────────────

def add_high_deviation(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """최근 N일 고점 대비 현재 종가의 낙폭(%). 0=신고가, 음수=고점 아래."""
    df = df.copy()
    roll_high = df["High"].rolling(window).max()
    df["high_dev_20d"] = (df["Close"] - roll_high) / roll_high.replace(0, np.nan) * 100
    return df


# ── RSI 베어리시 다이버전스 ──────────────────────────────────────────────────

def add_rsi_divergence(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """
    가격은 최근 고점권인데 RSI는 그만큼 못 오른 날 = 베어리시 다이버전스(1).
    xlsx 근거: '다이버전스가 단일 레벨보다 유효'.
    """
    df = df.copy()
    if "rsi_14" not in df.columns:
        df["rsi_bear_div"] = np.nan
        return df
    px_max     = df["Close"].rolling(window).max()
    rsi_max    = df["rsi_14"].rolling(window).max()
    price_high = df["Close"] >= px_max * 0.999      # 가격은 신고가(혹은 근접)
    rsi_lags   = df["rsi_14"] < rsi_max * 0.97       # RSI는 자신의 고점 대비 하회
    df["rsi_bear_div"] = ((price_high & rsi_lags) & (df["rsi_14"] > 55)).astype(float)
    return df


# ── RSI ─────────────────────────────────────────────────────────────────────

def add_rsi(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    df = df.copy()
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(window).mean()
    loss  = (-delta.clip(upper=0)).rolling(window).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    return df


# ── ATR (Average True Range) ─────────────────────────────────────────────────

def add_atr(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    df = df.copy()
    high, low, prev_close = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"]     = tr.rolling(window).mean()
    df["atr_14_pct"] = df["atr_14"] / df["Close"] * 100
    return df


# ── Realized Volatility ──────────────────────────────────────────────────────

def add_realized_vol(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    log_ret = np.log(df["Close"] / df["Close"].shift(1))
    for w in [5, 20, 60]:
        df[f"realized_vol_{w}d"] = log_ret.rolling(w).std() * np.sqrt(252) * 100
    return df


# ── Z-Score (수익률의 표준화) ────────────────────────────────────────────────

def add_zscore(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ret = df["log_return_1d"] if "log_return_1d" in df.columns else np.log(df["Close"] / df["Close"].shift(1)) * 100
    for w in [20, 60]:
        mu  = ret.rolling(w).mean()
        std = ret.rolling(w).std()
        df[f"zscore_{w}d"] = (ret - mu) / std.replace(0, np.nan)
    return df


# ── 거래량 비율 ──────────────────────────────────────────────────────────────

def add_volume_ratio(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    df = df.copy()
    if "Volume" in df.columns and df["Volume"].sum() > 0:
        avg_vol = df["Volume"].rolling(window).mean()
        df["volume_ratio"] = df["Volume"] / avg_vol.replace(0, np.nan)
    else:
        df["volume_ratio"] = np.nan
    return df


# ── ADV (평균 거래대금) ──────────────────────────────────────────────────────

def add_adv(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """20일 평균 거래대금(가격×거래량). 유동성·보유한도 산정의 기준값."""
    df = df.copy()
    if "Volume" in df.columns and df["Volume"].sum() > 0:
        df["adv_20d"] = (df["Close"] * df["Volume"]).rolling(window).mean()
    else:
        df["adv_20d"] = np.nan
    return df


# ── 연속 방향 (연속 상승/하락 일수) ─────────────────────────────────────────

def add_consecutive_days(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    direction = np.sign(df["Close"].diff())
    streak = []
    count = 0
    for d in direction:
        if d == 0 or np.isnan(d):
            streak.append(count)
            continue
        if count == 0 or np.sign(count) == d:
            count += int(d)
        else:
            count = int(d)
        streak.append(count)
    df["streak"] = streak
    return df


# ── 12-1M 가격 모멘텀 ────────────────────────────────────────────────────────

def add_momentum_12_1m(df: pd.DataFrame) -> pd.DataFrame:
    """12개월 수익률 - 1개월 수익률. 학계·실무에서 가장 검증된 모멘텀 팩터."""
    df = df.copy()
    if "pct_change_252d" not in df.columns:
        df["pct_change_252d"] = df["Close"].pct_change(252) * 100
    pct_1m = df["Close"].pct_change(21) * 100   # 21 거래일 ≈ 1개월
    df["momentum_12_1m"] = df["pct_change_252d"] - pct_1m
    return df


# ── 펀더멘털 지표 병합 ────────────────────────────────────────────────────────

def add_fundamentals(df: pd.DataFrame, fund_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """
    분기별 재무 데이터를 일별 가격 DataFrame에 forward-fill로 합칩니다.
    가격 데이터가 필요한 파생 지표(FCF Yield, P/E, P/B, Altman Z)도 이 단계에서 계산.
    """
    if fund_df is None or fund_df.empty:
        return df

    df = df.copy()
    # 분기 → 일별 forward-fill
    fund_d = fund_df.reindex(df.index, method="ffill")

    shares = fund_d.get("shares_outstanding", pd.Series(np.nan, index=df.index))

    # ── FCF Yield = TTM FCF / 시가총액 × 100
    if "ttm_fcf" in fund_d.columns:
        mkt_cap = df["Close"] * shares.replace(0, np.nan)
        df["fcf_yield"] = fund_d["ttm_fcf"] / mkt_cap.replace(0, np.nan) * 100

    # ── Trailing P/E = Close / (TTM 순이익 / 주식수)
    if "ttm_ni" in fund_d.columns:
        ttm_eps = fund_d["ttm_ni"] / shares.replace(0, np.nan)
        df["trailing_pe"] = df["Close"] / ttm_eps.replace(0, np.nan)

    # ── P/B = Close / (자기자본 / 주식수)
    if "stockholders_equity" in fund_d.columns:
        bvps = fund_d["stockholders_equity"] / shares.replace(0, np.nan)
        df["pb_ratio"] = df["Close"] / bvps.replace(0, np.nan)

    # ── Altman Z-Score = 1.2×WC/TA + 1.4×RE/TA + 3.3×EBIT/TA + 0.6×MktCap/TL + 1.0×Rev/TA
    z_cols = ["z_wc_ta", "z_re_ta", "z_ebit_ta", "z_tl", "z_rev_ta"]
    if all(c in fund_d.columns for c in z_cols):
        mkt_cap = df["Close"] * shares.replace(0, np.nan)
        z_mkttl = mkt_cap / fund_d["z_tl"].replace(0, np.nan)
        df["altman_z"] = (
            1.2 * fund_d["z_wc_ta"] +
            1.4 * fund_d["z_re_ta"] +
            3.3 * fund_d["z_ebit_ta"] +
            0.6 * z_mkttl +
            1.0 * fund_d["z_rev_ta"]
        )

    # ── 나머지 펀더멘털 컬럼 그대로 복사
    for col in ["gross_margin", "op_margin", "net_debt_ebitda", "roic"]:
        if col in fund_d.columns:
            df[col] = fund_d[col]

    # ── EV/EBITDA · EV/Sales : EV = 시가총액 + 총부채 − 현금
    td   = fund_d.get("total_debt")
    cash = fund_d.get("cash")
    td0   = td.fillna(0)   if td   is not None else 0.0
    cash0 = cash.fillna(0) if cash is not None else 0.0
    mkt_cap = df["Close"] * shares.replace(0, np.nan)
    ev = mkt_cap + td0 - cash0
    if "ttm_ebitda" in fund_d.columns:
        df["ev_ebitda"] = ev / fund_d["ttm_ebitda"].replace(0, np.nan)
    if "ttm_rev" in fund_d.columns:
        df["ev_sales"] = ev / fund_d["ttm_rev"].replace(0, np.nan)

    # ── PEG = Trailing P/E ÷ EPS 성장률(%, 전년 대비)
    if "ttm_ni" in fund_d.columns and "trailing_pe" in df.columns:
        eps = fund_d["ttm_ni"] / shares.replace(0, np.nan)
        eps_growth = (eps / eps.shift(252) - 1) * 100
        df["peg"] = df["trailing_pe"] / eps_growth.replace(0, np.nan)

    # ── 총이익률 추세 : 전년 대비 ppt 변화
    if "gross_margin" in fund_d.columns:
        df["gross_margin_trend"] = fund_d["gross_margin"] - fund_d["gross_margin"].shift(252)

    # ── 현금전환율 = TTM FCF / TTM 순이익 × 100
    if "ttm_fcf" in fund_d.columns and "ttm_ni" in fund_d.columns:
        df["cash_conversion"] = fund_d["ttm_fcf"] / fund_d["ttm_ni"].replace(0, np.nan) * 100

    return df


# ── 지표 메타데이터 ──────────────────────────────────────────────────────────

INDICATOR_META = {
    # 가격 레벨 — 한국 종목 OHLC는 모두 정규장(09:00~15:30) 기준.
    # 시간외 단일가는 dataset에 포함되지 않으므로 매수 신호도 정규장 종가로 평가.
    "price_level":        {"label": "가격 (정규장 종가)",  "unit": "",   "decimals": 2},
    # 가격 수익률 — 모두 정규장 종가 기반
    "pct_change_1d":      {"label": "전일대비(%)",        "unit": "%",  "decimals": 2},
    "pct_change_5d":      {"label": "5일 수익률(%)",      "unit": "%",  "decimals": 2},
    "pct_change_20d":     {"label": "20일 수익률(%)",     "unit": "%",  "decimals": 2},
    "pct_change_252d":    {"label": "1년 수익률(%)",      "unit": "%",  "decimals": 1},
    "log_return_1d":      {"label": "로그수익률(1일,%)",  "unit": "%",  "decimals": 3},
    # 모멘텀
    "momentum_12_1m":     {"label": "12-1M 모멘텀(%)",   "unit": "%",  "decimals": 1},
    "streak":             {"label": "연속방향(일)",       "unit": "일", "decimals": 0},
    # 이동평균 괴리율 · 크로스
    "ma_dev_20d":         {"label": "20일MA 괴리율(%)",  "unit": "%",  "decimals": 2},
    "ma_dev_60d":         {"label": "60일MA 괴리율(%)",  "unit": "%",  "decimals": 2},
    "ma_dev_200d":        {"label": "200일MA 괴리율(%)", "unit": "%",  "decimals": 2},
    "ma_gap_20_60":       {"label": "MA갭(20-60,%)",     "unit": "%",  "decimals": 2},
    "high_dev_20d":       {"label": "20일고점 괴리율(%)", "unit": "%", "decimals": 2},
    # 변동성 · 기술적
    "bb_width":           {"label": "BB폭(%)",           "unit": "%",  "decimals": 2},
    "bb_pct":             {"label": "볼린저 %b",         "unit": "",   "decimals": 3},
    "rsi_14":             {"label": "RSI(14)",           "unit": "",   "decimals": 1},
    "rsi_bear_div":       {"label": "RSI베어다이버전스",  "unit": "",   "decimals": 0},
    "atr_14_pct":         {"label": "ATR(14, %)",        "unit": "%",  "decimals": 2},
    "realized_vol_20d":   {"label": "실현변동성(20일,%)", "unit": "%", "decimals": 1},
    "realized_vol_60d":   {"label": "실현변동성(60일,%)", "unit": "%", "decimals": 1},
    # 통계
    "zscore_20d":         {"label": "Z-Score(20일)",     "unit": "",   "decimals": 2},
    "zscore_60d":         {"label": "Z-Score(60일)",     "unit": "",   "decimals": 2},
    # 거래량
    "volume_ratio":       {"label": "거래량비율(20일)",  "unit": "x",  "decimals": 2},
    "adv_20d":            {"label": "ADV(20일 거래대금)", "unit": "",  "decimals": 0},
    # ── 개별종목 펀더멘털 (해당 종목에만 존재) ──
    "gross_margin":       {"label": "총이익률(%)",        "unit": "%",  "decimals": 1},
    "gross_margin_trend": {"label": "총이익률 추세(%p)",  "unit": "%p", "decimals": 1},
    "op_margin":          {"label": "영업이익률(%)",      "unit": "%",  "decimals": 1},
    "roic":               {"label": "ROIC(%)",           "unit": "%",  "decimals": 1},
    "cash_conversion":    {"label": "현금전환율(%)",      "unit": "%",  "decimals": 0},
    "net_debt_ebitda":    {"label": "순부채/EBITDA",      "unit": "x",  "decimals": 2},
    "ev_ebitda":          {"label": "EV/EBITDA",         "unit": "x",  "decimals": 1},
    "ev_sales":           {"label": "EV/Sales",          "unit": "x",  "decimals": 2},
    "trailing_pe":        {"label": "Trailing P/E",      "unit": "x",  "decimals": 1},
    "pb_ratio":           {"label": "P/B Ratio",         "unit": "x",  "decimals": 2},
    "peg":                {"label": "PEG",               "unit": "",   "decimals": 2},
    "fcf_yield":          {"label": "FCF Yield(%)",      "unit": "%",  "decimals": 2},
    "altman_z":           {"label": "Altman Z-Score",    "unit": "",   "decimals": 2},
}

# 항상 존재하는 가격 기반 지표 (지수/ETF/코인 포함)
BASE_INDICATOR_COLS = [
    "pct_change_1d", "pct_change_5d", "pct_change_20d", "pct_change_252d",
    "log_return_1d", "momentum_12_1m", "streak",
    "ma_dev_20d", "ma_dev_60d", "ma_dev_200d", "ma_gap_20_60", "high_dev_20d",
    "bb_width", "bb_pct", "rsi_14", "rsi_bear_div", "atr_14_pct",
    "realized_vol_20d", "realized_vol_60d",
    "zscore_20d", "zscore_60d",
    "volume_ratio", "adv_20d", "price_level",
]

# 개별종목에만 존재하는 펀더멘털 지표
FUND_INDICATOR_COLS = [
    "gross_margin", "gross_margin_trend", "op_margin", "roic", "cash_conversion",
    "net_debt_ebitda", "ev_ebitda", "ev_sales", "trailing_pe", "pb_ratio", "peg",
    "fcf_yield", "altman_z",
]

# 지표 소분류 — 조건 빌더 UI에서 드롭다운을 그룹화하기 위한 분류
INDICATOR_GROUPS: dict[str, list[str]] = {
    "가격·수익률": ["price_level", "pct_change_1d", "pct_change_5d",
                  "pct_change_20d", "pct_change_252d", "log_return_1d"],
    "모멘텀":      ["momentum_12_1m", "streak"],
    "이동평균":    ["ma_dev_20d", "ma_dev_60d", "ma_dev_200d",
                  "ma_gap_20_60", "high_dev_20d"],
    "변동성·기술적": ["bb_width", "bb_pct", "rsi_14", "rsi_bear_div",
                   "atr_14_pct", "realized_vol_20d", "realized_vol_60d"],
    "통계":        ["zscore_20d", "zscore_60d"],
    "거래량":      ["volume_ratio", "adv_20d"],
    "펀더멘털":     list(FUND_INDICATOR_COLS),
}

_COL_TO_GROUP = {col: grp for grp, cols in INDICATOR_GROUPS.items() for col in cols}


def get_indicator_group(col: str) -> str:
    """지표 컬럼이 속한 소분류명을 반환."""
    return _COL_TO_GROUP.get(col, "기타")


def compute_all(df: pd.DataFrame, fund_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    df = add_returns(df)
    df = add_ma_deviation(df)
    df = add_ma_cross(df)
    df = add_bb_width(df)
    df = add_rsi(df)
    df = add_rsi_divergence(df)
    df = add_atr(df)
    df = add_realized_vol(df)
    df = add_zscore(df)
    df = add_volume_ratio(df)
    df = add_adv(df)
    df = add_high_deviation(df)
    df = add_consecutive_days(df)
    df = add_momentum_12_1m(df)
    if fund_df is not None and not fund_df.empty:
        df = add_fundamentals(df, fund_df)
    return df


def get_indicator_columns() -> list[str]:
    """가격 기반 지표 컬럼 목록 (항상 존재)."""
    return list(BASE_INDICATOR_COLS)


def get_all_indicator_columns() -> list[str]:
    """가격 기반 + 펀더멘털 지표 전체 목록."""
    return list(BASE_INDICATOR_COLS) + list(FUND_INDICATOR_COLS)


def get_indicator_label(col: str) -> str:
    return INDICATOR_META.get(col, {}).get("label", col)


# 비교 호환 그룹 — 같은 그룹 안에서만 지표↔지표 비교가 의미가 있다.
# (백분율과 0-100 RSI를 비교하면 무의미하게 항상 참/거짓이 되어 fool-proof 차단)
# rsi_14는 0-100 무차원이므로 자기 그룹으로 분리. bb_pct는 0-1 무차원.
# 비교를 위한 카테고리 키. unit 문자열만으로는 모자라서 별도 분류.
COMPARE_GROUP: dict[str, str] = {
    # 백분율 (%) — 수익률·괴리율·변동성·이익률 등 부호 있는 %
    "pct_change_1d": "pct", "pct_change_5d": "pct", "pct_change_20d": "pct",
    "pct_change_252d": "pct", "log_return_1d": "pct", "momentum_12_1m": "pct",
    "ma_dev_20d": "pct", "ma_dev_60d": "pct", "ma_dev_200d": "pct",
    "ma_gap_20_60": "pct", "high_dev_20d": "pct",
    "bb_width": "pct", "atr_14_pct": "pct",
    "realized_vol_20d": "pct", "realized_vol_60d": "pct",
    "gross_margin": "pct", "gross_margin_trend": "pct", "op_margin": "pct",
    "roic": "pct", "cash_conversion": "pct", "fcf_yield": "pct",
    # 가격 (원) — 절대 가격 레벨
    "price_level": "price",
    # 0-100 무차원 (RSI 류)
    "rsi_14": "rsi",
    # 0-1 무차원 (Bollinger %B)
    "bb_pct": "bbpct",
    # 불리언/플래그 (0 or 1)
    "rsi_bear_div": "flag",
    # 일수
    "streak": "days",
    # 배수 (x)
    "volume_ratio": "mult", "net_debt_ebitda": "mult",
    "ev_ebitda": "mult", "ev_sales": "mult",
    "trailing_pe": "mult", "pb_ratio": "mult", "peg": "mult",
    # 표준편차 (Z)
    "zscore_20d": "z", "zscore_60d": "z", "altman_z": "z",
    # 거래대금 (원·큰 값)
    "adv_20d": "money",
}


def get_indicator_unit(col: str) -> str:
    """지표의 단위 문자열 ('%', '', 'x', '일' 등). INDICATOR_META에서 가져옴."""
    return INDICATOR_META.get(col, {}).get("unit", "")


def get_indicator_compare_group(col: str) -> str:
    """지표끼리 비교가 의미 있는 그룹 키. 다른 그룹끼리는 비교 차단."""
    return COMPARE_GROUP.get(col, "other")
