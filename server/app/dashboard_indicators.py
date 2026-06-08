"""개별 종목 분석 탭 — 확장 기술지표 계산.

글로벌 차트(TradingView/Bloomberg) + 국내 증권사 앱에서 흔히 쓰는 지표 38종을
한 곳에서 계산한다. market.py의 symbol_detail이 기본 지표(ma/bb/macd/stoch/obv 등)를
계산한 DataFrame을 넘기면 여기서 확장 지표 컬럼을 추가한다.

입력 DataFrame 전제: Open/High/Low/Close/Volume + rsi_14 + atr_14 컬럼 보유
(market.py가 compute_all + 자체 계산으로 채워둔 상태).

NEW_INDICATORS: 선택 카탈로그(프론트 '지표 선택'에 노출).
NEW_FIELDS: 시리즈 직렬화 대상 (필드명, 반올림 자릿수).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ── 선택 카탈로그 ────────────────────────────────────────────────────────────
# pane: price(주가 오버레이 — 그리드 셀에 종가+오버레이) / sub(오실레이터)
NEW_INDICATORS: list[dict] = [
    # 추세·가격 오버레이
    {"key": "ema", "label": "지수이동평균 EMA(20·60)", "pane": "price"},
    {"key": "wma", "label": "가중이동평균 WMA(20)", "pane": "price"},
    {"key": "vwap", "label": "거래량가중평균가 VWAP(20)", "pane": "price"},
    {"key": "envelope", "label": "엔벨로프(20, ±5%)", "pane": "price"},
    {"key": "keltner", "label": "켈트너 채널(20,2 ATR)", "pane": "price"},
    {"key": "donchian", "label": "돈치안 채널(20)", "pane": "price"},
    {"key": "psar", "label": "파라볼릭 SAR", "pane": "price"},
    {"key": "ichimoku", "label": "일목균형표", "pane": "price"},
    {"key": "supertrend", "label": "슈퍼트렌드(10,3)", "pane": "price"},
    # 모멘텀·오실레이터
    {"key": "cci", "label": "CCI(20)", "pane": "sub"},
    {"key": "williams_r", "label": "Williams %R(14)", "pane": "sub"},
    {"key": "roc", "label": "ROC 가격변화율(12)", "pane": "sub"},
    {"key": "momentum", "label": "모멘텀(10)", "pane": "sub"},
    {"key": "stochrsi", "label": "스토캐스틱 RSI(14)", "pane": "sub"},
    {"key": "trix", "label": "TRIX(15)", "pane": "sub"},
    {"key": "uo", "label": "얼티밋 오실레이터", "pane": "sub"},
    {"key": "ao", "label": "어썸 오실레이터", "pane": "sub"},
    {"key": "dmi", "label": "DMI/ADX(14) 방향성", "pane": "sub"},
    {"key": "aroon", "label": "아룬(25)", "pane": "sub"},
    {"key": "vortex", "label": "볼텍스 VI(14)", "pane": "sub"},
    {"key": "dpo", "label": "DPO 추세제거(20)", "pane": "sub"},
    {"key": "ppo", "label": "PPO(12,26,9)", "pane": "sub"},
    {"key": "disparity", "label": "이격도(20)", "pane": "sub"},
    {"key": "psy", "label": "투자심리도(12)", "pane": "sub"},
    {"key": "kst", "label": "KST", "pane": "sub"},
    {"key": "coppock", "label": "코폭 곡선", "pane": "sub"},
    {"key": "elder", "label": "엘더레이(13) 불/베어파워", "pane": "sub"},
    # 거래량
    {"key": "mfi", "label": "MFI 자금흐름(14)", "pane": "sub"},
    {"key": "cmf", "label": "CMF 차이킨자금(20)", "pane": "sub"},
    {"key": "chaikin_osc", "label": "차이킨 오실레이터", "pane": "sub"},
    {"key": "force_index", "label": "강도지수 Force(13)", "pane": "sub"},
    {"key": "eom", "label": "이동편의도 EOM(14)", "pane": "sub"},
    {"key": "vr", "label": "거래량비율 VR(20)", "pane": "sub"},
    {"key": "ad_line", "label": "A/D 누적분배선", "pane": "sub"},
    # 변동성
    {"key": "bb_pctb", "label": "볼린저 %B", "pane": "sub"},
    {"key": "bb_bw", "label": "볼린저 밴드폭", "pane": "sub"},
    {"key": "stddev", "label": "표준편차(20)", "pane": "sub"},
    {"key": "mass_index", "label": "매스 인덱스(25)", "pane": "sub"},
]

# 직렬화 필드 (필드명, 반올림 자릿수)
NEW_FIELDS: list[tuple[str, int]] = [
    ("ema20", 2), ("ema60", 2), ("wma20", 2), ("vwap", 2),
    ("env_upper", 2), ("env_lower", 2),
    ("kc_upper", 2), ("kc_mid", 2), ("kc_lower", 2),
    ("dc_upper", 2), ("dc_mid", 2), ("dc_lower", 2),
    ("psar", 2),
    ("ichi_tenkan", 2), ("ichi_kijun", 2), ("ichi_spanA", 2), ("ichi_spanB", 2),
    ("supertrend", 2),
    ("cci", 1), ("williams_r", 1), ("roc", 2), ("momentum", 2),
    ("stochrsi_k", 1), ("stochrsi_d", 1), ("trix", 3), ("uo", 1), ("ao", 2),
    ("plus_di", 1), ("minus_di", 1), ("adx", 1),
    ("aroon_up", 1), ("aroon_down", 1), ("vi_plus", 3), ("vi_minus", 3),
    ("dpo", 2), ("ppo", 3), ("ppo_signal", 3), ("ppo_hist", 3),
    ("disparity", 1), ("psy_line", 1), ("kst", 2), ("kst_signal", 2),
    ("coppock", 2), ("bull_power", 2), ("bear_power", 2),
    ("mfi", 1), ("cmf", 4), ("chaikin_osc", 0), ("force_index", 0),
    ("eom", 4), ("vr", 1), ("ad_line", 0),
    ("bb_pct_b", 4), ("bb_bw", 2), ("stddev_20", 2), ("mass_index", 2),
]


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder 평활 (RMA) — alpha=1/n."""
    return s.ewm(alpha=1 / n, adjust=False).mean()


