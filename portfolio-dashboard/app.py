# -*- coding: utf-8 -*-
"""주식계좌 포트폴리오 대시보드 (Streamlit).
실행:  streamlit run app.py   (또는 run_dashboard.bat 더블클릭)
탭 구성: Portfolio Dashboard / Quant Signals / Rebalancing
"""
import os
import datetime as dt
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO

import config
import engine
import chart
import flows
import performance
import dart

st.set_page_config(page_title="Portfolio Dashboard", layout="wide")

# ── 전역 스타일: 대제목·메트릭 폰트 축소 ──────────────────────────────
st.markdown("""
<style>
/* 메트릭 값(숫자) 축소 */
[data-testid="stMetricValue"] { font-size: 1.35rem; font-weight: 700; }
/* 메트릭 라벨(총 투자원금 등) 축소 */
[data-testid="stMetricLabel"] p { font-size: 0.80rem; }
/* 메트릭 델타(-0.09% 등) 축소 */
[data-testid="stMetricDelta"] { font-size: 0.78rem; }
[data-testid="stMetricDelta"] svg { height: 0.85rem; width: 0.85rem; }
/* 소제목(st.subheader → h3) 크기 축소 */
h3 { font-size: 1.12rem !important; font-weight: 700 !important; }
h2 { font-size: 1.3rem !important; }
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=1800, show_spinner="시세 불러오는 중…")
def load():
    df = engine.build_positions()
    isa = engine.compute_isa_tax(df)
    gold = engine.compute_gold_summary(df)
    sec = engine.sector_breakdown(df)
    return df, isa, gold, sec


def won(x):
    return "-" if pd.isna(x) else f"{x:,.0f}원"


def pct(x):
    return "-" if pd.isna(x) else f"{x*100:,.2f}%"


def strategy_brief(df, total_eval):
    """장마감(최근 종가) 기준 퀀트 시그널을 활용한 비중 조정 제언(동적 생성)."""
    d = df.copy()
    d["현재p"] = d["비중"] * 100
    d["목표p"] = d["퀀트목표비중"].fillna(d["현재p"])
    d["조정액"] = total_eval * d["목표p"] / 100 - d["평가금액"]
    sig = d.dropna(subset=["시그널점수"])
    n_strong = int((sig["시그널점수"] >= 1).sum())
    n_weak = int((sig["시그널점수"] <= -1).sum())
    n_neu = int((sig["시그널점수"] == 0).sum())

    def line(r):
        drv = r["사유"] if isinstance(r["사유"], str) else ""
        act = "매수" if r["조정액"] > 0 else "매도"
        sc = r["시그널점수"]
        sc_txt = f"{sc:+.0f}점" if pd.notna(sc) else "시그널 없음"
        return (f"- **{r['종목명']}** ({sc_txt}): {r['현재p']:.1f}% → {r['목표p']:.1f}% "
                f"({r['조정액']:+,.0f}원 {act}) · {drv}")

    buys = d[d["조정액"] > 0].sort_values("조정액", ascending=False).head(4)
    sells = d[(d["조정액"] < 0) & (d["섹터"] != "현금성자산")].sort_values("조정액").head(4)
    crow = d[d["종목명"] == "예수금"]
    c_cur = float(crow["현재p"].iloc[0]) if len(crow) else float("nan")
    c_tgt = float(crow["목표p"].iloc[0]) if len(crow) else float("nan")

    md = [f"**장마감(최근 종가) 기준 퀀트 시그널 종합** — 강세 {n_strong} · 중립 {n_neu} · 약세 {n_weak}개",
          "", "**▲ 비중 확대 제언** (시그널 양호)"]
    md += [line(r) for _, r in buys.iterrows()] if len(buys) else ["- 해당 없음"]
    md += ["", "**▼ 비중 축소 제언** (약세·과열 시그널)"]
    md += [line(r) for _, r in sells.iterrows()] if len(sells) else ["- 해당 없음"]
    md += ["", f"**현금(예수금)**: {c_cur:.1f}% → {c_tgt:.1f}% — 시그널 종합의 잔여로 산출. "
           "시그널이 약해지면 현금을 자동으로 늘려 원금을 방어합니다(리스크오프).",
           "", "> 과매수(RSI>70) 종목은 시그널이 비중을 자동 축소해 추격매수를 자제합니다. "
           "베타는 매수·매도 사유가 아니라 '시장 동반 하락 위험'의 참고지표입니다. "
           "본 제언은 기술적 시그널 기반 참고이며 투자 권유가 아닙니다."]
    return "\n".join(md)


# ── 글로벌 지수 티커 바 ───────────────────────────────────────────────
INDEX_SYMBOLS = {  # FinanceDataReader 심볼
    "S&P500": "US500", "나스닥": "IXIC", "다우존스": "DJI",
    "니케이225": "N225", "항셍": "HSI", "코스피": "KS11", "코스닥": "KQ11",
}


@st.cache_data(ttl=60, show_spinner=False)
def load_indices(periods: int = 90):
    import FinanceDataReader as fdr
    start = (dt.datetime.now() - dt.timedelta(days=int(periods * 1.7) + 30)).strftime("%Y-%m-%d")
    out = {}
    for nm, s in INDEX_SYMBOLS.items():
        try:
            df = fdr.DataReader(s, start).dropna().tail(periods)
            c = df["Close"]
            out[nm] = {"price": float(c.iloc[-1]),
                       "chg": float(c.iloc[-1] / c.iloc[-2] - 1) * 100,
                       "o": [float(v) for v in df["Open"]],
                       "h": [float(v) for v in df["High"]],
                       "l": [float(v) for v in df["Low"]],
                       "c": [float(v) for v in c]}
        except Exception:
            out[nm] = None
    return out


@st.fragment(run_every=60)   # 60초마다 지수 바만 자동 갱신(지연시세 기준)
def render_index_bar():
    data = load_indices()
    cols = st.columns(len(INDEX_SYMBOLS))
    for col, nm in zip(cols, INDEX_SYMBOLS):
        d = data.get(nm)
        with col:
            if not d:
                st.caption(nm)
                st.write("—")
                continue
            up = d["chg"] >= 0
            color = "#e03131" if up else "#1c7ed6"   # 한국식: 상승=빨강 / 하락=파랑
            arrow = "▲" if up else "▼"
            st.markdown(
                f"<div style='line-height:1.18'>"
                f"<span style='font-size:0.78rem;color:#9aa0a6'>{nm}</span><br>"
                f"<span style='font-size:1.05rem;font-weight:700'>{d['price']:,.2f}</span>  "
                f"<span style='font-size:0.82rem;color:{color}'>{arrow}{abs(d['chg']):.2f}%</span>"
                f"</div>", unsafe_allow_html=True)
            o, h, lw, cl = d["o"], d["h"], d["l"], d["c"]
            fig = go.Figure(go.Candlestick(
                x=list(range(len(cl))), open=o, high=h, low=lw, close=cl,
                increasing_line_color="#e03131", decreasing_line_color="#1c7ed6",
                increasing_fillcolor="#e03131", decreasing_fillcolor="#1c7ed6",
                line=dict(width=0.6), whiskerwidth=0.2, showlegend=False))
            lo, hi = min(lw), max(h)
            pad = (hi - lo) * 0.05 or 1
            fig.update_layout(height=64, margin=dict(t=3, b=3, l=2, r=2),
                              xaxis_rangeslider_visible=False,
                              xaxis=dict(visible=False),
                              yaxis=dict(visible=False, range=[lo - pad, hi + pad]),
                              paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)", showlegend=False)
            st.plotly_chart(fig, use_container_width=True,
                            config={"displayModeBar": False}, key=f"idx_{nm}")


# ── 헤더 ──────────────────────────────────────────────────────────────
c1, c2 = st.columns([0.75, 0.25])
c1.markdown(
    "<h1 style='font-size:1.85rem;font-weight:800;margin:0.1rem 0 0'>Portfolio Dashboard</h1>",
    unsafe_allow_html=True)
if c2.button("시세 새로고침", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

# ── 글로벌 지수 티커 바 (최상단) ──────────────────────────────────────
render_index_bar()
st.caption("글로벌 지수: 전일 종가 대비 등락률 · 캔들=최근 90거래일(상승 빨강/하락 파랑) · "
           "60초마다 자동 갱신(무료 지연시세라 실시간 틱은 아님)")
st.divider()

df, isa, gold, sec = load()
total_principal = df["투자원금"].sum(skipna=True)
total_eval = df["평가금액"].sum(skipna=True)
total_pl = total_eval - total_principal
# 현금(예수금) 제외 투자원금 — '총 투자원금'·손익률 분모로 사용
cash_principal = df[df["섹터"] == "현금성자산"]["투자원금"].sum(skipna=True)
invested_principal = total_principal - cash_principal
pbeta = engine.compute_portfolio_beta(df)

# 오늘자 마감 성과 스냅샷 누적 기록(같은 날은 갱신)
_idx = load_indices()
_kospi = _idx.get("코스피", {}).get("price") if isinstance(_idx.get("코스피"), dict) else None
perf_hist = performance.record_today(total_principal, total_eval,
                                     isa.get("세후수익률"), pbeta.get("beta_all"), _kospi)

@st.cache_data(ttl=1800, show_spinner="수급 데이터 불러오는 중…")
def load_flows(pairs, days=60):
    out = {}
    for nm, code in pairs:
        tdf = flows.get_investor_trend(code, days)
        out[nm] = (code, tdf, flows.summarize(tdf))
    return out


@st.cache_data(ttl=1800, show_spinner="중장기 수급 불러오는 중…")
def load_longterm(code, pages=9):
    return flows.get_longterm_trend(code, pages)


@st.cache_data(ttl=600)
def render_aum_banner(aum: float):
    """AUM(운용자산 총액) 배너 이미지(matplotlib PNG)."""
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    fig = plt.figure(figsize=(12, 1.5), dpi=110)
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, color="#11233f"))
    ax.add_patch(plt.Rectangle((0, 0), 0.012, 1, color="#e03131"))
    ax.text(0.03, 0.66, "AUM · 운용자산 총액 (현금·금 포함 총 평가)", color="#9db2d3",
            fontsize=12.5, va="center")
    ax.text(0.03, 0.30, f"₩ {aum:,.0f}", color="#ffffff", fontsize=30,
            fontweight="bold", va="center")
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor="#11233f", bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return buf.getvalue()


@st.cache_data(ttl=3600, show_spinner=False)
def load_marketcaps():
    """전 KRX 주식·ETF 시가총액·전일대비 등락률·상장주식수 사전(코드→값). fdr.StockListing."""
    import FinanceDataReader as fdr
    caps, chgs, stks = {}, {}, {}
    for mkt in ["KRX", "ETF/KR"]:
        try:
            l = fdr.StockListing(mkt)
            ccol = "Code" if "Code" in l.columns else ("Symbol" if "Symbol" in l.columns else l.columns[0])
            mcol = next((c for c in ["Marcap", "MarCap", "Marketcap"] if c in l.columns), None)
            rcol = next((c for c in ["ChagesRatio", "ChangesRatio", "ChangeRatio"] if c in l.columns), None)
            scol = next((c for c in ["Stocks", "Shares", "ListedShares"] if c in l.columns), None)
            for _, r in l.iterrows():
                code = str(r[ccol]).zfill(6)
                if mcol:
                    caps[code] = r[mcol]
                if rcol:
                    chgs[code] = r[rcol]
                if scol:
                    stks[code] = r[scol]
        except Exception:
            pass
    return caps, chgs, stks


@st.cache_data(ttl=1800, show_spinner="산업 데이터 불러오는 중…")
def load_industry(fname: str):
    """industry_*.csv 로드 + 시가총액 자동 병합 + 영업이익률 계산."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    try:
        u = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame()
    caps, chgs, stks = load_marketcaps()
    for c in ["매출액", "영업이익", "특허수"]:
        if c in u.columns:
            u[c] = pd.to_numeric(u[c].astype(str).str.replace(",", "", regex=False), errors="coerce")
    u["티커"] = u["티커"].astype(str).str.strip().apply(lambda t: t.zfill(6) if t.isdigit() else t)
    # StockListing 스냅샷은 정규장 확정 종가 갱신이 늦어 등락률·시총이 네이버와 어긋남.
    # → fdr.DataReader(EOD, 네이버 공식종가와 일치)의 최근 2종가로 보정(실패 시 StockListing 폴백).
    import FinanceDataReader as fdr
    from concurrent.futures import ThreadPoolExecutor

    def _eod(t):
        try:
            c = fdr.DataReader(t)["Close"].dropna()
            if len(c) >= 2:
                return t, float(c.iloc[-1]), float(c.iloc[-2])
        except Exception:
            pass
        return t, None, None

    eod = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, last, prev in ex.map(_eod, list(u["티커"])):
            eod[t] = (last, prev)

    def _chg(t):
        last, prev = eod.get(t, (None, None))
        if last and prev and prev > 0:
            return (last / prev - 1) * 100
        return chgs.get(t, np.nan)

    def _cap(t):
        last, _ = eod.get(t, (None, None))
        s = stks.get(t)
        if last and s and s > 0:
            return last * s
        return caps.get(t, np.nan)

    u["시가총액"] = u["티커"].map(_cap)
    u["등락률"] = u["티커"].map(_chg)
    if "매출액" in u and "영업이익" in u:
        u["영업이익률"] = u["영업이익"] / u["매출액"]
    # 밸류체인 순서 정렬 (Upstream → Midstream → Downstream → 단계)
    gu_order = {"Upstream": 0, "Midstream": 1, "Downstream": 2}
    order = {"원자재": 0, "소재": 1, "셀": 2, "부품": 3, "장비": 4,
             "리사이클": 5, "애플리케이션": 6}
    u["_g"] = u["구분"].map(gu_order).fillna(9)
    u["_ord"] = u["단계"].map(order).fillna(9)
    u = u.sort_values(["_g", "_ord", "세부분류"]).drop(columns=["_g", "_ord"]).reset_index(drop=True)
    return u


@st.cache_data(ttl=1800, show_spinner="주가 수익률 계산 중…")
def load_industry_returns(fname: str):
    """산업 기업들의 최근 주가 수익률(5/30/60/120/240일·상장이후). fdr 전체이력."""
    import FinanceDataReader as fdr
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    try:
        u = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame()
    rows = []
    for _, r in u.iterrows():
        tk = str(r["티커"]).strip()
        tk = tk.zfill(6) if tk.isdigit() else tk
        try:
            c = fdr.DataReader(tk)["Close"].dropna()
        except Exception:
            continue
        if len(c) < 6:
            continue

        def _r(n):
            return (c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else np.nan
        rows.append({"기업명": r["기업명"], "단계": r["단계"], "티커": tk,
                     "5일": _r(5), "30일": _r(30), "60일": _r(60),
                     "120일": _r(120), "240일": _r(240),
                     "상장이후": c.iloc[-1] / c.iloc[0] - 1})
    return pd.DataFrame(rows)


@st.cache_data(ttl=86400, show_spinner="멀티플(밸류에이션) 계산 중… DART+주가 기준, 최초 1~3분 소요")
def load_multiples(fname: str):
    """산업 기업들의 밸류에이션 멀티플(5개년 Historical + Forward). DART+fdr+네이버."""
    import multiples as M
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    try:
        return M.build_multiple_long(path)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner="후보 종목 스크리닝 중…")
def load_screener():
    """universe.csv 후보를 퀀트 시그널로 스코어링(데이터·퀀트 기반 매력도)."""
    import quant
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe.csv")
    try:
        u = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame()
    rows = []
    for _, r in u.iterrows():
        tk = str(r["ticker"]).strip()
        if tk.isdigit():
            tk = tk.zfill(6)
        o = engine.get_ohlcv(tk, r.get("market", "KRX"))
        if o is None or len(o) < 60:
            continue
        ind = quant.compute_indicators(o)
        if not ind:
            continue
        close = o["Close"]
        vol = float(close.pct_change().dropna().std() * (252 ** 0.5))
        beta, _ = engine._beta_corr(close, engine.get_benchmark_series(config.MARKET_BENCHMARK))
        attractive = max(0.0, ind["시그널점수"] + 2) / max(vol, 0.08) if vol > 0 else 0.0
        rows.append({
            "종목명": r["name"], "티커": tk, "카테고리": r.get("category", "-"),
            "현재가": float(close.iloc[-1]), "RSI": ind["RSI"], "ADX": ind["ADX"],
            "추세": ind["추세"], "시그널점수": ind["시그널점수"], "베타": beta,
            "변동성%": vol * 100, "매력도": attractive,
            "상황해석": quant.interpret_row(ind, beta)})
    return (pd.DataFrame(rows)
            .sort_values(["시그널점수", "매력도"], ascending=[False, False])
            .reset_index(drop=True))


(tab_dash, tab_quant, tab_rebal, tab_flow, tab_perf, tab_screen,
 tab_flowret, tab_industry) = st.tabs(
    ["Portfolio Dashboard", "Quant Signals", "Rebalancing", "Supply/Demand",
     "Performance", "Screener", "Flow Analysis", "Industry"])

# ══════════════════════════════════════════════════════════════════════
# TAB 1 · Portfolio Dashboard
# ══════════════════════════════════════════════════════════════════════
with tab_dash:
    st.caption(f"기준시각 {dt.datetime.now():%Y-%m-%d %H:%M}  ·  베타/상관계수/초과수익률은 "
               f"최근 {config.LOOKBACK_DAYS}일, 지수추종 ETF 프록시 기준")

    k = st.columns(6)
    k[0].metric("AUM (운용자산)", won(total_eval),
                help="운용자산 총액 = 현금(예수금)·금현물 포함 총 평가금액.")
    k[1].metric("총 투자원금", won(invested_principal),
                help="예수금(현금) 제외, 실제 투자된 원금(주식·ETF·금).")
    k[2].metric("총 평가손익", won(total_pl), pct(total_pl / invested_principal))
    k[3].metric("ISA 세전수익률", pct(isa["세전수익률"]))
    k[4].metric("ISA 세후수익률", pct(isa["세후수익률"]),
                help="지금 전량 매도 가정. 계좌 손익통산 후 비과세한도 차감, 초과분 9.9% 분리과세.")
    k[5].metric("포트폴리오 베타",
                "-" if pd.isna(pbeta["beta_all"]) else f"{pbeta['beta_all']:.2f}",
                help=("코스피 대비 평가금액 가중 베타(현금·금은 0 취급). "
                      f"투자자산(현금 제외) 기준 {pbeta['beta_ex_cash']:.2f}. "
                      "1보다 낮을수록 코스피 등락에 덜 민감(방어적)."))

    with st.expander("ISA 세후 수익률 계산 상세 (계좌 단위 손익통산)", expanded=False):
        t = st.columns(4)
        t[0].metric("ISA 세전손익", won(isa["세전손익"]))
        t[1].metric("비과세 한도", won(isa["비과세한도"]),
                    help=f"현재 설정: {config.ISA_TYPE} (config.py에서 변경)")
        t[2].metric("과세대상 / 예상세금", f"{won(isa['과세대상'])} / {won(isa['예상세금'])}")
        t[3].metric("실효세율", pct(isa["실효세율"]))
        st.info("ISA는 종목별이 아니라 계좌 전체 손익통산 후 과세됩니다. "
                "위 세금은 지금 전량 매도 가정의 추정치이며, 실제로는 만기(3년 이상) "
                "해지 시점 손익으로 계산됩니다. 비과세 한도·세율은 매년 세법개정 확인 필요.")
        if gold:
            st.success(f"금현물(별도계좌): 평가금액 {won(gold['평가금액'])} · "
                       f"세전손익 {won(gold['세전손익'])} ({pct(gold['세전수익률'])}) · "
                       f"KRX금현물 매매차익은 비과세(세금 0) 가정")

    st.subheader("보유종목 현황")
    view = df[["계좌", "종목명", "티커", "섹터", "보유수량", "평균매입가", "현재가",
               "투자원금", "평가금액", "평가손익", "수익률", "베타", "상관계수",
               "초과수익률", "비중"]].copy()
    st.dataframe(
        view.style.format({
            "보유수량": "{:,.0f}", "평균매입가": "{:,.0f}", "현재가": "{:,.0f}",
            "투자원금": "{:,.0f}", "평가금액": "{:,.0f}", "평가손익": "{:,.0f}",
            "수익률": "{:+.2%}", "베타": "{:.2f}", "상관계수": "{:.2f}",
            "초과수익률": "{:+.2%}", "비중": "{:.1%}",
        }).map(lambda v: "color:#d62728" if isinstance(v, (int, float)) and v < 0
               else ("color:#2ca02c" if isinstance(v, (int, float)) and v > 0 else ""),
               subset=["평가손익", "수익률", "초과수익률"]),
        use_container_width=True, height=340)

    g1, g2 = st.columns(2)
    with g1:
        st.subheader("섹터별 투자 비중")
        fig = px.pie(sec, values="평가금액", names="섹터", hole=0.45)
        fig.update_traces(textposition="inside", texttemplate="%{label}<br>%{percent}")
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=380)
        st.plotly_chart(fig, use_container_width=True)
    with g2:
        st.subheader("종목별 수익률 / 초과수익률")
        bar = df.dropna(subset=["수익률"]).sort_values("수익률")
        fig2 = go.Figure()
        fig2.add_bar(y=bar["종목명"], x=bar["수익률"], orientation="h",
                     name="수익률", marker_color="#1f77b4")
        fig2.add_bar(y=bar["종목명"], x=bar["초과수익률"], orientation="h",
                     name="기초지수 대비 초과", marker_color="#ff7f0e")
        fig2.update_layout(barmode="group", xaxis_tickformat=".0%",
                           margin=dict(t=10, b=10, l=10, r=10), height=380,
                           legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig2, use_container_width=True)

    st.subheader("위험-수익 지형도 (베타 vs 수익률, 크기=평가금액)")
    sc = df.dropna(subset=["베타", "수익률"])
    fig3 = px.scatter(sc, x="베타", y="수익률", size="평가금액", color="섹터",
                      text="종목명", size_max=55)
    fig3.update_traces(textposition="top center")
    fig3.update_layout(yaxis_tickformat=".0%", height=420,
                       margin=dict(t=10, b=10, l=10, r=10))
    fig3.add_hline(y=0, line_dash="dot", line_color="gray")
    fig3.add_vline(x=1, line_dash="dot", line_color="gray", annotation_text="시장베타=1")
    st.plotly_chart(fig3, use_container_width=True)

    # ── 종목 상세 캔들차트 ──
    st.subheader("종목 상세 차트 (캔들 · 이동평균 · 볼린저밴드 · 거래량 · RSI)")
    chartable = df[~df["티커"].isin(["CASH", "GOLD"])]
    names = chartable["종목명"].tolist()
    sel = st.selectbox("종목 선택", names, index=0, key="chart_sel")
    row = chartable[chartable["종목명"] == sel].iloc[0]
    ohlcv = engine.get_ohlcv(str(row["티커"]), "KRX")

    # ── 3개 메시지 패널: 차트 단기신호 / 퀀트 시그널 / 리밸런싱 ──
    mk = chart.latest_marker(ohlcv)
    bd = f"{mk['buy']:%m/%d}" if mk["buy"] is not None else "없음"
    sd = f"{mk['sell']:%m/%d}" if mk["sell"] is not None else "없음"
    sc = row["시그널점수"]
    sc_txt = f"{int(sc):+d}점" if pd.notna(sc) else "—"
    cur_w = row["비중"] * 100
    tgt_w = row["퀀트목표비중"] if pd.notna(row["퀀트목표비중"]) else cur_w
    act = "매수" if tgt_w - cur_w > 0.3 else ("매도" if tgt_w - cur_w < -0.3 else "유지")
    mcol = st.columns(3)
    mcol[0].info(f"**① 차트 단기신호** (RSI·MA·볼린저)\n\n"
                 f"**{mk['state']}**\n\n최근 매수 {bd} · 최근 매도 {sd}")
    mcol[1].info(f"**② 퀀트 종합 시그널** (다요인)\n\n"
                 f"**{sc_txt}** · {row['추세']}\n\n{row['사유'] if isinstance(row['사유'], str) else '—'}")
    mcol[2].info(f"**③ 리밸런싱(목표 비중)**\n\n"
                 f"현재 {cur_w:.1f}% → 목표 {tgt_w:.1f}% **{act}**\n\n"
                 f"{'미보유분 전략 편입 필요' if cur_w < 0.1 and tgt_w > 0 else '퀀트 목표 기준'}")

    fig_c = chart.build_price_chart(ohlcv, sel)
    if fig_c is None:
        st.info("해당 종목은 시세 이력이 부족해 차트를 그릴 수 없습니다.")
    else:
        st.plotly_chart(fig_c, use_container_width=True)
        st.caption("⚠️ 위 차트의 Buy(초록▲)·Sell(빨강▼) 마커는 '단기 타이밍'용으로, "
                   "이동평균(MA5/MA20) 교차 · RSI(70/65) · 볼린저밴드 상단만 반영합니다. "
                   "→ 비중 결정에 쓰는 퀀트 종합 시그널(MACD·ADX·스토캐스틱·OBV·거래량 등 포함)·리밸런싱과는 "
                   "별개 지표입니다. 단기 과열로 Sell이 떠도 중기 추세가 강하면 퀀트는 비중 확대일 수 있습니다(매매 권유 아님).")

