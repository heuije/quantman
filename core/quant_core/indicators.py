"""
퀀트 지표 계산 모듈.
입력: OHLCV DataFrame (인덱스=날짜)
출력: 지표 컬럼이 추가된 DataFrame
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional


def _safe_log_return(close: pd.Series) -> pd.Series:
    """log(close / prev_close) — 두 값 모두 양수일 때만 계산, 아니면 NaN (C-02).

    가격(주가)에는 잘 정의되지만 매크로 시계열(금리차·스프레드 등 음수 가능)에서는
    ``np.log``가 -inf 또는 NaN을 만들면서 'divide by zero in log'·'invalid value in
    log' 경고를 띄우고, 다운스트림 신호가 ``fillna(False)``로 조용히 누락된다.
    정의역에서 마스킹하여 경고를 근본 차단하고 NaN을 명시적 미정의 신호로 둔다.
    """
    prev = close.shift(1)
    valid = (close > 0) & (prev > 0)
    ratio = close.where(valid) / prev.where(valid)
    return np.log(ratio)


# ── 기본 수익률 ──────────────────────────────────────────────────────────────

def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["price_level"]     = df["Close"]   # 가격 레벨 자체를 조건으로 쓰기 위함 (예: VIX > 30)
    df["pct_change_1d"]   = df["Close"].pct_change(1) * 100
    df["pct_change_5d"]   = df["Close"].pct_change(5) * 100
    df["pct_change_20d"]  = df["Close"].pct_change(20) * 100
    df["pct_change_252d"] = df["Close"].pct_change(252) * 100   # 1년(약 252 거래일)
    df["log_return_1d"]   = _safe_log_return(df["Close"]) * 100
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
    log_ret = _safe_log_return(df["Close"])  # C-02: 음수/0 Close 마스킹
    for w in [5, 20, 60]:
        df[f"realized_vol_{w}d"] = log_ret.rolling(w).std() * np.sqrt(252) * 100
    return df


# ── Z-Score (수익률의 표준화) ────────────────────────────────────────────────

def add_zscore(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # C-02: log_return_1d가 없으면 _safe_log_return으로 계산(음수/0 마스킹).
    ret = (df["log_return_1d"] if "log_return_1d" in df.columns
           else _safe_log_return(df["Close"]) * 100)
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
    """연속 동일방향 일수(부호 있는 streak). 등락 없는 날(diff=0)·첫날은 직전 streak 유지.

    벡터화 — 과거 행단위 Python 루프가 compute_all 비용의 ~63%(지표당 ~14ms)였다.
    등락 있는 날만 부호 런렝스(연속 동일부호 길이)로 집계하고, flat 일은 직전 streak을
    ffill해 루프와 동일 결과를 낸다(엣지 포함 등가성은 test_indicators가 고정).
    """
    df = df.copy()
    d = np.sign(df["Close"].diff()).to_numpy()
    n = len(d)
    nz = ~np.isnan(d) & (d != 0)                 # 등락 있는 날(첫날 NaN·flat 제외)
    out = np.zeros(n, dtype=np.int64)
    if nz.any():
        signs = d[nz].astype(np.int64)           # ±1
        ss = pd.Series(signs)
        grp = (ss != ss.shift()).cumsum()        # 부호 바뀌면 새 런
        pos = ss.groupby(grp).cumcount().to_numpy() + 1   # 런 내 1-기반 위치
        scattered = np.full(n, np.nan)
        scattered[nz] = pos * signs              # 비-flat 위치에 부호 런렝스
        out = pd.Series(scattered).ffill().fillna(0).astype(np.int64).to_numpy()  # flat=직전값·선두=0
    df["streak"] = out
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

    # 시가총액 = 종가 × 발행주식수(PIT). 스크리너·사이징 참조 메트릭(추가 소스 0 — shares는 펀더멘털서).
    df["market_cap"] = df["Close"] * shares.replace(0, np.nan)

    # ── FCF Yield = TTM FCF / 시가총액 × 100
    if "ttm_fcf" in fund_d.columns:
        mkt_cap = df["Close"] * shares.replace(0, np.nan)
        df["fcf_yield"] = fund_d["ttm_fcf"] / mkt_cap.replace(0, np.nan) * 100

    # ── Trailing P/E = Close / (TTM 순이익 / 주식수)
    # EPS<=0(적자)이면 PER은 미정의(NaN) — 음수 PER은 '싼 것'이 아니라 적자 신호라
    # "저평가"=낮은값 랭킹을 오염시킨다(분모 비양수 → NaN).
    if "ttm_ni" in fund_d.columns:
        ttm_eps = fund_d["ttm_ni"] / shares.replace(0, np.nan)
        df["trailing_pe"] = df["Close"] / ttm_eps.where(ttm_eps > 0)

    # ── P/B = Close / (자기자본 / 주식수)
    # BVPS<=0(자본잠식)이면 PBR은 미정의(NaN) — 음수 PBR은 부실 신호지 저평가가 아니다.
    if "stockholders_equity" in fund_d.columns:
        bvps = fund_d["stockholders_equity"] / shares.replace(0, np.nan)
        df["pb_ratio"] = df["Close"] / bvps.where(bvps > 0)

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
    "market_cap":         {"label": "시가총액",           "unit": "",   "decimals": 0},
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
    "fcf_yield", "altman_z", "market_cap",
]

# 기관·외국인 수급 (flow.kr_investor 피드) — 일별 순매수(거래대금), reindex-ffill 병합
FLOW_INDICATOR_COLS = ["inst_net_buy", "foreign_net_buy"]

# 애널 컨센서스 (estimate.consensus 피드) — 7 패널 컬럼 + target_upside(종가 결합 파생)
CONSENSUS_FEED_COLS = ["consensus_target", "consensus_target_median", "analyst_count",
                       "consensus_opinion", "target_dispersion", "target_revision_pct",
                       "days_since_report"]
CONSENSUS_INDICATOR_COLS = CONSENSUS_FEED_COLS + ["target_upside"]

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
    "수급":         list(FLOW_INDICATOR_COLS),
    "컨센서스":     list(CONSENSUS_INDICATOR_COLS),
}

_COL_TO_GROUP = {col: grp for grp, cols in INDICATOR_GROUPS.items() for col in cols}


def get_indicator_group(col: str) -> str:
    """지표 컬럼이 속한 소분류명을 반환."""
    return _COL_TO_GROUP.get(col, "기타")


def add_flow(df: pd.DataFrame, flow_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """기관·외국인 순매수(일별)를 가격 인덱스에 reindex-ffill 병합. as_of=거래일 → look-ahead 0."""
    if flow_df is None or flow_df.empty:
        return df
    df = df.copy()
    f = flow_df.reindex(df.index, method="ffill")
    for col in FLOW_INDICATOR_COLS:
        if col in f.columns:
            df[col] = f[col]
    return df


def add_consensus(df: pd.DataFrame, consensus_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """애널 컨센서스(변경점 패널) reindex-ffill 병합 + target_upside(종가 괴리율) 파생.

    target_upside = consensus_target / Close − 1 (양수=상승여력) — pb_ratio류 Close 결합과 동형.
    """
    if consensus_df is None or consensus_df.empty:
        return df
    df = df.copy()
    c = consensus_df.reindex(df.index, method="ffill")
    for col in CONSENSUS_FEED_COLS:
        if col in c.columns:
            df[col] = c[col]
    if "consensus_target" in c.columns:
        df["target_upside"] = c["consensus_target"] / df["Close"].replace(0, np.nan) - 1
    return df


def compute_all(df: pd.DataFrame, fund_df: Optional[pd.DataFrame] = None,
                consensus_df: Optional[pd.DataFrame] = None,
                flow_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
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
    if consensus_df is not None and not consensus_df.empty:
        df = add_consensus(df, consensus_df)
    if flow_df is not None and not flow_df.empty:
        df = add_flow(df, flow_df)
    return df


# ── 컬럼 프로젝션 (선택적 지표 계산) ──────────────────────────────────────────
# 각 add_*가 만드는 출력 컬럼 맵 — compute_columns가 "요청 컬럼 → 필요한 producer"만
# 골라 실행하기 위함. all/스크리너 백테스트가 참조하는 지표만 계산해 메모리/시간을 줄인다
# (전 유니버스 45컬럼 동시 상주 ≈ 9.4GB → 참조 2~3컬럼만이면 ~1.5-2GB).
#
# 불변성 근거: 각 add_*는 OHLCV(+하드의존 rsi_14)의 순수 함수다. 소프트 의존
# (zscore→log_return_1d, momentum→pct_change_252d)은 그 컬럼이 없으면 **동일 공식으로
# 자가계산**한다 → 일부만 실행해도 요청 컬럼 값은 compute_all과 byte 동일.
# 순서는 compute_all과 동일(rsi가 rsi_divergence보다 선행). test_compute_columns가 고정.
_PRODUCERS: list[tuple] = [
    (add_returns,          ("price_level", "pct_change_1d", "pct_change_5d",
                            "pct_change_20d", "pct_change_252d", "log_return_1d"), ()),
    (add_ma_deviation,     ("ma_dev_20d", "ma_dev_60d", "ma_dev_200d"), ()),
    (add_ma_cross,         ("ma_gap_20_60",), ()),
    (add_bb_width,         ("bb_width", "bb_pct"), ()),
    (add_rsi,              ("rsi_14",), ()),
    (add_rsi_divergence,   ("rsi_bear_div",), ("rsi_14",)),   # 하드 의존: rsi_14 선행
    (add_atr,              ("atr_14", "atr_14_pct"), ()),
    (add_realized_vol,     ("realized_vol_5d", "realized_vol_20d", "realized_vol_60d"), ()),
    (add_zscore,           ("zscore_20d", "zscore_60d"), ()),       # 소프트: log_return_1d
    (add_volume_ratio,     ("volume_ratio",), ()),
    (add_adv,              ("adv_20d",), ()),
    (add_high_deviation,   ("high_dev_20d",), ()),
    (add_consecutive_days, ("streak",), ()),
    (add_momentum_12_1m,   ("momentum_12_1m",), ()),              # 소프트: pct_change_252d
]
_COL_TO_PRODUCER_IDX: dict[str, int] = {
    c: i for i, (_, cols, _) in enumerate(_PRODUCERS) for c in cols}


def compute_columns(df: pd.DataFrame, columns,
                    fund_df: Optional[pd.DataFrame] = None,
                    consensus_df: Optional[pd.DataFrame] = None,
                    flow_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """요청한 지표 컬럼만 계산해 부착(컬럼 프로젝션). OHLCV는 항상 보존.

    compute_all(45컬럼 전부)의 부분집합 버전. 반환 DataFrame의 **요청 컬럼 값은
    compute_all과 byte 동일**(각 add_*가 OHLCV의 순수 함수이고 소프트 의존은 자가복구).
    요청 컬럼이 다른 producer를 하드 의존하면(rsi_bear_div→rsi_14) 그 컬럼도 함께 생성된다.
    OHLCV·매크로 등 지표 아닌 참조는 무시(이미 df에 있거나 계산 대상 아님).
    """
    wanted = set(columns)
    need_idx: set[int] = set()
    for c in wanted:
        i = _COL_TO_PRODUCER_IDX.get(c)
        if i is not None:
            need_idx.add(i)
    # 하드 의존 전이 폐쇄 (rsi_bear_div → rsi_14 등)
    changed = True
    while changed:
        changed = False
        for i in list(need_idx):
            for dep in _PRODUCERS[i][2]:
                j = _COL_TO_PRODUCER_IDX.get(dep)
                if j is not None and j not in need_idx:
                    need_idx.add(j)
                    changed = True
    out = df.copy()
    for i, (fn, _cols, _deps) in enumerate(_PRODUCERS):   # compute_all과 동일 순서
        if i in need_idx:
            out = fn(out)
    # 펀더멘털 컬럼이 하나라도 요청되면 add_fundamentals 1회(Close+fund_df의 순수 함수)
    if (wanted & set(FUND_INDICATOR_COLS)) and fund_df is not None and not fund_df.empty:
        out = add_fundamentals(out, fund_df)
    if (wanted & set(CONSENSUS_INDICATOR_COLS)) and consensus_df is not None and not consensus_df.empty:
        out = add_consensus(out, consensus_df)
    if (wanted & set(FLOW_INDICATOR_COLS)) and flow_df is not None and not flow_df.empty:
        out = add_flow(out, flow_df)
    return out


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
    # 시가총액 (절대 통화액) — 자기 그룹(per-share 가격과 비교 무의미)
    "market_cap": "mktcap",
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
