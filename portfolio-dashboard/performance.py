# -*- coding: utf-8 -*-
"""일별 마감 포트폴리오 성과 누적 기록 + 시각화.

- 앱을 열 때마다 '오늘' 스냅샷을 performance_history.csv에 upsert(같은 날은 갱신).
- 매일(거래일) 한 줄씩 쌓여 1~2년 뒤 일별 수익률·변동 추이를 한눈에 볼 수 있음.
- 벤치마크(코스피) 종가도 함께 기록해 추적 시작 대비 누적수익률을 비교.
"""
import os
import datetime as dt
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(BASE, "performance_history.csv")
COLS = ["날짜", "투자원금", "평가금액", "평가손익", "수익률", "ISA세후수익률", "포트베타", "KOSPI"]


def load_history() -> pd.DataFrame:
    if os.path.exists(HIST):
        try:
            df = pd.read_csv(HIST, encoding="utf-8-sig")
            df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
            return df.dropna(subset=["날짜"]).sort_values("날짜").reset_index(drop=True)
        except Exception:
            pass
    return pd.DataFrame(columns=COLS)


def record_today(total_principal, total_eval, isa_after_ret, pbeta, kospi_close) -> pd.DataFrame:
    """오늘자 스냅샷 upsert 후 전체 이력 반환."""
    df = load_history()
    today = pd.Timestamp(dt.date.today())
    pl = total_eval - total_principal
    row = {
        "날짜": today,
        "투자원금": round(float(total_principal), 0),
        "평가금액": round(float(total_eval), 0),
        "평가손익": round(float(pl), 0),
        "수익률": round(pl / total_principal, 6) if total_principal else 0.0,
        "ISA세후수익률": round(float(isa_after_ret), 6) if isa_after_ret is not None and not pd.isna(isa_after_ret) else np.nan,
        "포트베타": round(float(pbeta), 4) if pbeta is not None and not pd.isna(pbeta) else np.nan,
        "KOSPI": round(float(kospi_close), 2) if kospi_close else np.nan,
    }
    df = df[df["날짜"] != today]                       # 같은 날 기존행 제거(갱신)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True).sort_values("날짜")
    try:
        df.to_csv(HIST, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return df.reset_index(drop=True)


def add_cumulative(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if len(d) == 0:
        return d
    d["포트수익률%"] = d["수익률"] * 100                # 원금 대비 누적 손익률
    if d["KOSPI"].notna().any():
        base = d["KOSPI"].dropna().iloc[0]
        d["KOSPI누적%"] = (d["KOSPI"] / base - 1) * 100  # 추적 시작일 대비
    else:
        d["KOSPI누적%"] = np.nan
    d["포트전일대비%"] = d["평가금액"].pct_change() * 100
    d["KOSPI전일대비%"] = d["KOSPI"].pct_change() * 100
    d["일간초과%"] = d["포트전일대비%"] - d["KOSPI전일대비%"]  # 포트 − 코스피(일간)
    return d


def build_perf_chart(df: pd.DataFrame):
    """상단 시각화: 포트 수익률 vs KOSPI 누적수익률 + 평가금액(보조축)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    if df is None or len(df) == 0:
        return None
    d = add_cumulative(df)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=d["날짜"], y=d["평가금액"], name="평가금액(우축)",
                  marker_color="rgba(150,150,150,0.22)", marker_line_width=0), secondary_y=True)
    fig.add_trace(go.Scatter(x=d["날짜"], y=d["포트수익률%"], name="포트폴리오 수익률(원금 대비)",
                  line=dict(color="#e03131", width=2.2), mode="lines+markers"), secondary_y=False)
    if d["KOSPI누적%"].notna().any():
        fig.add_trace(go.Scatter(x=d["날짜"], y=d["KOSPI누적%"], name="KOSPI 누적수익률(추적 시작 대비)",
                      line=dict(color="#1c7ed6", width=1.6, dash="dot")), secondary_y=False)
    fig.add_hline(y=0, line_color="#666", line_width=0.8, secondary_y=False)
    fig.update_layout(height=430, margin=dict(t=40, b=10, l=10, r=10), hovermode="x unified",
                      title=dict(text="일별 누적 성과 추이 (포트폴리오 vs 코스피)", x=0.01, font=dict(size=13)),
                      legend=dict(orientation="h", y=1.0, x=0, font=dict(size=10)),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.update_yaxes(title_text="수익률(%)", secondary_y=False)
    fig.update_yaxes(title_text="평가금액(원)", secondary_y=True, showgrid=False)
    return fig