def _psar(high: pd.Series, low: pd.Series, af0: float = 0.02,
          step: float = 0.02, af_max: float = 0.2) -> np.ndarray:
    """파라볼릭 SAR (Wilder). 외부 시스템 한계 없이 표준 반복식으로 계산."""
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    n = len(h)
    sar = np.full(n, np.nan)
    if n < 2:
        return sar
    up = True                      # 초기 추세 가정: 상승
    af = af0
    ep = h[0]
    sar[0] = l[0]
    for i in range(1, n):
        prev = sar[i - 1]
        cur = prev + af * (ep - prev)
        if up:
            cur = min(cur, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if h[i] > ep:
                ep = h[i]; af = min(af + step, af_max)
            if l[i] < cur:          # 추세 반전 → 하락
                up = False; cur = ep; ep = l[i]; af = af0
        else:
            cur = max(cur, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if l[i] < ep:
                ep = l[i]; af = min(af + step, af_max)
            if h[i] > cur:          # 추세 반전 → 상승
                up = True; cur = ep; ep = h[i]; af = af0
        sar[i] = cur
    return sar


def _supertrend(high, low, close, mult: float = 3.0, period: int = 10) -> np.ndarray:
    """슈퍼트렌드 — ATR(period) 기반 추세 추종 라인."""
    h, l, c = (x.to_numpy(dtype=float) for x in (high, low, close))
    n = len(c)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    hl2 = (h + l) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    st = np.full(n, np.nan)
    trend_up = True
    for i in range(1, n):
        if np.isnan(atr[i]):
            continue
        if not np.isnan(upper[i - 1]):
            upper[i] = min(upper[i], upper[i - 1]) if c[i - 1] <= upper[i - 1] else upper[i]
            lower[i] = max(lower[i], lower[i - 1]) if c[i - 1] >= lower[i - 1] else lower[i]
        if trend_up and c[i] < lower[i]:
            trend_up = False
        elif not trend_up and c[i] > upper[i]:
            trend_up = True
        st[i] = lower[i] if trend_up else upper[i]
    return st


def compute(df: pd.DataFrame) -> None:
    """확장 지표 컬럼을 df에 in-place 추가."""
    c, h, l = df["Close"], df["High"], df["Low"]
    v = df["Volume"].astype(float)
    o = df.get("Open", c)
    tp = (h + l + c) / 3                                   # 전형가격
    prev_c = c.shift(1)
    eps = 1e-12

    # 이동평균 계열
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema60"] = c.ewm(span=60, adjust=False).mean()
    w = np.arange(1, 21)
    df["wma20"] = c.rolling(20).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
    df["vwap"] = (tp * v).rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)

    ma20 = c.rolling(20).mean()
    df["env_upper"], df["env_lower"] = ma20 * 1.05, ma20 * 0.95

    atr14 = df["atr_14"] if "atr_14" in df.columns else None
    if atr14 is None:
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr14 = _wilder(tr, 14)
    df["kc_mid"] = df["ema20"]
    df["kc_upper"] = df["ema20"] + 2 * atr14
    df["kc_lower"] = df["ema20"] - 2 * atr14

    df["dc_upper"] = h.rolling(20).max()
    df["dc_lower"] = l.rolling(20).min()
    df["dc_mid"] = (df["dc_upper"] + df["dc_lower"]) / 2

    df["psar"] = _psar(h, l)

    # 일목균형표 (표시 단순화: 선행스팬 +26 시프트 생략 — 그리드 미니차트용)
    df["ichi_tenkan"] = (h.rolling(9).max() + l.rolling(9).min()) / 2
    df["ichi_kijun"] = (h.rolling(26).max() + l.rolling(26).min()) / 2
    df["ichi_spanA"] = (df["ichi_tenkan"] + df["ichi_kijun"]) / 2
    df["ichi_spanB"] = (h.rolling(52).max() + l.rolling(52).min()) / 2

    df["supertrend"] = _supertrend(h, l, c)

    # CCI
    md = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df["cci"] = (tp - tp.rolling(20).mean()) / (0.015 * md.replace(0, np.nan))

    # Williams %R
    hh14, ll14 = h.rolling(14).max(), l.rolling(14).min()
    df["williams_r"] = (hh14 - c) / (hh14 - ll14).replace(0, np.nan) * -100

    df["roc"] = (c / c.shift(12) - 1) * 100
    df["momentum"] = c - c.shift(10)

    # Stochastic RSI
    rsi = df["rsi_14"] if "rsi_14" in df.columns else _rsi(c, 14)
    rmin, rmax = rsi.rolling(14).min(), rsi.rolling(14).max()
    stochrsi = (rsi - rmin) / (rmax - rmin).replace(0, np.nan)
    df["stochrsi_k"] = (stochrsi * 100).rolling(3).mean()
    df["stochrsi_d"] = df["stochrsi_k"].rolling(3).mean()

    # TRIX
    e1 = c.ewm(span=15, adjust=False).mean()
    e2 = e1.ewm(span=15, adjust=False).mean()
    e3 = e2.ewm(span=15, adjust=False).mean()
    df["trix"] = e3.pct_change() * 100

    # Ultimate Oscillator
    bp = c - pd.concat([l, prev_c], axis=1).min(axis=1)
    tr_uo = pd.concat([h, prev_c], axis=1).max(axis=1) - pd.concat([l, prev_c], axis=1).min(axis=1)
    avg7 = bp.rolling(7).sum() / tr_uo.rolling(7).sum().replace(0, np.nan)
    avg14 = bp.rolling(14).sum() / tr_uo.rolling(14).sum().replace(0, np.nan)
    avg28 = bp.rolling(28).sum() / tr_uo.rolling(28).sum().replace(0, np.nan)
    df["uo"] = 100 * (4 * avg7 + 2 * avg14 + avg28) / 7

    # Awesome Oscillator
    med = (h + l) / 2
    df["ao"] = med.rolling(5).mean() - med.rolling(34).mean()

    # DMI / ADX (Wilder)
    up_move, down_move = h.diff(), -l.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr_w = _wilder(tr, 14).replace(0, np.nan)
    df["plus_di"] = 100 * _wilder(pd.Series(plus_dm, index=c.index), 14) / atr_w
    df["minus_di"] = 100 * _wilder(pd.Series(minus_dm, index=c.index), 14) / atr_w
    dx = 100 * (df["plus_di"] - df["minus_di"]).abs() / (df["plus_di"] + df["minus_di"]).replace(0, np.nan)
    df["adx"] = _wilder(dx, 14)

    # Aroon (25)
    df["aroon_up"] = h.rolling(26).apply(lambda x: 100 * (len(x) - 1 - x.argmax()) / 25, raw=True)
    df["aroon_down"] = l.rolling(26).apply(lambda x: 100 * (len(x) - 1 - x.argmin()) / 25, raw=True)

    # Vortex (14)
    vm_plus = (h - l.shift(1)).abs()
    vm_minus = (l - h.shift(1)).abs()
    tr_sum = tr.rolling(14).sum().replace(0, np.nan)
    df["vi_plus"] = vm_plus.rolling(14).sum() / tr_sum
    df["vi_minus"] = vm_minus.rolling(14).sum() / tr_sum

    # DPO (20)
    df["dpo"] = c.shift(11) - c.rolling(20).mean()

    # PPO
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    df["ppo"] = (e12 - e26) / e26.replace(0, np.nan) * 100
    df["ppo_signal"] = df["ppo"].ewm(span=9, adjust=False).mean()
    df["ppo_hist"] = df["ppo"] - df["ppo_signal"]

    df["disparity"] = c / ma20.replace(0, np.nan) * 100

    # 투자심리도 — 최근 12일 중 상승일 비율
    df["psy_line"] = (c.diff() > 0).rolling(12).sum() / 12 * 100

    # KST
    def _roc(n): return (c / c.shift(n) - 1) * 100
    rcma1 = _roc(10).rolling(10).mean()
    rcma2 = _roc(15).rolling(10).mean()
    rcma3 = _roc(20).rolling(10).mean()
    rcma4 = _roc(30).rolling(15).mean()
    df["kst"] = rcma1 * 1 + rcma2 * 2 + rcma3 * 3 + rcma4 * 4
    df["kst_signal"] = df["kst"].rolling(9).mean()

    # Coppock = WMA10( ROC(14) + ROC(11) )
    cop_in = _roc(14) + _roc(11)
    w10 = np.arange(1, 11)
    df["coppock"] = cop_in.rolling(10).apply(lambda x: np.dot(x, w10) / w10.sum(), raw=True)

    # Elder Ray (13)
    ema13 = c.ewm(span=13, adjust=False).mean()
    df["bull_power"], df["bear_power"] = h - ema13, l - ema13

    # MFI (14)
    rmf = tp * v
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    mfr = pos.rolling(14).sum() / neg.rolling(14).sum().replace(0, np.nan)
    df["mfi"] = 100 - 100 / (1 + mfr)

    # CMF / Chaikin / A-D
    hl = (h - l).replace(0, np.nan)
    mfm = ((c - l) - (h - c)) / hl
    mfv = (mfm * v).fillna(0)
    df["cmf"] = mfv.rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)
    adl = mfv.cumsum()
    df["ad_line"] = adl
    df["chaikin_osc"] = adl.ewm(span=3, adjust=False).mean() - adl.ewm(span=10, adjust=False).mean()

    # Force Index (13)
    df["force_index"] = ((c - prev_c) * v).ewm(span=13, adjust=False).mean()

    # Ease of Movement (14)
    dist = (h + l) / 2 - (h.shift(1) + l.shift(1)) / 2
    box = (v / 1e8) / hl
    df["eom"] = (dist / box.replace(0, np.nan)).rolling(14).mean()

    # Volume Ratio (20)
    chg = c.diff()
    up_v = v.where(chg > 0, 0.0)
    dn_v = v.where(chg < 0, 0.0)
    flat_v = v.where(chg == 0, 0.0)
    df["vr"] = ((up_v.rolling(20).sum() + flat_v.rolling(20).sum() / 2) /
                (dn_v.rolling(20).sum() + flat_v.rolling(20).sum() / 2 + eps)) * 100

    # 볼린저 파생 (bb_upper/lower/mid는 market.py가 채워둔다 — 없으면 자체계산)
    if "bb_upper" in df.columns:
        bbu, bbl, bbm = df["bb_upper"], df["bb_lower"], df["bb_mid"]
    else:
        s20 = c.rolling(20).std()
        bbm, bbu, bbl = ma20, ma20 + 2 * s20, ma20 - 2 * s20
    width = (bbu - bbl).replace(0, np.nan)
    df["bb_pct_b"] = (c - bbl) / width
    df["bb_bw"] = (bbu - bbl) / bbm.replace(0, np.nan) * 100

    df["stddev_20"] = c.rolling(20).std()

    # Mass Index (25)
    hl_ema9 = (h - l).ewm(span=9, adjust=False).mean()
    hl_ema9b = hl_ema9.ewm(span=9, adjust=False).mean()
    df["mass_index"] = (hl_ema9 / hl_ema9b.replace(0, np.nan)).rolling(25).sum()


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    gain = _wilder(d.clip(lower=0), n)
    loss = _wilder(-d.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)
