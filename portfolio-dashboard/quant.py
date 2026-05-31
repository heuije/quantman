# -*- coding: utf-8 -*-
"""퀀트 시그널 엔진 — 다요인(multi-factor) 합성.

단일 지표의 약점을 서로 보완해 거짓신호(whipsaw)를 줄인다.
※ '완벽/결함 없는' 시그널은 존재하지 않음 — 본 엔진은 신뢰도를 높이는 설계임.

구성 요인
  추세    : 이동평균 배열(20/60), MA20 기울기, 장기추세(가격 vs MA120)
  모멘텀  : MACD(12,26,9) 히스토그램, RSI(14), 스토캐스틱 %K(14)
  변동성  : 볼린저밴드 %B(20,2σ)
  거래량  : OBV(20일 기울기)
  신뢰도  : ADX(14) — 추세강도가 약하면(ADX<20) 추세계열 신호를 자동 약화(가짜신호 방지)

최종 시그널점수 = 위 요인들을 합산 후 -3~+3로 정규화.
전략 기본비중(target_pct)을 점수로 틸트해 '퀀트목표비중'을 산출(현금은 잔여).
"""
import numpy as np
import pandas as pd


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _macd_hist(close, fast=12, slow=26, sig=9):
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return macd - macd.ewm(span=sig, adjust=False).mean()


def _stoch_k(high, low, close, n=14):
    ll = low.rolling(n).min()
    hh = high.rolling(n).max()
    return 100 * (close - ll) / (hh - ll).replace(0, np.nan)


def _bollinger_pctb(close, n=20, k=2):
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std()
    upper, lower = ma + k * sd, ma - k * sd
    return (close - lower) / (upper - lower).replace(0, np.nan)


