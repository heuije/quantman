# -*- coding: utf-8 -*-
"""종목 상세 캔들차트 — 캔들 + 이동평균 + 볼린저밴드 + 거래량 + RSI(14)/SMA9 + 매매신호.
TradingView 스타일(한국식 색상: 상승 빨강 / 하락 파랑).
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

UP, DN = "#e03131", "#1c7ed6"          # 상승 빨강 / 하락 파랑
MA_COLORS = {5: "#f59f00", 20: "#1c7ed6", 60: "#e8590c",
             120: "#2f9e44", 240: "#862e9c"}


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def latest_marker(ohlcv: pd.DataFrame):
    """차트 Buy/Sell 마커의 '현재 상태' 요약(가장 최근 매수/매도 신호일)."""
    if ohlcv is None or len(ohlcv) < 30 or "Close" not in ohlcv:
        return {"state": "데이터 부족", "buy": None, "sell": None}
    c = ohlcv["Close"].astype(float)
    ma5, ma20 = c.rolling(5).mean(), c.rolling(20).mean()
    bbu = ma20 + 2 * c.rolling(20).std()
    r = _rsi(c)
    buy = (ma5.shift(1) <= ma20.shift(1)) & (ma5 > ma20) & (r < 65)
    sell = ((r.shift(1) > 70) & (r < r.shift(1))) | ((c < c.shift(1)) & (c.shift(1) > bbu.shift(1)))
    bd = c.index[buy.fillna(False)]
    sd = c.index[sell.fillna(False)]
    last_buy = bd[-1] if len(bd) else None
    last_sell = sd[-1] if len(sd) else None
    if last_buy is None and last_sell is None:
        state = "중립(신호 없음)"
    elif last_sell is None or (last_buy is not None and last_buy >= last_sell):
        state = "매수 우세"
    else:
        state = "매도(단기 과열) 우세"
    return {"state": state, "buy": last_buy, "sell": last_sell}


def build_price_chart(ohlcv: pd.DataFrame, name: str, lookback: int = 240):
    """OHLCV → 3단(가격/거래량/RSI) plotly Figure."""
    if ohlcv is None or len(ohlcv) < 30 or "Open" not in ohlcv:
        return None
    d = ohlcv.tail(lookback + 240).copy()      # 이평 계산 여유 후 표시구간 컷
    close = d["Close"]

    # 지표
    for n in (5, 20, 60, 120, 240):
        d[f"MA{n}"] = close.rolling(n).mean()
    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    d["BB_U"], d["BB_L"] = ma20 + 2 * sd20, ma20 - 2 * sd20
    d["RSI"] = _rsi(close)
    d["RSI_SMA9"] = d["RSI"].rolling(9).mean()

    # 매매신호
    ma5, ma20s = d["MA5"], d["MA20"]
    cross_up = (ma5.shift(1) <= ma20s.shift(1)) & (ma5 > ma20s)
    buy = cross_up & (d["RSI"] < 65)
    rsi_drop = (d["RSI"].shift(1) > 70) & (d["RSI"] < d["RSI"].shift(1))   # 과매수 꺾임
    price_turn = (close < close.shift(1)) & (close.shift(1) > d["BB_U"].shift(1))
    sell = rsi_drop | price_turn

    d = d.tail(lookback)                       # 표시 구간만
    x = d.index

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        vertical_spacing=0.045, row_heights=[0.6, 0.18, 0.22])

    # ── 1단: 캔들 + 이평 + 볼린저 ──
    fig.add_trace(go.Candlestick(
        x=x, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
        name="캔들", increasing_line_color=UP, decreasing_line_color=DN,
        increasing_fillcolor=UP, decreasing_fillcolor=DN, showlegend=False), row=1, col=1)
    for n in (5, 20, 60, 120, 240):
        fig.add_trace(go.Scatter(x=x, y=d[f"MA{n}"], name=f"MA{n}",
                                 line=dict(color=MA_COLORS[n], width=1)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=d["BB_U"], name="BB상단",
                             line=dict(color="#adb5bd", width=1, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=d["BB_L"], name="BB하단",
                             line=dict(color="#adb5bd", width=1, dash="dot"),
                             fill="tonexty", fillcolor="rgba(173,181,189,0.07)"), row=1, col=1)
    # 매수/매도 마커
    b, s = d[buy.reindex(d.index, fill_value=False)], d[sell.reindex(d.index, fill_value=False)]
    if len(b):
        fig.add_trace(go.Scatter(x=b.index, y=b["Low"] * 0.985, mode="markers+text",
                                 marker=dict(symbol="triangle-up", color="#2f9e44", size=11),
                                 text="Buy", textposition="bottom center",
                                 textfont=dict(color="#2f9e44", size=9), name="매수"), row=1, col=1)
    if len(s):
        fig.add_trace(go.Scatter(x=s.index, y=s["High"] * 1.015, mode="markers+text",
                                 marker=dict(symbol="triangle-down", color="#e03131", size=11),
                                 text="Sell", textposition="top center",
                                 textfont=dict(color="#e03131", size=9), name="매도"), row=1, col=1)

    # ── 2단: 거래량 ──
    vol_color = np.where(d["Close"] >= d["Open"], UP, DN)
    fig.add_trace(go.Bar(x=x, y=d["Volume"], marker_color=vol_color,
                         marker_line_width=0, name="거래량", showlegend=False), row=2, col=1)

    # ── 3단: RSI ──
    fig.add_trace(go.Scatter(x=x, y=d["RSI"], name="RSI",
                             line=dict(color="#9c36b5", width=1.2)), row=3, col=1)
    fig.add_trace(go.Scatter(x=x, y=d["RSI_SMA9"], name="RSI SMA9",
                             line=dict(color="#f59f00", width=1)), row=3, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="#e03131", line_width=0.8, row=3, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#1c7ed6", line_width=0.8, row=3, col=1)

    fig.update_layout(height=780, margin=dict(t=88, b=10, l=10, r=10),
                      xaxis_rangeslider_visible=False, hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.012,
                                  xanchor="left", x=0, font=dict(size=10)),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.update_yaxes(title_text="가격(원)", row=1, col=1)
    fig.update_yaxes(title_text="거래량", row=2, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1)
    return fig
