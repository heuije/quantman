"""신규 고급 블록 — 비전이 요구하나 기존 엔진엔 없던 연산.

명세 §3: 축2b(시계열 관계)·축3 dispersion·축4 group_rank/aggregate·축1 bucket·
축2a 시계열 동역학(halflife/autocorr). 보강① 포함.

기존 코드에 없어 새로 구현 — 수학적 속성으로 검증(test_blocks_advanced.py).
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from ..expression_parser import get_symbol_group
from .catalog import BlockDef, register
from .context import as_bool_panel
from .types import ValueType


# ── 축2b: 시계열 간 관계 ──────────────────────────────────────────────────────

def _ev_ts_corr(resolved, params, ctx):
    """두 패널의 롤링 상관(동행성). 종목별(컬럼별) 계산."""
    a, b = resolved["a"], resolved["b"]
    w = int(params.get("window", 20))
    out = {}
    for col in a.columns:
        out[col] = a[col].rolling(w).corr(b[col]) if col in b.columns else np.nan
    return pd.DataFrame(out, index=a.index)


def _ev_ts_regression(resolved, params, ctx):
    """롤링 선형적합 — output=beta(민감도)·residual(고유 움직임)·r2(적합 직선성).

    r2 = cov²/(var_x·var_y) = 결정계수[0,1]. bar_index를 x로 쓰면 추세의 직선성(추세강도
    필터 — slope×r2). beta=기울기, r2=매끄러움. 추세추종 알파가 추세 직선성에 종속되는지.
    """
    y, x = resolved["y"], resolved["x"]
    w = int(params.get("window", 20))
    kind = params.get("output", "beta")
    res = {}
    for col in y.columns:
        if col not in x.columns:
            res[col] = pd.Series(np.nan, index=y.index)
            continue
        yc, xc = y[col], x[col]
        cov = yc.rolling(w).cov(xc)
        var = xc.rolling(w).var().replace(0, np.nan)
        beta = cov / var
        if kind == "beta":
            res[col] = beta
        elif kind == "r2":
            var_y = yc.rolling(w).var().replace(0, np.nan)
            res[col] = (cov ** 2) / (var * var_y)
        else:  # residual = y - (alpha + beta*x), alpha = mean_y - beta*mean_x
            alpha = yc.rolling(w).mean() - beta * xc.rolling(w).mean()
            res[col] = yc - (alpha + beta * xc)
    return pd.DataFrame(res, index=y.index)


def _ev_orthogonalize(resolved, params, ctx):
    """횡단 직교화 — 매 날짜 a를 b로 회귀한 잔차(b 방향 성분 제거)."""
    a, b = resolved["a"], resolved["b"]
    res = pd.DataFrame(np.nan, index=a.index, columns=a.columns, dtype=float)
    for t in a.index:
        av, bv = a.loc[t], b.loc[t]
        m = av.notna() & bv.notna()
        if int(m.sum()) >= 2:
            xv = bv[m].to_numpy(dtype=float)
            yv = av[m].to_numpy(dtype=float)
            xm, ym = xv.mean(), yv.mean()
            denom = ((xv - xm) ** 2).sum()
            beta = ((xv - xm) * (yv - ym)).sum() / denom if denom > 0 else 0.0
            res.loc[t, m] = yv - (ym + beta * (xv - xm))
    return res


# ── 축4: 그룹 ─────────────────────────────────────────────────────────────────

def _groups(cols, group_type):
    mapping = {c: get_symbol_group(c, group_type) for c in cols}
    g: dict = {}
    for c, grp in mapping.items():
        g.setdefault(grp, []).append(c)
    return g


def _ev_group_rank(resolved, params, ctx):
    x = resolved["signal"]
    out = pd.DataFrame(np.nan, index=x.index, columns=x.columns, dtype=float)
    for _, cols in _groups(x.columns, params.get("group_type", "Industry")).items():
        out[cols] = x[cols].rank(axis=1, pct=True)
    return out


def _ev_group_aggregate(resolved, params, ctx):
    x = resolved["signal"]
    stat = params.get("stat", "mean")
    out = pd.DataFrame(np.nan, index=x.index, columns=x.columns, dtype=float)
    for _, cols in _groups(x.columns, params.get("group_type", "Industry")).items():
        agg = x[cols].sum(axis=1) if stat == "sum" else x[cols].mean(axis=1)
        for c in cols:
            out[c] = agg
    return out


# ── 축3: 횡단 산포 ────────────────────────────────────────────────────────────

def _ev_cs_dispersion(resolved, params, ctx):
    """그날 종목값의 횡단 표준편차(산포)를 전 종목에 브로드캐스트."""
    x = resolved["signal"]
    disp = x.std(axis=1)
    return pd.DataFrame({c: disp for c in x.columns}, index=x.index)


# ── 축1: 구간분할 → label ─────────────────────────────────────────────────────

def _ev_bucket(resolved, params, ctx):
    """경계값 리스트로 값을 구간 라벨(0..len(edges))로. NaN 보존."""
    x = resolved["signal"]
    edges = params["edges"]
    arr = x.to_numpy(dtype=float)
    out = np.digitize(arr, bins=np.asarray(edges, dtype=float), right=False).astype(float)
    out[np.isnan(arr)] = np.nan
    return pd.DataFrame(out, index=x.index, columns=x.columns)


# ── 축2a: 시계열 동역학 (보강①) ──────────────────────────────────────────────

def _ev_ts_autocorr(resolved, params, ctx):
    """롤링 자기상관(지속성) — 주어진 lag."""
    x = resolved["signal"]
    w = int(params.get("window", 20))
    lag = int(params.get("lag", 1))

    def ac(v):
        s = pd.Series(v)
        return s.autocorr(lag) if len(s) > lag else np.nan

    out = {col: x[col].rolling(w).apply(ac, raw=True) for col in x.columns}
    return pd.DataFrame(out, index=x.index)


def _ev_ts_halflife(resolved, params, ctx):
    """롤링 평균회귀 반감기 — AR(1) Δ=a+b·level_lag, phi=1+b, hl=-ln2/ln(phi)."""
    x = resolved["signal"]
    w = int(params.get("window", 60))

    def hl(v):
        v = np.asarray(v, dtype=float)
        if len(v) < 3 or np.any(np.isnan(v)):
            return np.nan
        lev = v[:-1]
        dlt = np.diff(v)
        lm = lev.mean()
        denom = ((lev - lm) ** 2).sum()
        if denom <= 0:
            return np.nan
        b = ((lev - lm) * (dlt - dlt.mean())).sum() / denom
        phi = 1 + b
        if phi <= 0 or phi >= 1:
            return np.nan          # 비정상·발산 — 반감기 정의 안 됨
        return -np.log(2) / np.log(phi)

    out = {col: x[col].rolling(w).apply(hl, raw=True) for col in x.columns}
    return pd.DataFrame(out, index=x.index)


# ── 축1: 달력 라벨링 (요일·월) — 입력 없이 날짜 인덱스에서 파생 ────────────────

def _ev_calendar(resolved, params, ctx):
    """날짜 인덱스에서 달력 라벨 패널 생성 (펼침 달력 분할용).

    unit: weekday(0=월)·month·monthweek(월중주차)·dom(월중일)·bday(월내 영업일서수)·
    turn_of_month(월말 tom_end + 월초 tom_start 영업일=1, 나머지=0).
    bday/turn_of_month는 거래일(인덱스) 기준 월내 순번을 쓴다 — 휴장일을 건너뛴 실효 영업일.
    """
    unit = params.get("unit", "weekday")
    idx = ctx.master_idx
    if unit == "month":
        vals = idx.month
    elif unit == "monthweek":
        vals = (idx.day - 1) // 7 + 1
    elif unit == "dom":
        vals = idx.day
    elif unit in ("bday", "turn_of_month"):
        grp = pd.Series(idx.to_period("M"), index=idx)
        from_start = grp.groupby(grp).cumcount().to_numpy() + 1            # 월내 1번째 영업일=1
        size = grp.groupby(grp).transform("size").to_numpy()
        from_end = size - from_start + 1                                   # 월말 마지막=1
        if unit == "bday":
            vals = from_start
        else:
            ts, te = int(params.get("tom_start", 3)), int(params.get("tom_end", 1))
            vals = ((from_start <= ts) | (from_end <= te)).astype(float)
    else:  # weekday (0=월)
        vals = idx.dayofweek
    s = pd.Series(np.asarray(vals, dtype=float), index=idx)
    return pd.DataFrame({sym: s for sym in ctx.symbols}, index=idx)


def _ev_bar_index(resolved, params, ctx):
    """경과 영업일 램프(0..N-1)를 전 종목에 동일 부여 — 입력 없는 잎(calendar와 동형).

    선형회귀의 시간축 x로 쓴다: ts_regression(가격, bar_index)의 beta=일당 추세 기울기,
    r2=추세 직선성. 롤링 윈도 내에선 등간격 정수 램프라 beta·r2가 오프셋 불변(정상)."""
    idx = ctx.master_idx
    s = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    return pd.DataFrame({sym: s for sym in ctx.symbols}, index=idx)


# ── 축4: per-symbol 정적 분류 라벨 + 멤버십 조건 (섹터 필터·슬리브 구분) ────────

def _ev_attribute(resolved, params, ctx):
    """종목별 정적 분류 라벨 패널 — 섹터/업종(static.classification)·시장(ticker_db 거래소).
    날짜축 불변(브로드캐스트).

    그룹 연산이 내부로만 쓰던 get_symbol_group을 라벨로 노출 → is_in으로 필터(섹터 제외·
    시장 분리)·select로 슬리브 구분·group_label로 그룹 노출 캡에 쓸 수 있다.
    attr="Market"은 상장 거래소(KOSPI·KOSDAQ·NASDAQ·NYSE) — 개별 주식 전용(ETF·지수는 폴백).
    미수급은 '기타'/'Other' 폴백.
    """
    attr = params.get("attr", "Industry")
    vals = {sym: get_symbol_group(sym, attr) for sym in ctx.symbols}
    return pd.DataFrame({sym: pd.Series(v, index=ctx.master_idx) for sym, v in vals.items()},
                        index=ctx.master_idx)


def _ev_is_in(resolved, params, ctx):
    """라벨 멤버십 → condition. 라벨이 values 집합에 속하면 참(negate면 반전).

    '금융·지주 제외'(negate)·'특정 버킷만'(bucket 라벨)·슬리브 구분 등 범주 선택의 원자.

    match="contains"는 정확매칭 대신 부분문자열 매칭 — 섹터/업종 분류가 KSIC 자유서술
    ("반도체 제조업")이라 사용자어("반도체")로는 정확매칭이 0건이 되는 문제 때문에 필요하다.
    기본은 "exact"(버킷·국면 라벨 등 정확 멤버십이 옳은 용도의 회귀 불변).
    """
    x = resolved["signal"]
    values = list(params.get("values", []))
    if params.get("match") == "contains":
        if values:
            pat = "|".join(re.escape(str(v)) for v in values)
            contains = lambda s: s.astype(str).str.contains(pat, regex=True, na=False)
            mask = x.apply(contains) if isinstance(x, pd.DataFrame) else contains(x)
        else:
            mask = (pd.DataFrame(False, index=x.index, columns=x.columns)
                    if isinstance(x, pd.DataFrame) else pd.Series(False, index=x.index))
    else:
        mask = x.isin(values)
    if params.get("negate", False):
        mask = ~mask
    return as_bool_panel(mask)


# ── 등록 ──────────────────────────────────────────────────────────────────────

register(BlockDef("ts_corr", ValueType.SCORE, _ev_ts_corr,
                  slots={"a": ValueType.SCORE, "b": ValueType.SCORE},
                  param_defaults={"window": 20}, doc="롤링 상관(동행성)"))
register(BlockDef("ts_regression", ValueType.SCORE, _ev_ts_regression,
                  slots={"y": ValueType.SCORE, "x": ValueType.SCORE},
                  param_defaults={"window": 20, "output": "beta"},
                  doc="롤링 선형적합(베타/잔차)"))
register(BlockDef("orthogonalize", ValueType.SCORE, _ev_orthogonalize,
                  slots={"a": ValueType.SCORE, "b": ValueType.SCORE},
                  requires_panel=True, doc="횡단 직교화(잔차)"))
register(BlockDef("group_rank", ValueType.SCORE, _ev_group_rank,
                  slots={"signal": ValueType.SCORE},
                  param_defaults={"group_type": "Industry"}, requires_panel=True,
                  doc="그룹 내 순위"))
register(BlockDef("group_aggregate", ValueType.SCORE, _ev_group_aggregate,
                  slots={"signal": ValueType.SCORE},
                  param_defaults={"group_type": "Industry", "stat": "mean"},
                  requires_panel=True, doc="그룹별 평균·합"))
register(BlockDef("cs_dispersion", ValueType.SCORE, _ev_cs_dispersion,
                  slots={"signal": ValueType.SCORE}, requires_panel=True,
                  doc="횡단 산포(표준편차)"))
register(BlockDef("bucket", ValueType.LABEL, _ev_bucket,
                  slots={"signal": ValueType.SCORE},
                  doc="경계값으로 구간 라벨 부여"))
register(BlockDef("ts_autocorr", ValueType.SCORE, _ev_ts_autocorr,
                  slots={"signal": ValueType.SCORE},
                  param_defaults={"window": 20, "lag": 1}, doc="롤링 자기상관(지속성)"))
register(BlockDef("ts_halflife", ValueType.SCORE, _ev_ts_halflife,
                  slots={"signal": ValueType.SCORE},
                  param_defaults={"window": 60}, doc="평균회귀 반감기"))
register(BlockDef("calendar", ValueType.LABEL, _ev_calendar,
                  param_defaults={"unit": "weekday"},
                  doc="달력 라벨(요일·월·월중주차·turn_of_month) — 국면 분할(study.label)뿐 아니라 "
                      "is_in과 결합해 *신호 게이트*로도 사용(예: Sell in May = "
                      "is_in(calendar(month), [11,12,1,2,3,4])·월말효과 = calendar(turn_of_month)==1)"))
register(BlockDef("bar_index", ValueType.SCORE, _ev_bar_index,
                  doc="경과 영업일 램프(추세 회귀의 시간축 x)"))
register(BlockDef("attribute", ValueType.LABEL, _ev_attribute,
                  param_defaults={"attr": "Industry"},
                  doc="종목 정적 분류 라벨(섹터·업종)"))
register(BlockDef("is_in", ValueType.CONDITION, _ev_is_in,
                  slots={"signal": ValueType.LABEL},
                  param_defaults={"negate": False, "match": "exact"},
                  doc="라벨 멤버십 조건(집합 포함/제외, match=exact|contains)"))