def _adx(high, low, close, n=14):
    up = high.diff()
    dn = -low.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = pd.concat([(high - low),
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _obv(close, vol):
    direction = np.sign(close.diff()).fillna(0)
    return (direction * vol).cumsum()


def compute_indicators(ohlcv):
    """OHLCV DataFrame → 지표값 + 다요인 시그널점수(-3~+3) + 사유."""
    if ohlcv is None or len(ohlcv) < 60 or "Close" not in ohlcv:
        return None
    close = ohlcv["Close"].astype(float)
    high = ohlcv["High"].astype(float) if "High" in ohlcv else close
    low = ohlcv["Low"].astype(float) if "Low" in ohlcv else close
    has_vol = "Volume" in ohlcv and ohlcv["Volume"].fillna(0).abs().sum() > 0
    vol = ohlcv["Volume"].astype(float) if has_vol else None

    last = float(close.iloc[-1])
    rsi = float(_rsi(close).iloc[-1])
    hist = float(_macd_hist(close).iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1])
    ma20_prev = float(close.rolling(20).mean().iloc[-6])           # 5거래일 전
    ma120 = float(close.rolling(120).mean().iloc[-1]) if len(close) >= 120 else np.nan
    stoch = float(_stoch_k(high, low, close).iloc[-1])
    pctb = float(_bollinger_pctb(close).iloc[-1])
    adx = float(_adx(high, low, close).iloc[-1])
    obv_slope = np.nan
    if vol is not None and len(close) > 21:
        obv = _obv(close, vol)
        obv_slope = float(obv.iloc[-1] - obv.iloc[-21])

    # ── 요인별 점수 ──────────────────────────────────────────────
    T1 = 1 if last > ma20 > ma60 else (-1 if last < ma20 < ma60 else 0)   # 배열
    T2 = 0 if np.isnan(ma120) else (1 if last > ma120 else -1)            # 장기추세
    T3 = 1 if ma20 > ma20_prev else (-1 if ma20 < ma20_prev else 0)       # MA20 기울기
    M1 = 1 if hist > 0 else (-1 if hist < 0 else 0)                       # MACD
    M2 = -1 if rsi > 70 else (1 if rsi < 30 else 0)                       # RSI 가드
    M3 = -1 if stoch > 80 else (1 if stoch < 20 else 0)                   # 스토캐스틱
    V1 = -1 if pctb > 1 else (1 if pctb < 0 else 0)                       # 볼린저 %B
    O1 = 0 if np.isnan(obv_slope) else (1 if obv_slope > 0 else -1)       # OBV

    # ADX 신뢰도 게이트: 추세강도 약하면 추세계열(T1,T3,M1) 약화
    conf = 1.0 if adx >= 25 else (0.7 if adx >= 20 else 0.4)
    raw = conf * (T1 + T3 + M1) + T2 + M2 + M3 + V1 + O1                  # 약 -8~+8
    score = int(np.clip(round(raw * 3 / 8), -3, 3))

    adx_txt = "강추세" if adx >= 25 else ("추세약" if adx < 20 else "추세보통")
    arr = "정배열" if T1 == 1 else ("역배열" if T1 == -1 else "혼조")
    reason = (f"RSI{rsi:.0f}{'(과매수)' if rsi > 70 else '(과매도)' if rsi < 30 else ''}"
              f"·MACD{'+' if hist > 0 else '−' if hist < 0 else '0'}"
              f"·ADX{adx:.0f}({adx_txt})·{arr}"
              f"·스토{stoch:.0f}"
              f"{'·거래량↑' if O1 == 1 else '·거래량↓' if O1 == -1 else ''}")
    return {"RSI": rsi, "MACD히스토": hist, "ADX": adx, "추세": arr,
            "시그널점수": score, "사유": reason}


def interpret_row(ind: dict, beta=None) -> str:
    """지표 수치별로 '지금 어떤 상황인지'를 구체적으로 서술."""
    if not ind or ind.get("RSI") is None or (isinstance(ind.get("RSI"), float) and np.isnan(ind.get("RSI"))):
        return "현금·안전자산 등으로 기술적 시그널 미적용."
    rsi = ind["RSI"]; hist = ind["MACD히스토"]; adx = ind["ADX"]
    trend = ind["추세"]; score = ind["시그널점수"]
    p = []
    # RSI
    if rsi > 70:
        p.append(f"RSI {rsi:.0f} 과매수(>70) — 단기 조정 위험, 신규 추격매수 자제")
    elif rsi < 30:
        p.append(f"RSI {rsi:.0f} 과매도(<30) — 반등 가능 구간, 분할매수 고려")
    elif rsi >= 50:
        p.append(f"RSI {rsi:.0f} 중립~강세")
    else:
        p.append(f"RSI {rsi:.0f} 중립~약세")
    # ADX(추세 신뢰도)
    if adx >= 40:
        p.append(f"ADX {adx:.0f} 매우 강한 추세(방향 신뢰도 높음)")
    elif adx >= 25:
        p.append(f"ADX {adx:.0f} 강한 추세")
    elif adx >= 20:
        p.append(f"ADX {adx:.0f} 추세 보통")
    else:
        p.append(f"ADX {adx:.0f} 추세 약함(횡보, 신호 신뢰도 낮음)")
    # MACD
    p.append("MACD 상승모멘텀(+)" if hist > 0 else
             ("MACD 하락모멘텀(−)" if hist < 0 else "MACD 중립"))
    # 이동평균 배열
    p.append(f"이평 {trend}")
    # 베타
    if beta is not None and not (isinstance(beta, float) and np.isnan(beta)):
        if beta >= 1:
            p.append(f"베타 {beta:.2f} 시장보다 큰 변동(공격적)")
        elif beta >= 0.5:
            p.append(f"베타 {beta:.2f} 시장 동행")
        elif beta >= 0:
            p.append(f"베타 {beta:.2f} 시장 둔감(방어적)")
        else:
            p.append(f"베타 {beta:.2f} 시장 역방향(헤지 효과)")
    # 종합 결론
    concl = {3: "종합 강한 매수신호 → 비중 확대",
             2: "종합 매수우위 → 비중 확대",
             1: "종합 약강세 → 비중 소폭 확대",
             0: "종합 중립 → 현 비중 유지",
             -1: "종합 약세 → 비중 소폭 축소",
             -2: "종합 매도우위 → 비중 축소",
             -3: "종합 강한 매도신호 → 비중 축소"}.get(int(score), "중립")
    return " · ".join(p) + f"  ⟹  {concl}"


def decision_reason(role, cur_w, tgt_w, rsi, hist, adx, trend, score, beta, sector):
    """실제 계산과 연동된 동적 투자근거(왜 이 목표비중인지)."""
    delta = (tgt_w or 0) - (cur_w or 0)
    if delta > 0.3:
        act = f"목표 {tgt_w:.1f}% (현 {cur_w:.1f}%, +{delta:.1f}%p 확대)"
    elif delta < -0.3:
        act = f"목표 {tgt_w:.1f}% (현 {cur_w:.1f}%, {delta:.1f}%p 축소)"
    else:
        act = f"목표 {tgt_w:.1f}% (현 비중 유지)"

    if sector == "현금성자산":
        return (f"{role}. 시그널이 강하면 줄고 약하면 늘어나는 잔여 완충(리스크오프 자동조절). {act}.")
    if "금" in str(sector):
        return f"{role}. 주식과 낮은 상관 → 위기 방어용 전략비중. {act}."
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return f"{role}. 시세 이력 부족으로 기술 시그널 미적용 → 전략비중 유지. {act}."

    score = int(score)
    drivers = []
    if hist > 0:
        drivers.append("MACD 상승모멘텀")
    elif hist < 0:
        drivers.append("MACD 하락모멘텀")
    if trend == "정배열":
        drivers.append("이평 정배열")
    elif trend == "역배열":
        drivers.append("이평 역배열")
    if rsi > 70:
        drivers.append(f"RSI {rsi:.0f} 과매수")
    elif rsi < 30:
        drivers.append(f"RSI {rsi:.0f} 과매도")
    conf = ("ADX {0:.0f} 추세신뢰 높음".format(adx) if adx >= 25
            else "ADX {0:.0f} 추세신뢰 보통".format(adx) if adx >= 20
            else "ADX {0:.0f} 횡보(신뢰 낮음)".format(adx))
    sigword = {3: "강한 매수우위", 2: "매수우위", 1: "약강세", 0: "중립",
               -1: "약세", -2: "매도우위", -3: "강한 매도우위"}[score]
    if np.isnan(beta):
        brisk = ""
    elif beta >= 1:
        brisk = f" 리스크: 베타 {beta:.2f} 코스피보다 변동 큼(하락장 방어 약함)."
    elif beta >= 0.5:
        brisk = f" 리스크: 베타 {beta:.2f} 코스피와 동행."
    elif beta >= 0:
        brisk = f" 리스크: 베타 {beta:.2f} 코스피 둔감(방어적)."
    else:
        brisk = f" 리스크: 베타 {beta:.2f} 코스피 역방향(하락장 헤지)."
    drv = ", ".join(drivers) if drivers else "뚜렷한 신호 없음"
    return (f"{role}. 기술 시그널 {score:+d}점({sigword}: {drv}; {conf}) → {act}.{brisk}")


def recommend_targets(df, gold_sleeve=8.0, cash_floor=5.0, max_weight=30.0, shift=2):
    """순수 데이터·퀀트 목표비중(%). 수동 target_pct 미사용.

    종목 가중치 ∝ max(0, 시그널점수+shift) / 변동성  (시그널 강도 비례 + 위험조정).
    금현물=고정 안전자산 슬리브, 종목당 상한, 현금=잔여(상한 초과·약신호분 흡수, 하한 cash_floor).
    """
    score = df["시그널점수"]
    vol = df["변동성"] if "변동성" in df else pd.Series(np.nan, index=df.index)
    is_cash = df["섹터"].eq("현금성자산")
    is_gold = df["섹터"].astype(str).str.contains("금")

    out = pd.Series(0.0, index=df.index)
    g = gold_sleeve if is_gold.any() else 0.0
    budget = 100.0 - g - cash_floor          # 주식에 배분 가능한 예산

    raw = {}
    for i in df.index:
        if is_cash[i] or is_gold[i]:
            continue
        s, v = score[i], vol[i]
        # 변동성 하한 8% — 초저변동(채권 등) 자산이 역변동성으로 폭발하는 것 방지
        raw[i] = (max(0.0, float(s) + shift) / max(float(v), 0.08)
                  if pd.notna(s) and pd.notna(v) and v > 0 else 0.0)

    total = sum(raw.values())
    capped_sum = 0.0
    if total > 0:
        for i, r in raw.items():
            w = min(budget * r / total, max_weight)   # 종목당 상한
            out[i] = w
            capped_sum += w
    out[is_gold] = g
    out[is_cash] = cash_floor + (budget - capped_sum)  # 미배분분(상한초과·약신호)→현금
    tot = out.sum()
    return (out / tot * 100).round(1) if tot > 0 else out.round(1)