# ══════════════════════════════════════════════════════════════════════
# TAB 2 · Quant Signals
# ══════════════════════════════════════════════════════════════════════
with tab_quant:
    st.subheader("퀀트 시그널 & 자동 목표비중 (RSI · MACD · 이동평균 · ADX · 거래량)")
    st.caption("RSI(14)·MACD(12,26,9)·이동평균(20/60/120)·스토캐스틱·볼린저·OBV·ADX를 합성한 시그널점수(-3~+3)와 "
               "변동성을 결합해 목표비중을 산출합니다(수동 입력 없이 데이터·퀀트로만). "
               "종목 가중치 ∝ max(0, 시그널점수+2) ÷ 변동성 · 금현물 8% 고정 · 현금=잔여 · 종목당 상한 30%.")

    qv = df[["종목명", "RSI", "MACD히스토", "ADX", "베타", "변동성", "추세", "시그널점수",
             "비중", "퀀트목표비중", "상황해석"]].copy()
    qv["현재비중"] = qv["비중"] * 100
    qv["변동성%"] = qv["변동성"] * 100
    qv = qv.rename(columns={"퀀트목표비중": "퀀트목표(%)"})
    st.dataframe(
        qv[["종목명", "RSI", "MACD히스토", "ADX", "베타", "변동성%", "추세", "시그널점수",
            "현재비중", "퀀트목표(%)", "상황해석"]].style.format(
            {"RSI": "{:.0f}", "MACD히스토": "{:+,.0f}", "ADX": "{:.0f}", "베타": "{:.2f}",
             "변동성%": "{:.0f}%", "시그널점수": "{:+.0f}", "현재비중": "{:.1f}",
             "퀀트목표(%)": "{:.1f}"}, na_rep="—").map(
            lambda v: "color:#2ca02c" if isinstance(v, (int, float)) and v > 0
            else ("color:#d62728" if isinstance(v, (int, float)) and v < 0 else ""),
            subset=["시그널점수"]),
        use_container_width=True, hide_index=True, height=560,
        column_config={"상황해석": st.column_config.TextColumn("상황 해석(수치별 국면)", width="large")})

    with st.expander("전략 설명 — 장마감 기준 시그널 분석 & 비중 조정 제언", expanded=True):
        st.markdown(strategy_brief(df, total_eval))

