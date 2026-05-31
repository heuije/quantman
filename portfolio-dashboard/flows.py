# -*- coding: utf-8 -*-
"""투자자별 수급(외국인/기관/개인 순매수) 데이터 + 시사점 생성.

데이터 출처: 네이버 모바일 금융 API (m.stock.naver.com/api/stock/<code>/trend).
- pykrx의 투자자별 매매 함수는 이 환경에서 KRX 엔드포인트 오류로 동작하지 않아 네이버 사용.
- 종목·ETF·지수추종 ETF(프록시) 모두 동일 API로 외국인/기관/개인 순매수 수량 + 외국인 보유율 제공.
"""
import os
import json
import urllib.request
import numpy as np
import pandas as pd

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
_BASE = os.path.dirname(os.path.abspath(__file__))
FLOW_HIST = os.path.join(_BASE, "flows_history.csv")
_FH_COLS = ["날짜", "종목명", "티커", "외국인", "기관", "개인", "외국인보유율", "종가"]


def load_flows_history() -> pd.DataFrame:
    if os.path.exists(FLOW_HIST):
        try:
            h = pd.read_csv(FLOW_HIST, encoding="utf-8-sig")
            h["날짜"] = pd.to_datetime(h["날짜"], errors="coerce")
            h["티커"] = h["티커"].astype(str)
            return h.dropna(subset=["날짜"])
        except Exception:
            pass
    return pd.DataFrame(columns=_FH_COLS)