# ══════════════════════════════════════════════════════════════════════
# TAB 3 · Rebalancing
# ══════════════════════════════════════════════════════════════════════
with tab_rebal:
    st.subheader("전략 수정 · 리밸런싱 시뮬레이터")
    st.caption("기본 목표비중은 Quant Signals 탭의 퀀트 자동 목표비중이 적용됩니다(표에서 직접 수정 가능). "
               "조정금액 = 총평가금액 × 목표비중 − 현재 평가금액.")

    with st.expander("투자 제안 로직 (목표비중 산정 방식) 보기", expanded=False):
        st.markdown(config.SIM_LOGIC)

    # 리밸런싱 제안 (퀀트 자동 목표비중 기준, 편집표 제거)
    merged = df.copy()
    merged["현재비중"] = (merged["비중"] * 100).round(1)
    merged["목표비중(%)"] = merged["퀀트목표비중"].fillna(merged["현재비중"]).round(1)
    merged["조정금액"] = total_eval * merged["목표비중(%)"] / 100 - merged["평가금액"]
    merged["액션"] = np.where(merged["조정금액"] > 0, "매수", "매도")
    merged["근거"] = merged["투자근거"]   # 실제 계산과 연동된 동적 근거

    tgt_sum = merged["목표비중(%)"].sum()
    if abs(tgt_sum - 100) > 0.1:
        st.warning(f"목표비중 합계가 {tgt_sum:.1f}% 입니다.")
    st.dataframe(
        merged[["종목명", "평가금액", "현재비중", "목표비중(%)", "조정금액", "액션", "근거"]].style.format(
            {"평가금액": "{:,.0f}원", "현재비중": "{:.1f}", "목표비중(%)": "{:.1f}",
             "조정금액": "{:+,.0f}원"}),
        use_container_width=True, hide_index=True,
        column_config={"근거": st.column_config.TextColumn("근거", width="large")})
    st.caption("종목별 지표 상세·수치 해석은 'Quant Signals' 탭에서 확인하세요.")