def record_flows_today(pairs) -> pd.DataFrame:
    """매일 자동 적립: 보유·지수 종목의 '최근 거래일' 투자자별 순매수를 flows_history.csv에 upsert.
    시간이 누적되며 자체 다년치 수급 DB가 되어 향후 백테스트 표본을 확보한다.
    """
    rows = []
    for nm, code in pairs:
        d = get_investor_trend(str(code), 5)
        if d is None or len(d) == 0:
            continue
        last = d.iloc[-1]
        rows.append({"날짜": d.index[-1], "종목명": nm, "티커": str(code),
                     "외국인": last["외국인"], "기관": last["기관"], "개인": last["개인"],
                     "외국인보유율": last["외국인보유율"], "종가": last["종가"]})
    if not rows:
        return load_flows_history()
    new = pd.DataFrame(rows)
    hist = load_flows_history()
    if len(hist):                         # 같은 (날짜,티커)는 갱신
        key = set(zip(new["날짜"], new["티커"]))
        hist = hist[~hist.apply(lambda r: (r["날짜"], str(r["티커"])) in key, axis=1)]
    out = pd.concat([hist, new], ignore_index=True).sort_values(["날짜", "종목명"])
    try:
        out.to_csv(FLOW_HIST, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return out.reset_index(drop=True)


def _num(s):
    if s is None:
        return np.nan
    t = str(s).replace(",", "").replace("+", "").replace("%", "").strip()
    try:
        return float(t)
    except ValueError:
        return np.nan


def get_investor_trend(code: str, days: int = 60) -> pd.DataFrame:
    """일자별 외국인/기관/개인 순매수 수량 + 외국인 보유율 + 종가."""
    import re
    c = re.sub(r"^[A-Za-z]+", "", str(code))   # ETN 등 Q520037 → 520037
    url = f"https://m.stock.naver.com/api/stock/{c}/trend?pageSize={days}"
    try:
        req = urllib.request.Request(url, headers=_UA)
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception:
        return pd.DataFrame()
    rows = []
    for it in data:
        rows.append({
            "날짜": pd.to_datetime(it.get("bizdate"), errors="coerce"),
            "외국인": _num(it.get("foreignerPureBuyQuant")),
            "기관": _num(it.get("organPureBuyQuant")),
            "개인": _num(it.get("individualPureBuyQuant")),
            "외국인보유율": _num(it.get("foreignerHoldRatio")),
            "종가": _num(it.get("closePrice")),
        })
    if not rows:                               # 빈 응답(ETN 등) → 빈 DF 안전 반환
        return pd.DataFrame(columns=["외국인", "기관", "개인", "외국인보유율", "종가"])
    return pd.DataFrame(rows).dropna(subset=["날짜"]).sort_values("날짜").set_index("날짜")


def get_longterm_trend(code: str, pages: int = 9) -> pd.DataFrame:
    """중장기(~1년) 외국인·기관 순매매 + 외국인보유율 — 네이버 frgn HTML.
    (이 소스는 개인 순매수를 제공하지 않음 → 외국인·기관만.)
    """
    frames = []
    for pg in range(1, pages + 1):
        url = f"https://finance.naver.com/item/frgn.naver?code={code}&page={pg}"
        try:
            req = urllib.request.Request(url, headers=_UA)
            html = urllib.request.urlopen(req, timeout=10).read().decode("euc-kr", "ignore")
            tbs = pd.read_html(html)
        except Exception:
            break
        tb = next((t for t in tbs if t.shape[1] >= 9 and t.shape[0] > 5), None)
        if tb is None:
            break
        tb = tb.copy()
        tb.columns = ["날짜", "종가", "전일비", "등락률", "거래량",
                      "기관", "외국인", "외국인보유주수", "외국인보유율"]
        frames.append(tb)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    for c in ["기관", "외국인", "종가"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["외국인보유율"] = pd.to_numeric(
        df["외국인보유율"].astype(str).str.replace("%", ""), errors="coerce")
    df = (df.dropna(subset=["날짜"]).drop_duplicates("날짜")
            .sort_values("날짜").set_index("날짜"))
    return df[["외국인", "기관", "외국인보유율", "종가"]]


def build_longterm_chart(df: pd.DataFrame, name: str):
    """중장기 누적 순매매(외국인·기관) + 종가 — 매집/분산 추세 확인용."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d["외국인누적"] = d["외국인"].cumsum()
    d["기관누적"] = d["기관"].cumsum()
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=d.index, y=d["외국인누적"], name="외국인 누적순매수",
                  line=dict(color="#1c7ed6", width=1.6)), secondary_y=False)
    fig.add_trace(go.Scatter(x=d.index, y=d["기관누적"], name="기관 누적순매수",
                  line=dict(color="#f59f00", width=1.6)), secondary_y=False)
    fig.add_trace(go.Scatter(x=d.index, y=d["종가"], name="종가",
                  line=dict(color="#868e96", width=1, dash="dot")), secondary_y=True)
    fig.add_hline(y=0, line_color="#666", line_width=0.8, secondary_y=False)
    fig.update_layout(height=420, title=dict(text=f"{name} 중장기 누적 순매매(외국인·기관) vs 종가",
                      x=0.01, font=dict(size=13)), margin=dict(t=55, b=10, l=10, r=10),
                      hovermode="x unified", legend=dict(orientation="h", y=1.0, x=0, font=dict(size=10)),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.update_yaxes(title_text="누적순매수(주)", secondary_y=False)
    fig.update_yaxes(title_text="종가", secondary_y=True)
    return fig


def _streak(s: pd.Series) -> int:
    """최근 연속 순매수(+일수)/순매도(-일수)."""
    v = s.dropna().values[::-1]
    if len(v) == 0:
        return 0
    sign = np.sign(v[0])
    if sign == 0:
        return 0
    c = 0
    for x in v:
        if np.sign(x) == sign:
            c += 1
        else:
            break
    return int(c * sign)


def summarize(df: pd.DataFrame):
    """수급 요약: 5/20일 누적 순매수, 보유율·변화, 외국인 연속일수."""
    if df is None or len(df) == 0:
        return None
    return {
        "외국인5": df["외국인"].tail(5).sum(),
        "외국인20": df["외국인"].tail(20).sum(),
        "기관5": df["기관"].tail(5).sum(),
        "기관20": df["기관"].tail(20).sum(),
        "개인20": df["개인"].tail(20).sum(),
        "외국인보유율": df["외국인보유율"].iloc[-1],
        "보유율변화": df["외국인보유율"].iloc[-1] - df["외국인보유율"].iloc[0],
        "외국인연속": _streak(df["외국인"]),
    }


_INV_COLOR = {"외국인": "#1c7ed6", "기관": "#f59f00", "개인": "#adb5bd"}


def build_flow_chart(df: pd.DataFrame, name: str, days: int = 40, investors=None):
    """수급 차트: 선택 수급처 순매수 막대 + 외국인보유율·종가 추이."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    if df is None or len(df) == 0:
        return None
    investors = investors or ["외국인", "기관", "개인"]
    d = df.tail(days)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        row_heights=[0.62, 0.38], specs=[[{}], [{"secondary_y": True}]])
    for inv in investors:
        if inv in d:
            fig.add_bar(x=d.index, y=d[inv], name=inv, marker_color=_INV_COLOR.get(inv),
                        marker_line_width=0, row=1, col=1)
    fig.add_hline(y=0, line_color="#666", line_width=0.8, row=1, col=1)
    fig.add_trace(go.Scatter(x=d.index, y=d["외국인보유율"], name="외국인보유율(%)",
                  line=dict(color="#e03131", width=1.6)), row=2, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(x=d.index, y=d["종가"], name="종가",
                  line=dict(color="#868e96", width=1, dash="dot")), row=2, col=1, secondary_y=True)
    fig.update_layout(barmode="group", height=540, bargap=0.2,
                      title=dict(text=f"{name} 투자자별 순매수 · 외국인보유율", x=0.01, font=dict(size=13)),
                      margin=dict(t=60, b=10, l=10, r=10), hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.update_yaxes(title_text="순매수(주)", row=1, col=1)
    fig.update_yaxes(title_text="외국인보유율%", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="종가", row=2, col=1, secondary_y=True)
    return fig


def flow_return_study(code: str, horizons=(5, 10, 20)):
    """외국인 순매수 방향·규모 → 선행수익률 과거 통계(탐색용, 예측 아님).
    get_longterm_trend(~1년) 사용. 표본 작아 통계적 신뢰도 낮음을 전제.
    """
    d = get_longterm_trend(code, 9)
    if d is None or len(d) < 60:
        return None, None
    c = d["종가"].astype(float)
    f = d["외국인"].astype(float)
    rows = []
    for h in horizons:
        fwd = c.shift(-h) / c - 1
        j = pd.concat([f, fwd], axis=1).dropna()
        j.columns = ["f", "r"]
        if len(j) < 20:
            continue
        buy, sell = j[j["f"] > 0]["r"], j[j["f"] < 0]["r"]
        thr = j[j["f"] > 0]["f"].quantile(0.75) if (j["f"] > 0).any() else np.nan
        strong = j[j["f"] >= thr]["r"] if pd.notna(thr) else pd.Series(dtype=float)
        rows.append({
            "선행기간": f"{h}일", "상관계수": round(j["f"].corr(j["r"]), 2),
            "외인매수일 평균": buy.mean(), "매수일 상승확률": (buy > 0).mean(), "매수일 n": len(buy),
            "외인매도일 평균": sell.mean(), "매도일 상승확률": (sell > 0).mean(), "매도일 n": len(sell),
            "강매수(상위25%) 평균": strong.mean() if len(strong) else np.nan,
            "강매수 상승확률": (strong > 0).mean() if len(strong) else np.nan, "강매수 n": len(strong)})
    meta = {"표본일수": len(d), "시작": d.index.min(), "끝": d.index.max()}
    return pd.DataFrame(rows), meta


def build_flow_scatter(code: str, horizon: int = 10):
    """외국인 순매수(x) vs 선행수익률(y) 산점도 — 관계(또는 무관계) 시각화."""
    import plotly.express as px
    d = get_longterm_trend(code, 9)
    if d is None or len(d) < 60:
        return None
    c = d["종가"].astype(float)
    j = pd.DataFrame({"외국인순매수": d["외국인"].astype(float),
                      f"{horizon}일선행수익률": c.shift(-horizon) / c - 1}).dropna()
    fig = px.scatter(j, x="외국인순매수", y=f"{horizon}일선행수익률", opacity=0.6)
    fig.update_traces(marker=dict(size=6, color="#1c7ed6"))
    fig.update_layout(height=380, margin=dict(t=30, b=10, l=10, r=10),
                      yaxis_tickformat=".0%",
                      title=dict(text=f"외국인 순매수 → {horizon}일 선행수익률 (추세선=회귀)",
                                 x=0.01, font=dict(size=12)),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.add_hline(y=0, line_color="#666", line_width=0.6)
    fig.add_vline(x=0, line_color="#666", line_width=0.6)
    return fig


def insight(name: str, sm: dict, signal_score=None) -> str:
    """수급 방향 + 시사점 + (퀀트 시그널 결합) 전략 제언."""
    if sm is None:
        return "수급 데이터 없음(현금·금현물·해외종목 등)."
    f20, i20, p20 = sm["외국인20"], sm["기관20"], sm["개인20"]

    def d(v, who):
        return f"{who} {'순매수' if v > 0 else '순매도' if v < 0 else '중립'}({v:+,.0f}주)"

    head = f"20일 {d(f20, '외국인')} · {d(i20, '기관')}"
    streak = sm["외국인연속"]
    if abs(streak) >= 3:
        head += f" · 외국인 {abs(streak)}일 연속 {'순매수' if streak > 0 else '순매도'}"

    if f20 > 0 and i20 > 0:
        imp = "외국인·기관 동반 매집 → 강한 수급, 추세 신뢰 높음(비중 확대 근거)"
    elif f20 < 0 and i20 < 0:
        imp = "외국인·기관 동반 이탈 → 수급 악화, 비중 축소·관망"
    elif f20 < 0 and p20 > 0:
        imp = "외국인 이탈을 개인이 받는 구조 → 단기 하방·변동성 주의"
    elif f20 > 0 and i20 < 0:
        imp = "외국인 매수·기관 매도로 주체 엇갈림 → 외국인 주도 여부 확인"
    else:
        imp = "수급 주체 혼조 → 방향성 불명확, 관망"

    hold = f"외국인 보유율 {sm['외국인보유율']:.2f}%({sm['보유율변화']:+.2f}%p)"

    combo = ""
    if signal_score is not None and not (isinstance(signal_score, float) and np.isnan(signal_score)):
        s = int(signal_score)
        if f20 > 0 and s >= 1:
            combo = " ⟹ 수급+기술 동반 양호 → 비중 확대 강화"
        elif f20 < 0 and s <= -1:
            combo = " ⟹ 수급+기술 동반 약세 → 축소 신호 강화"
        elif (f20 > 0) != (s >= 1):
            combo = " ⟹ 수급·기술 신호 엇갈림 → 신중(관망)"
    return f"{head} | {hold} → {imp}{combo}"