# ══════════════════════════════════════════════════════════════════════
# TAB 4 · Supply/Demand (투자자별 수급 동향)
# ══════════════════════════════════════════════════════════════════════
with tab_flow:
    st.subheader("투자자별 수급 동향 (외국인 · 기관 · 개인 순매수)")
    st.caption("출처: 네이버 금융 일별 투자자 순매수. 새로고침 시 갱신 · 지수는 대표 ETF 프록시(코스피=KODEX200, 코스닥=KODEX코스닥150).")

    # ── 사용자 선택: 기간 + 수급처 ──
    cc = st.columns([0.32, 0.68])
    PERIODS = {"1일": 1, "5일": 5, "20일": 20, "60일": 60}
    period_lbl = cc[0].selectbox("누적 기간", list(PERIODS.keys()), index=2, key="flow_period",
                                 help="1일 = 최근 거래일(당일 장마감 기준, 장중이면 전일 마감).")
    period = PERIODS[period_lbl]
    invs = cc[1].multiselect("수급처 선택", ["외국인", "기관", "개인"],
                             default=["외국인", "기관", "개인"], key="flow_invs")
    if not invs:
        invs = ["외국인", "기관", "개인"]

    krx = df[~df["티커"].isin(["CASH", "GOLD"])]
    pairs = (("[지수] 코스피", "069500"), ("[지수] 코스닥", "229200")) + tuple(
        (r["종목명"], str(r["티커"])) for _, r in krx.iterrows())
    fdata = load_flows(pairs, 60)
    score_map = dict(zip(df["종목명"], df["시그널점수"]))

    # 수급 요약 표 — 선택 기간·수급처 누적 순매수
    rows = []
    for nm, (code, tdf, sm) in fdata.items():
        if sm is None or len(tdf) == 0:
            continue
        rec = {"종목/지수": nm}
        for inv in invs:
            rec[f"{inv} {period_lbl}"] = tdf[inv].tail(period).sum()
        rec["외국인보유율"] = sm["외국인보유율"]
        hs = tdf["외국인보유율"]          # 보유율Δ도 선택 기간과 동일 horizon으로
        rec["보유율Δ"] = (hs.iloc[-1] - hs.iloc[-(period + 1)]
                         if len(hs) > period else hs.iloc[-1] - hs.iloc[0])
        rec["시사점"] = flows.insight(nm, sm, score_map.get(nm))
        rows.append(rec)
    fsum = pd.DataFrame(rows)
    if len(fsum):
        numcols = [c for c in fsum.columns if c not in ("종목/지수", "시사점")]
        fmt = {c: ("{:+,.0f}" if "보유" not in c else "{:.2f}%") for c in numcols}
        fmt = {c: ("{:+.2f}%p" if c == "보유율Δ" else fmt[c]) for c in numcols}
        st.dataframe(
            fsum.style.format(fmt).map(
                lambda v: "color:#d62728" if isinstance(v, (int, float)) and v < 0
                else ("color:#2ca02c" if isinstance(v, (int, float)) and v > 0 else ""),
                subset=[c for c in numcols if c != "외국인보유율"]),
            use_container_width=True, hide_index=True, height=460,
            column_config={"시사점": st.column_config.TextColumn("시사점 (수급+퀀트 결합)", width="large")})
        _basis = "최근 거래일(당일 장마감, 장중이면 전일 마감)" if period == 1 else f"최근 {period_lbl} 누적"
        st.caption(f"표는 {_basis} 순매수(주) 기준 · 보유율Δ도 동일 기간({period_lbl}) 변화 · "
                   "시사점은 20일 기준 종합 해석.")

        # 포트폴리오 종합 시사점
        buy_f = [nm for nm, (c, t, s) in fdata.items()
                 if s and t["외국인"].tail(20).sum() > 0 and not nm.startswith("[지수]")]
        sell_f = [nm for nm, (c, t, s) in fdata.items()
                  if s and t["외국인"].tail(20).sum() < 0 and not nm.startswith("[지수]")]
        ksp = fdata.get("[지수] 코스피")
        market_txt = ""
        if ksp and ksp[2]:
            v = ksp[1]["외국인"].tail(20).sum()
            market_txt = f"코스피 전반 외국인 {'순매수' if v > 0 else '순매도'}({v:+,.0f}주, 20일) → "
        st.info(
            f"**포트폴리오 수급 종합** · {market_txt}"
            f"외국인 20일 순매수 종목: {', '.join(buy_f) if buy_f else '없음'} / "
            f"순매도 종목: {', '.join(sell_f) if sell_f else '없음'}.  "
            "외국인·기관 동반 순매수 종목은 비중 확대 근거가 강화되고, 동반 이탈 종목은 "
            "기술 시그널이 약하면 우선 축소 대상입니다(수급+기술 결합 판단).")

    # 종목별 상세 수급 차트 (선택 기간·수급처 반영)
    st.subheader("종목/지수별 수급 상세")
    names = [r["종목/지수"] for r in rows] or [p[0] for p in pairs]
    sel = st.selectbox("종목/지수 선택", names, index=0, key="flow_sel")
    code_s, tdf_s, _ = fdata[sel]
    figf = flows.build_flow_chart(tdf_s, sel, days=period, investors=invs)
    if figf is None:
        st.info("해당 종목의 수급 데이터를 불러오지 못했습니다.")
    else:
        st.plotly_chart(figf, use_container_width=True)
        st.caption("막대=일별 순매수 수량(선택 수급처) · 하단 빨강선=외국인 보유율(%), 점선=종가.")

    # 중장기(~1년) 누적 추세 — 외국인·기관 (개인은 소스 미제공)
    with st.expander("중장기(~1년) 누적 순매매 추세 보기 (외국인·기관)", expanded=False):
        st.caption("네이버 frgn 자료 기준 약 1년치. 누적선이 우상향=지속 매집, 우하향=지속 분산.")
        lt = load_longterm(code_s, 9)
        figl = flows.build_longterm_chart(lt, sel)
        if figl is None:
            st.info("중장기 데이터를 불러오지 못했습니다(지수 프록시·신형 ETF는 제한될 수 있음).")
        else:
            st.plotly_chart(figl, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════
# TAB 5 · Performance (일별 마감 성과 누적)
# ══════════════════════════════════════════════════════════════════════
with tab_perf:
    st.subheader("일별 마감 포트폴리오 성과 (누적 기록)")
    st.caption("앱을 열 때마다 '오늘' 종가 기준 스냅샷이 performance_history.csv에 저장되어 매 거래일 누적됩니다. "
               "1~2년 뒤 일별 수익률·변동 추이를 한눈에 보기 위한 기록입니다.")

    d = performance.add_cumulative(perf_hist)
    if len(d):
        last = d.iloc[-1]
        first = d.iloc[0]
        kk = st.columns(5)
        kk[0].metric("추적 시작일", f"{first['날짜']:%Y-%m-%d}")
        kk[1].metric("기록 일수", f"{len(d)}일")
        kk[2].metric("현재 평가금액", won(last["평가금액"]))
        kk[3].metric("누적 수익률(원금대비)", f"{last['포트수익률%']:+.2f}%")
        if pd.notna(last.get("KOSPI누적%")):
            kk[4].metric("KOSPI 누적수익률", f"{last['KOSPI누적%']:+.2f}%",
                         help="추적 시작일 대비 코스피 등락률(비교용 벤치마크).")
        else:
            kk[4].metric("KOSPI 누적수익률", "-")

        # 전일 대비 일간 변동 (포트 vs 코스피) — 상단 5칸 그리드에 맞춰 정렬
        if len(d) >= 2:
            pv, kv, ex = last["포트전일대비%"], last["KOSPI전일대비%"], last["일간초과%"]
            dd = st.columns(5)
            dd[0].metric("포트 전일대비", "-" if pd.isna(pv) else f"{pv:+.2f}%",
                         help="당일 평가금액 변동률(입출금·매매일엔 왜곡 가능).")
            dd[1].metric("KOSPI 전일대비", "-" if pd.isna(kv) else f"{kv:+.2f}%")
            dd[2].metric("일간 초과(포트−코스피)", "-" if pd.isna(ex) else f"{ex:+.2f}%p",
                         help="당일 포트 변동률 − 코스피 변동률. +면 시장보다 선방, −면 시장보다 부진.")

    figp = performance.build_perf_chart(perf_hist)
    if figp is not None:
        st.plotly_chart(figp, use_container_width=True)
    if len(d) <= 1:
        st.info("아직 기록이 1일치입니다. 매 거래일 앱을 열면(또는 새로고침) 그날 마감 성과가 자동으로 누적되어 "
                "추세 그래프가 점점 채워집니다.")

    st.subheader("일자별 성과 기록")
    show = d[["날짜", "평가금액", "평가손익", "수익률",
              "포트전일대비%", "KOSPI전일대비%", "일간초과%",
              "KOSPI", "KOSPI누적%"]].sort_values("날짜", ascending=False).copy()
    show["날짜"] = show["날짜"].dt.strftime("%Y-%m-%d")
    st.dataframe(
        show.style.format({"평가금액": "{:,.0f}원", "평가손익": "{:+,.0f}원", "수익률": "{:+.2%}",
                           "포트전일대비%": "{:+.2f}%", "KOSPI전일대비%": "{:+.2f}%",
                           "일간초과%": "{:+.2f}%p", "KOSPI": "{:,.2f}",
                           "KOSPI누적%": "{:+.2f}%"}, na_rep="—").map(
            lambda v: "color:#d62728" if isinstance(v, (int, float)) and v < 0
            else ("color:#2ca02c" if isinstance(v, (int, float)) and v > 0 else ""),
            subset=["평가손익", "포트전일대비%", "KOSPI전일대비%", "일간초과%", "KOSPI누적%"]),
        use_container_width=True, hide_index=True, height=420)
    st.caption("※ '수익률'은 원금 대비 누적 손익률이며, 추가 입금/출금 시 일시적으로 희석·변동될 수 있습니다. "
               "현금흐름 영향을 배제한 정밀 비교(시간가중수익률, TWR)는 추후 추가 가능합니다.")

# ══════════════════════════════════════════════════════════════════════
# TAB 6 · Screener (퀀트 시그널 기반 ISA 매수후보 추천)
# ══════════════════════════════════════════════════════════════════════
with tab_screen:
    st.subheader("ISA 매수후보 스크리너 (퀀트 시그널 기반)")
    st.caption("universe.csv의 ISA 매수가능 후보(ETF·대형주)를 동일 퀀트 지표로 스코어링합니다. "
               "매력도 = max(0, 시그널점수+2) ÷ 변동성 (시그널 강할수록·저변동일수록↑). "
               "후보 추가/삭제는 universe.csv 편집 → 새로고침.")

    sc_df = load_screener()
    held = set(df["티커"].astype(str))
    if len(sc_df) == 0:
        st.info("스크리너 데이터를 불러오지 못했습니다. universe.csv를 확인하세요.")
    else:
        sc_df = sc_df.copy()
        sc_df["보유"] = sc_df["티커"].apply(lambda t: "보유/관심" if t in held else "신규")
        # 신규 매수후보 TOP
        new_top = sc_df[sc_df["보유"] == "신규"].head(5)
        if len(new_top):
            picks = " · ".join(f"{r['종목명']}({r['카테고리']}, 시그널 {int(r['시그널점수']):+d}·변동성 {r['변동성%']:.0f}%)"
                               for _, r in new_top.iterrows())
            st.success(f"**신규 매수후보 TOP5 (매력도순)**  →  {picks}")
        st.dataframe(
            sc_df[["종목명", "카테고리", "보유", "현재가", "시그널점수", "RSI", "ADX",
                   "베타", "변동성%", "매력도", "상황해석"]].style.format(
                {"현재가": "{:,.0f}", "시그널점수": "{:+.0f}", "RSI": "{:.0f}", "ADX": "{:.0f}",
                 "베타": "{:.2f}", "변동성%": "{:.0f}%", "매력도": "{:.1f}"}, na_rep="—").map(
                lambda v: "color:#2ca02c" if isinstance(v, (int, float)) and v > 0
                else ("color:#d62728" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["시그널점수"]),
            use_container_width=True, hide_index=True, height=560,
            column_config={"상황해석": st.column_config.TextColumn("상황 해석", width="large")})
        st.caption("매력도 = 비중 산출과 동일 로직(시그널강도÷변동성). 높을수록 '신호 강+저변동' 매력. "
                   "‘신규’ = 현재 미보유 후보. 실제 매수 시 해당 종목을 holdings.csv에 추가하면 포트폴리오에 편입됩니다. "
                   "교육용 참고이며 투자 권유가 아닙니다.")

        # ── 후보 종목 상세 차트 (Portfolio Dashboard 하단과 동일 구성) ──
        st.subheader("후보 종목 상세 차트 (캔들 · 이동평균 · 볼린저밴드 · 거래량 · RSI)")
        scn = sc_df["종목명"].tolist()
        ssel = st.selectbox("후보 종목 선택", scn, index=0, key="screen_chart_sel")
        srow = sc_df[sc_df["종목명"] == ssel].iloc[0]
        stk = str(srow["티커"])
        sohlcv = engine.get_ohlcv(stk, "KRX")
        smk = chart.latest_marker(sohlcv)
        sbd = f"{smk['buy']:%m/%d}" if smk["buy"] is not None else "없음"
        ssd = f"{smk['sell']:%m/%d}" if smk["sell"] is not None else "없음"
        scc = st.columns(3)
        scc[0].info(f"**① 차트 단기신호** (RSI·MA·볼린저)\n\n"
                    f"**{smk['state']}**\n\n최근 매수 {sbd} · 최근 매도 {ssd}")
        scc[1].info(f"**② 퀀트 종합 시그널** (다요인)\n\n"
                    f"**{int(srow['시그널점수']):+d}점** · {srow['추세']}\n\n{srow['상황해석']}")
        scc[2].info(f"**③ 스크리너 정보**\n\n"
                    f"매력도 **{srow['매력도']:.1f}** · {srow['보유']}\n\n"
                    f"{srow['카테고리']} · 변동성 {srow['변동성%']:.0f}%")
        sfig = chart.build_price_chart(sohlcv, ssel)
        if sfig is None:
            st.info("해당 종목은 시세 이력이 부족해 차트를 그릴 수 없습니다.")
        else:
            st.plotly_chart(sfig, use_container_width=True)
            st.caption("⚠️ Buy(초록▲)·Sell(빨강▼) 마커는 단기 타이밍용(MA교차·RSI·볼린저)이며, "
                       "비중 산출에 쓰는 퀀트 종합 시그널과는 별개입니다(교육용 참고).")

# ══════════════════════════════════════════════════════════════════════
# TAB 7 · Flow Analysis (외국인 수급 → 선행수익률 탐색)
# ══════════════════════════════════════════════════════════════════════
with tab_flowret:
    st.subheader("외국인 수급 → 선행수익률 분석 (과거 경향 탐색)")
    st.warning("⚠️ 이것은 **예측 모델이 아니라 '과거 경향 탐색'**입니다. 무료 데이터 한계로 표본이 "
               "약 1년(~250일)이라 통계적 신뢰도가 낮고, 상관계수가 0에 가까우면 예측력이 사실상 없다는 뜻입니다. "
               "투자 판단의 단독 근거로 쓰지 마세요.")

    krx2 = df[~df["티커"].isin(["CASH", "GOLD"])]
    fa_names = krx2["종목명"].tolist()
    csel = st.selectbox("종목 선택", fa_names, index=0, key="flowret_sel")
    ctk = str(krx2[krx2["종목명"] == csel].iloc[0]["티커"])
    study, meta = flows.flow_return_study(ctk)
    if study is None or len(study) == 0:
        st.info("해당 종목은 수급-수익률 분석에 필요한 이력이 부족합니다.")
    else:
        st.caption(f"표본: {meta['표본일수']}일 ({meta['시작']:%Y-%m-%d} ~ {meta['끝']:%Y-%m-%d}) · "
                   "외국인 일별 순매수 기준")
        st.dataframe(
            study.style.format({
                "상관계수": "{:+.2f}",
                "외인매수일 평균": "{:+.2%}", "매수일 상승확률": "{:.0%}",
                "외인매도일 평균": "{:+.2%}", "매도일 상승확률": "{:.0%}",
                "강매수(상위25%) 평균": "{:+.2%}", "강매수 상승확률": "{:.0%}"}, na_rep="—").map(
                lambda v: "color:#d62728" if isinstance(v, (int, float)) and v < 0
                else ("color:#2ca02c" if isinstance(v, (int, float)) and v > 0 else ""),
                subset=["상관계수", "외인매수일 평균", "외인매도일 평균", "강매수(상위25%) 평균"]),
            use_container_width=True, hide_index=True)

        hsel = st.radio("산점도 선행기간", [5, 10, 20], index=1, horizontal=True, key="flowret_h")
        figs = flows.build_flow_scatter(ctk, hsel)
        if figs is not None:
            st.plotly_chart(figs, use_container_width=True)
        st.caption("해석: 상관계수 ≈ 0 또는 점들이 흩어져 있으면 '외인 순매수→수익률' 관계가 약함. "
                   "강매수(상위25%) 평균이 음수면 '많이 살수록 단기 눌림'(추격매수 함정) 경향. "
                   "→ 신뢰할 확률 예측은 KRX 다년 데이터 연동 후에 가능(별도 진행 예정).")

# ══════════════════════════════════════════════════════════════════════
# TAB 8 · Industry (산업 분석) — 상위탭 / 하위탭(산업별)
# ══════════════════════════════════════════════════════════════════════
with tab_industry:
    st.subheader("산업 분석 (Industry)")
    st.caption("산업별 밸류체인·기업·재무·시장정보. 2차전지부터 구축하고, 이후 동일 구조로 9개 산업을 복제 예정. "
               "기업/재무 데이터는 industry_<산업>.csv 편집으로 갱신(시가총액은 자동 조회).")

    isub = st.tabs(["2차전지", "반도체", "AI", "모빌리티", "기타(예정)"])

    with isub[0]:
        st.markdown("#### 2차전지 밸류체인 (Upstream → Midstream → Downstream)")
        st.markdown(
            "- **Upstream (원자재)**: 리튬·니켈·코발트·**전구체** 등 원료\n"
            "- **Midstream (소재·부품·장비)**: 양극재·음극재·분리막·전해질·전지박(동박)·도전재/바인더 / "
            "배터리 셀·캔·BMS / 제조장비 / 리사이클(폐배터리)\n"
            "- **Downstream (전방수요)**: 자동차(EV)·ESS·드론/UAM·IT기기 제조업체")

        dc = st.columns([0.26, 0.74])
        if dart.has_key():
            if dc[0].button("DART 재무 자동 갱신", key="dart_batt", use_container_width=True):
                with st.spinner("DART에서 매출·영업이익 수집 중…"):
                    _ipath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "industry_2차전지.csv")
                    _, _n, _fl = dart.update_industry_csv(_ipath)
                st.cache_data.clear()
                st.success(f"DART 재무 갱신 완료 ({_fl}/{_n}개 기업, 최신 사업보고서·연결 기준)")
                st.rerun()
            dc[1].caption("버튼 클릭 시 DART 전자공시에서 매출액·영업이익(최신 연간·연결)을 자동 수집해 채웁니다.")
        else:
            st.caption("DART 키(dart_key.txt)가 없어 재무 자동갱신은 비활성입니다(매출·영업이익 수동 입력).")

        ind = load_industry("industry_2차전지.csv")
        if len(ind) == 0:
            st.info("industry_2차전지.csv를 불러오지 못했습니다.")
        else:
            # ── 트리맵: Up/Mid/Down → 단계 → 기업 (시가총액=시장규모 비례) ──
            st.markdown("##### 시가총액 히트맵 (박스·색 = 시가총액 / 클수록 진한 와인색)")
            import math
            tm = ind.dropna(subset=["시가총액"]).copy()
            tm["시총조"] = tm["시가총액"] / 1e12
            tm["등락"] = pd.to_numeric(tm["등락률"], errors="coerce").fillna(0.0)
            # 스트림별 색 계열(연한→진한), 채도 수준은 유사하게 · 끝점 대비 강화
            PAL = {"Upstream": ((178, 206, 238), (14, 40, 92)),    # 파랑(finviz 톤 조화)
                   "Midstream": ((228, 152, 152), (78, 12, 12)),    # 레드(finviz)
                   "Downstream": ((158, 224, 176), (12, 78, 34))}   # 그린(finviz)
            # 스트림별 자체 min~max로 정규화 → 같은 계열 안에서 색 차이 뚜렷
            _rng = {gu: (g["시가총액"].min(), g["시가총액"].max())
                    for gu, g in tm.groupby("구분")}

            def _shade(cap, gu):  # 시총 클수록 진함, 작을수록 연함 (스트림 내 로그 스케일)
                a, b = PAL.get(gu, ((150, 150, 160), (60, 60, 70)))
                lo, hi = _rng.get(gu, (cap, cap))
                if not (cap and cap > 0) or hi <= lo:
                    t = 0.5
                else:
                    t = (math.log(cap) - math.log(lo)) / (math.log(hi) - math.log(lo))
                    t = max(0.0, min(1.0, t))
                return "rgb(%d,%d,%d)" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

            ids, labels, parents, vals, cols, txts, cds = [], [], [], [], [], [], []
            ROOT = "2차전지"
            ids.append(ROOT); labels.append(ROOT); parents.append(""); vals.append(0)
            cols.append("#171a21"); txts.append(""); cds.append(0.0)
            for gu in tm["구분"].dropna().unique():
                gid = f"{ROOT}/{gu}"
                ids.append(gid); labels.append(gu); parents.append(ROOT); vals.append(0)
                cols.append("#171a21"); txts.append(""); cds.append(0.0)
                sg = tm[tm["구분"] == gu]
                for dan in sg["단계"].dropna().unique():
                    did = f"{gid}/{dan}"
                    ids.append(did); labels.append(dan); parents.append(gid); vals.append(0)
                    cols.append("#566273"); txts.append(""); cds.append(0.0)  # 단계 그룹 프레임(회색=가시화)
                    for _, r in sg[sg["단계"] == dan].iterrows():
                        ids.append(f"{did}/{r['기업명']}"); labels.append(r["기업명"]); parents.append(did)
                        vals.append(float(r["시가총액"])); cols.append(_shade(r["시가총액"], gu))
                        txts.append(f"{r['시총조']:,.2f}조<br>{r['등락']:+.2f}%")
                        cds.append(float(r["시총조"]))
            figt = go.Figure(go.Treemap(
                ids=ids, labels=labels, parents=parents, values=vals, text=txts, customdata=cds,
                branchvalues="remainder", texttemplate="%{label}<br>%{text}",
                marker=dict(colors=cols, line=dict(width=0.5, color="#0e1117")),
                textfont=dict(size=15, color="#ffffff"), tiling=dict(pad=4),
                pathbar=dict(visible=True, side="top", thickness=26,
                             textfont=dict(size=14, color="#ffffff")),
                hovertemplate="<b>%{label}</b><br>시가총액 %{customdata:,.2f}조원<extra></extra>"))
            figt.update_layout(height=580, margin=dict(t=40, b=6, l=6, r=6), paper_bgcolor="#0e1117")
            st.plotly_chart(figt, use_container_width=True)
            st.caption("박스 크기·색 = 시가총액(클수록 진한 와인색) · 박스 내 표기 = 시가총액(조원) + 전일대비 변동률% · 영역 클릭=확대/축소.")

            # ── 필터 + 보기 모드 ──
            c1, c2 = st.columns(2)
            gu = c1.radio("스트림", ["전체", "Upstream", "Midstream", "Downstream"],
                          horizontal=True, key="ind_gu")
            mode = c2.radio("표 보기", ["재무", "주가수익률", "Multiple"],
                            horizontal=True, key="ind_mode")
            view = ind if gu == "전체" else ind[ind["구분"] == gu]

            if mode == "재무":
                show = view[["구분", "단계", "세부분류", "기업명", "티커", "주요제품",
                             "시가총액", "매출액", "영업이익", "영업이익률",
                             "시장점유율", "주요수출국", "고객사", "특허수", "비고"]].copy()
                for c in ["시가총액", "매출액", "영업이익"]:
                    show[c] = show[c] / 1_000_000
                show = show.rename(columns={"시가총액": "시가총액(백만)", "매출액": "매출액(백만)",
                                            "영업이익": "영업이익(백만)", "주요제품": "주요제품(FY25 기준)"})
                st.dataframe(
                    show.style.format({"시가총액(백만)": "{:,.0f}", "매출액(백만)": "{:,.0f}",
                                       "영업이익(백만)": "{:,.0f}", "영업이익률": "{:.1%}",
                                       "특허수": "{:,.0f}"}, na_rep="—"),
                    use_container_width=True, hide_index=True, height=520,
                    column_config={"주요제품(FY25 기준)": st.column_config.TextColumn(width="medium")})
                st.caption("금액 단위 **백만원**. 시가총액=자동조회, 매출·영업이익=DART(연결). "
                           "점유율·수출·고객사·특허수는 industry_2차전지.csv 직접 입력(영업이익률 자동).")
            elif mode == "주가수익률":
                rdf = load_industry_returns("industry_2차전지.csv")
                if gu != "전체" and len(rdf):
                    rdf = rdf[rdf["기업명"].isin(view["기업명"])]
                rcols = ["5일", "30일", "60일", "120일", "240일", "상장이후"]
                st.dataframe(
                    rdf[["기업명", "단계", "티커"] + rcols].style.format(
                        {c: "{:+.1%}" for c in rcols}, na_rep="—").map(
                        lambda v: "color:#d62728" if isinstance(v, (int, float)) and v < 0
                        else ("color:#2ca02c" if isinstance(v, (int, float)) and v > 0 else ""),
                        subset=rcols),
                    use_container_width=True, hide_index=True, height=520)
                st.caption("주가 수익률(거래일 기준): 5/30/60/120/240일 및 상장이후 누적 · FinanceDataReader 전체이력 기준.")
            else:  # Multiple (밸류에이션)
                if not dart.has_key():
                    st.info("Multiple(밸류에이션) 자동계산은 DART 키가 필요합니다(dart_key.txt).")
                else:
                    metric = st.radio("지표", ["PER", "P/B", "ROE", "EV/EBIT", "EV/EBITDA", "PEG"],
                                      horizontal=True, key="ind_mult_metric")
                    mdf = load_multiples("industry_2차전지.csv")
                    if len(mdf) == 0:
                        st.info("멀티플 데이터를 계산하지 못했습니다.")
                    else:
                        if gu != "전체":
                            mdf = mdf[mdf["기업명"].isin(view["기업명"])]
                        sub = mdf[mdf["지표"] == metric].copy()
                        pcols = ["FY2021", "FY2022", "FY2023", "FY2024", "FY2025", "Forward"]
                        show = sub[["기업명", "단계", "티커"] + pcols]
                        unit = "%" if metric == "ROE" else "배"
                        fmt = "{:,.1f}%" if metric == "ROE" else "{:,.1f}"
                        sty = show.style.format({c: fmt for c in pcols}, na_rep="—")
                        if metric == "ROE":
                            sty = sty.map(
                                lambda v: "color:#d62728" if isinstance(v, (int, float)) and v < 0
                                else "", subset=pcols)
                        st.dataframe(sty, use_container_width=True, hide_index=True, height=520,
                                     column_config={"Forward": st.column_config.NumberColumn(
                                         help="네이버 컨센서스(추정연도) · 커버리지 있는 종목만")})
                        _na = int(sub[pcols[:-1]].isna().all(axis=1).sum())
                        _fw = int(sub["Forward"].notna().sum())
                        st.caption(
                            f"단위 **{unit}** · 과거 5개년=DART(연결)+주가 자동계산(과거 시총=현재시총×종가비, 상장주식수 불변 가정), "
                            f"Forward=네이버 컨센서스(추정연도, 커버 {_fw}/{len(sub)}개). "
                            "순이익·영업이익 적자 연도는 PER·EV/EBIT·PEG가 N/A(—). "
                            "EV/EBITDA는 DART가 감가상각비를 표준 제공하지 않아 상당수 —(best-effort), EV/EBIT을 함께 참고하세요. "
                            "EV/EBIT·EV/EBITDA Forward는 무료 컨센서스 미제공으로 —.")

    for i, nm in enumerate(["반도체", "AI", "모빌리티", "기타(예정)"], start=1):
        with isub[i]:
            st.info(f"**{nm}** 산업 탭은 준비 중입니다. 2차전지 분석 시스템을 완성한 뒤 "
                    "동일 구조(밸류체인 + 기업·재무·시장정보)로 복제할 예정입니다.")
