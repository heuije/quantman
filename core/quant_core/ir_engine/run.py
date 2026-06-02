"""StrategyIR 디스패치 + 펼침/기간분할/이벤트스터디 레이어.

명세 §7·§8. run_strategy_ir은 §5.4 루트 경계 계약을 강제한 뒤 **통합 실행 엔진**
(engine.run_unified, §7.6)에 위임한다 — 이벤트·스케줄을 단일 포지션 기반 일별 루프로
처리(진입×청산·롱숏·refill 임의 조합). 이 모듈은 그 위의 분석 레이어를 담는다:
  - run_sweep: 펼침(조건/파라미터/자산/시간 축) → resultset
  - run_period_split: 시간순 폴드 OOS 일관성
  - _run_event_study: 이벤트(신호 참) 발생일 기준 forward 수익·경로지표
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from ..backtest import _empty
from ..blocks import EvalContext, evaluate, referenced_symbols
from ..blocks.catalog import get, has
from ..blocks.node import Node
from .compare import (
    compare_partition, distribution, one_sample_test, summarize_events,
    two_sample_test, walk_forward_consistency,
)
from .spec import StrategyIR
from .sweep import (
    daily_returns, partition_by_label, summarize_returns, sweep_condition,
)

TRADING_DAYS = 252


# ── 평가 컨텍스트 스코핑 ──────────────────────────────────────────────────────

def _scoped(dataset: dict, syms, *nodes) -> dict:
    """평가 대상 데이터를 유니버스(+명시 참조 외부심볼)로 좁힌 sub-dataset.

    전 종목(4천+) 합집합 달력에서 종목별 시계열을 평가하면, 서로 다른 시장·휴장일
    때문에 각 종목 값이 듬성해져 롤링 지표가 NaN이 된다(이벤트·룰 신호 0건 버그의
    근본 원인). 유니버스 종목의 공통 달력에서만 평가하도록 좁힌다. 명시적 "SYM.x"
    참조(VIX·S&P500 등)는 보존. 비면 안전하게 전체로 폴백.

    유니버스 순서(syms)를 보존하고 외부 참조는 정렬해 덧붙여 ds(=패널 컬럼) 순서를
    결정적으로 고정한다 — set 순회는 PYTHONHASHSEED에 따라 프로세스마다 순서가 달라져
    rank(method="first") 동률 분해·정수주 배분이 흔들렸다(engine._scoped와 동일 처리).
    """
    keep = list(dict.fromkeys(syms))
    extra: set = set()
    for nd in nodes:
        if nd is not None:
            extra |= referenced_symbols(nd)
    keep += sorted(extra.difference(keep))
    sub = {s: dataset[s] for s in keep
           if s in dataset and dataset[s] is not None and not dataset[s].empty}
    return sub or dataset


# ── 루트 경계 타입 계약 (명세 §5.4) ───────────────────────────────────────────

def _out_type(node) -> str | None:
    return get(node.op).out_type.value if (node is not None and has(node.op)) else None


def _root_type_error(strategy: StrategyIR) -> str | None:
    """signal 외 루트 경계의 out_type 계약을 엔진이 직접 강제 (§5.4 — validate_strategy 이중화).

    신호(signal)는 run_strategy_ir 디스패치가 직접 검사한다. 여기선 나머지 루트 경계
    — 스크리너 filter·매도조건·그룹라벨·펼침 라벨/이벤트 — 의 out_type을 확인해 미지원
    타입을 조용히 캐스팅(astype(bool)·임의 그룹화)하지 않고 명시 거부한다. 검증을 우회하거나
    엔진을 직접 호출해도 안전(label 코드가 조건으로 둔갑·연속 점수가 무의미 분할 생성 차단).
    """
    u, pos, sw = strategy.universe, strategy.position, strategy.sweep
    if (u.screener or {}).get("condition") is not None:
        ft = _out_type(Node.model_validate(u.screener["condition"]))
        if ft != "condition":
            return f"스크리너 조건은 condition이어야 합니다 (현재: {ft or '알 수 없는 블록'})."
    if pos.exit.condition is not None and _out_type(pos.exit.condition) != "condition":
        return f"매도 조건은 condition이어야 합니다 (현재: {_out_type(pos.exit.condition) or '알 수 없는 블록'})."
    if pos.overlays.group_label is not None and _out_type(pos.overlays.group_label) != "label":
        return f"그룹 노출 라벨은 label이어야 합니다 (현재: {_out_type(pos.overlays.group_label) or '알 수 없는 블록'})."
    if sw.label is not None and _out_type(sw.label) != "label":
        return f"펼침 분할 라벨은 label이어야 합니다 (현재: {_out_type(sw.label) or '알 수 없는 블록'})."
    if sw.event is not None and _out_type(sw.event) != "condition":
        return f"펼침 이벤트는 condition이어야 합니다 (현재: {_out_type(sw.event) or '알 수 없는 블록'})."
    return None


# ── 디스패치 ──────────────────────────────────────────────────────────────────

def run_strategy_ir(strategy: StrategyIR, dataset: dict[str, pd.DataFrame]) -> dict:
    """StrategyIR을 백테스트. entry.mode × signal 타입으로 경로 선택.

    신호 타입 계약을 엔진이 직접 강제한다 — 디스패치는 타입 enum에 '닫혀' 있어, 미지원 타입
    (label·scalar·미등록 op)을 조용히 점수/조건으로 둔갑시키지 않고 명시 거부한다. signal 외
    루트 경계(스크리너·청산·그룹·펼침)는 _root_type_error로 함께 강제 — validate_strategy의
    동일 계약(§5.4)을 엔진 불변식으로 이중화해 검증 우회·직접 호출에도 안전하다.
    """
    err = _root_type_error(strategy)
    if err is not None:
        return _empty(err)
    # 통합 실행 엔진(§7.6)으로 위임 — 이벤트·스케줄을 단일 포지션 기반 일별 루프로 처리.
    # 이벤트 경로는 기존 run_backtest_ir/run_portfolio_ir와 동치(패리티 고정), 스케줄은
    # 정수주 회계로 전환(롯·현금드래그 반영). 진입×청산·롱숏·refill 임의 조합 가능.
    from .engine import run_unified
    return run_unified(strategy, dataset)


# ── 유니버스 (펼침·이벤트스터디 공용) ─────────────────────────────────────────

def _universe_symbols(strategy: StrategyIR, dataset: dict) -> list[str]:
    u = strategy.universe
    if u.kind in ("single", "list"):
        return [s for s in u.symbols if s in dataset and not dataset[s].empty]
    # all — 매크로/자산 지수 제외, OHLC 보유 종목(전 유니버스 후보).
    # universe.screener 세부조건은 list/all 모두 _scoped·_screener_mask가 직교 적용한다.
    macro: set = set()
    if u.exclude_macro:
        try:
            from ..data_fetcher import MACRO_SYMBOLS
            macro = set(MACRO_SYMBOLS)
        except Exception:
            macro = set()
    out = []
    for s, df in dataset.items():
        # strat: 합성 자산은 시장 종목이 아님 — all 광역 스캔에서 제외
        # (명시 list 유니버스·데이터 참조로만 진입). 그 외 macro·빈 프레임 제외.
        if s in macro or s.startswith("strat:") or df is None or df.empty:
            continue
        if {"Open", "Close"}.issubset(df.columns):
            out.append(s)
    return out


# ── 펼침 (비전 §4) — 조건·파라미터·자산 축 ────────────────────────────────────

def _set_path(d: dict, path: str, value) -> None:
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value


def run_sweep(strategy: StrategyIR, dataset: dict) -> dict:
    """펼침 — 전략을 한 축으로 반복/분할해 resultset 산출.

    axis: none(단일 실행) · condition(사후 라벨 분할) · parameter(설정 그리드 재실행)
          · asset(종목별 재실행).
    """
    err = _root_type_error(strategy)
    if err is not None:
        return _empty(err)
    sw = strategy.sweep
    # 분석 대상 일반화 — target!=return이면 측정 대상이 수익률이 아니라 신호값/관계.
    # axis와 직교한 전용 경로로 분기(읽기층만, 신호 대수 무관).
    if sw.target == "signal":
        return _run_signal_study(strategy, dataset)
    if sw.target == "relation":
        return _run_ic_study(strategy, dataset)
    if sw.axis == "none":
        return run_strategy_ir(strategy, dataset)

    if sw.axis == "parameter":
        grid = sw.param_grid
        if not grid or any(not ax.values for ax in grid):
            return _empty("파라미터 축은 param_grid(경로·값)가 필요합니다.")
        import itertools
        base = strategy.model_dump()
        axes_meta = [{"path": ax.path, "values": list(ax.values)} for ax in grid]
        buckets = {}
        for combo in itertools.product(*[ax.values for ax in grid]):
            d = copy.deepcopy(base)
            d["sweep"] = {"axis": "none"}
            for ax, v in zip(grid, combo):
                _set_path(d, ax.path, v)
            key = " | ".join(f"{ax.path.split('.')[-1]}={v}" for ax, v in zip(grid, combo))
            res = run_strategy_ir(StrategyIR.model_validate(d), dataset)
            buckets[key] = (summarize_returns(daily_returns(res["equity"]))
                            if res.get("success") else {"error": res.get("error")})
        return {"success": True, "axis": "parameter", "axes": axes_meta, "buckets": buckets}

    if sw.axis == "asset":
        if not sw.assets:
            return _empty("자산 축은 assets가 필요합니다.")
        base = strategy.model_dump()
        buckets = {}
        for a in sw.assets:
            d = copy.deepcopy(base)
            d["sweep"] = {"axis": "none"}
            d["universe"] = {"kind": "single", "symbols": [a],
                             "screener": None, "exclude_macro": True}
            res = run_strategy_ir(StrategyIR.model_validate(d), dataset)
            buckets[a] = (summarize_returns(daily_returns(res["equity"]))
                          if res.get("success") else {"error": res.get("error")})
        return {"success": True, "axis": "asset", "buckets": buckets}

    if sw.axis == "condition":
        if sw.label is None:
            return _empty("조건 축은 라벨 블록이 필요합니다.")
        res = run_strategy_ir(strategy, dataset)
        if not res.get("success"):
            return res
        lab_syms = _universe_symbols(strategy, dataset)
        lab = evaluate(sw.label, EvalContext.from_dataset(_scoped(dataset, lab_syms, sw.label)))
        if isinstance(lab, pd.DataFrame):
            u = strategy.universe
            col = (u.symbols[0] if u.symbols and u.symbols[0] in lab.columns
                   else lab.columns[0])
            label_series = lab[col]
        else:
            label_series = lab
        rets = daily_returns(res["equity"])
        parts = partition_by_label(rets, label_series)
        return {"success": True, "axis": "condition",
                "overall": summarize_returns(rets),
                "buckets": sweep_condition(res["equity"], label_series),
                "compare": compare_partition(parts),   # A1 — 국면 간 유의성
                "metrics": res["metrics"], "equity": res["equity"]}

    if sw.axis == "time":
        return _run_event_study(strategy, dataset)

    return _empty(f"미지원 펼침 축: {sw.axis}")


# ── 기간분할 (비전 §3.5·§6) — 워크포워드/OOS/시계열 k-fold ────────────────────

def run_period_split(strategy: StrategyIR, dataset: dict) -> dict:
    """전략을 1회 실행한 뒤 수익을 시간순 폴드로 나눠 OOS 일관성을 본다.

    walk_forward·kfold: 4폴드(시간순), oos: 인샘플/아웃샘플 2분할. 무작위 분할이
    아니라 항상 시간 순서 유지(§6.2 — 미래로 학습해 과거 검증하는 오류 차단).
    """
    res = run_strategy_ir(strategy, dataset)
    if not res.get("success"):
        return res
    rets = daily_returns(res["equity"])
    sim = strategy.simulation
    buckets: dict = {}
    if sim.split_dates:
        # 명시 날짜 경계 — 지정 시점으로 분할(세그먼트 라벨=실제 기간 span). 학습/검증을
        # 등분이 아닌 사용자 지정 시점으로 가른다(예: ["2018-01-01"] → 2010-17 / 2018-25).
        cuts = sorted(pd.Timestamp(d) for d in sim.split_dates)
        seg = np.zeros(len(rets), dtype=int)
        for c in cuts:
            seg += (rets.index >= c).astype(int)
        n_seg = 0
        for sid in sorted(set(seg.tolist())):
            grp = rets[seg == sid]
            if not len(grp):
                continue
            buckets[f"{grp.index[0].date()}~{grp.index[-1].date()}"] = summarize_returns(grp)
            n_seg += 1
        consistency = walk_forward_consistency(rets, n_folds=max(n_seg, 1))
    else:
        mode = sim.period_split
        n = 2 if mode == "oos" else 4
        folds = [f for f in np.array_split(rets.to_numpy(), n) if len(f) > 0]
        for i, f in enumerate(folds):
            label = ("인샘플" if i == 0 else "아웃샘플") if mode == "oos" else f"구간{i + 1}"
            buckets[label] = summarize_returns(pd.Series(f))
        consistency = walk_forward_consistency(rets, n_folds=n)
    return {"success": True, "axis": "period_split", "buckets": buckets,
            "consistency": consistency, "metrics": res["metrics"]}


# ── 신호값 분석 (target=signal) — 수익률이 아닌 신호 자체의 분포 ───────────────

def _run_signal_study(strategy: StrategyIR, dataset: dict) -> dict:
    """임의 score 노드의 *값* 분포를 (선택)국면 라벨별로 — 신호 자체를 연구한다.

    "반감기가 변동성 레짐별로 다른가(MR3)", "BTC-주식 상관이 긴축기에 1로 수렴하나(CR2)"
    처럼 손익이 아니라 신호값의 분포·왜도가 답인 질문용. 분포는 비율 스케일(pct=False).
    """
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("분석 유니버스에 종목이 없습니다.")
    node = strategy.sweep.target_node
    if node is None:
        return _empty("분석 노드(target_node)가 없습니다.")
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, node, strategy.sweep.label))
    panel = evaluate(node, ctx)
    if not isinstance(panel, pd.DataFrame):
        return _empty("분석 노드가 패널(시계열)을 산출하지 않습니다.")
    pv = panel.to_numpy(dtype=float)
    overall = distribution(pd.Series(pv.ravel()), pct=False)
    by_regime = None
    if strategy.sweep.label is not None:
        lp = evaluate(strategy.sweep.label, ctx)
        if isinstance(lp, pd.DataFrame):
            lv = lp.reindex(index=panel.index, columns=panel.columns).to_numpy(dtype=float)
            mask = np.isfinite(pv) & np.isfinite(lv)
            parts = {str(r): pd.Series(pv[mask & (lv == r)])
                     for r in np.unique(lv[mask])}
            parts = {k: v for k, v in parts.items() if len(v)}
            by_regime = compare_partition(parts, pct=False) if parts else None
    return {"success": True, "axis": "signal",
            "overall": overall, "by_regime": by_regime}


# ── 횡단 IC 분석 (target=relation) — factor ↔ forward수익 예측력 ────────────────

def _run_ic_study(strategy: StrategyIR, dataset: dict) -> dict:
    """factor[t]와 forward수익[t→t+w]의 횡단 순위상관(IC) 시계열 — 예측력·팩터 타이밍.

    "이 팩터가 다음 달 수익을 횡단으로 설명하나(IC>0, F1)", "IC가 국면으로 예측되나
    (factor timing)"에 답한다. forward수익은 미래참조라 *분석 전용*(거래 신호 아님) —
    이벤트 스터디와 동일한 연구 목적 전향 측정. IC = 매 거래일 횡단 Spearman(순위 상관).
    """
    syms = _universe_symbols(strategy, dataset)
    if len(syms) < 2:
        return _empty("IC 분석은 종목이 2개 이상이어야 합니다.")
    node = strategy.sweep.target_node
    if node is None:
        return _empty("분석 노드(target_node)가 없습니다.")
    windows = strategy.sweep.windows or [21]
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, node, strategy.sweep.label))
    factor = evaluate(node, ctx)
    if not isinstance(factor, pd.DataFrame):
        return _empty("팩터 노드가 패널(시계열)을 산출하지 않습니다.")
    close = pd.DataFrame({s: dataset[s]["Close"] for s in syms
                          if s in dataset and "Close" in dataset[s].columns}).reindex(factor.index)
    fr = factor.rank(axis=1)                       # 횡단 순위(Spearman용)
    label_series = None
    if strategy.sweep.label is not None:
        lp = evaluate(strategy.sweep.label, ctx)
        if isinstance(lp, pd.DataFrame) and len(lp.columns):
            label_series = lp[lp.columns[0]]        # 국면은 시장 전역(브로드캐스트) 가정
    by_window: dict = {}
    for w in windows:
        fwd = close.shift(-int(w)) / close - 1.0    # forward수익(미래참조, 분석 전용)
        ic = fr.corrwith(fwd.rank(axis=1), axis=1).dropna()   # 날짜별 횡단 순위상관
        summ = one_sample_test(ic, pct=False)       # 평균 IC≠0 유의성(스케일 불변)
        summ["ir"] = (float(ic.mean() / ic.std())
                      if len(ic) > 1 and ic.std() and ic.std() > 0 else np.nan)
        block = {"overall": summ}
        if label_series is not None:
            parts = partition_by_label(ic, label_series.reindex(ic.index))
            block["by_regime"] = compare_partition(parts, pct=False) if parts else None
        by_window[str(w)] = block
    return {"success": True, "axis": "relation", "relation": "ic",
            "windows": [str(w) for w in windows], "by_window": by_window}


# ── 이벤트 스터디 (비전 §4 시간축) ────────────────────────────────────────────

def _market_index(dataset: dict, syms: list[str]) -> pd.Series | None:
    """초과수익(excess) basis용 시장 지수 — 유니버스 동일가중 정규화 가격 평균.

    종목별 (close/첫값) 정규화 → 횡단 평균. 어떤 날 d에서 M[d+k]/M[d]-1이 시장
    누적수익. 단일 종목이면 자기 자신이라 excess가 무의미 → None.
    """
    if len(syms) < 2:
        return None
    norm = []
    for s in syms:
        df = dataset.get(s)
        if df is None or "Close" not in df.columns or df.empty:
            continue
        c = df["Close"].astype(float)
        base = c.dropna()
        if base.empty or base.iloc[0] <= 0:
            continue
        norm.append(c / base.iloc[0])
    if len(norm) < 2:
        return None
    return pd.concat(norm, axis=1).mean(axis=1)


def _event_paths(ca, oa, p, w, basis, mvals):
    """이벤트 위치 p·윈도 w에서 (endpoint, mae, mfe) 경로지표. 불가 시 None.

    close   : 종가 anchor, k=1..w 누적.
    intraday: 시가 anchor, k=0..w 누적(k=0=당일 시가→종가, intraday reversal).
    excess  : 종가 anchor 누적 − 시장 누적.
    """
    q = p + w
    if q >= len(ca):
        return None
    if basis == "intraday":
        anchor = oa[p]
        if not np.isfinite(anchor) or anchor <= 0:
            return None
        seg = ca[p:q + 1] / anchor - 1.0
    else:
        anchor = ca[p]
        if not np.isfinite(anchor) or anchor <= 0:
            return None
        seg = ca[p + 1:q + 1] / anchor - 1.0
        if basis == "excess":
            if mvals is None or not np.isfinite(mvals[p]) or mvals[p] <= 0:
                return None
            seg = seg - (mvals[p + 1:q + 1] / mvals[p] - 1.0)
    seg = seg[np.isfinite(seg)]
    if seg.size == 0:
        return None
    return float(seg[-1]), float(seg.min()), float(seg.max())


def _run_event_study(strategy: StrategyIR, dataset: dict) -> dict:
    """이벤트(신호 참) 발생일 기준 forward 수익 + 경로지표(MAE·MFE) + 국면 유의성.

    "돌파 후 반등이 유의한가", "변동성 확대가 mean reversion 선행지표인가", "갭하락 후
    당일 종가까지 반등하나(intraday)", "거래량 동반 돌파의 forward 낙폭(MAE)" 같은
    질문에 단일 메커니즘으로 답한다. basis로 종가/시가내재/초과수익 기준을 고른다.
    """
    from collections import defaultdict

    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("이벤트 분석 유니버스에 종목이 없습니다.")
    windows = strategy.sweep.windows or [5, 10, 20]
    basis = strategy.sweep.event_basis
    ev_node = strategy.sweep.event or strategy.signal
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, ev_node, strategy.sweep.label))
    ev_panel = evaluate(ev_node, ctx)
    if not isinstance(ev_panel, pd.DataFrame):
        return _empty("이벤트 신호가 패널을 산출하지 않습니다.")
    ev_panel = ev_panel.astype(bool)

    label_panel = None
    if strategy.sweep.label is not None:
        lp = evaluate(strategy.sweep.label, ctx)
        label_panel = lp if isinstance(lp, pd.DataFrame) else None

    market = _market_index(dataset, syms) if basis == "excess" else None
    sim = strategy.simulation
    start_ts = pd.Timestamp(sim.start) if sim.start is not None else None
    end_ts = pd.Timestamp(sim.end) if sim.end is not None else None
    collected: dict[int, list] = {w: [] for w in windows}   # w → [(end, mae, mfe, regime)]
    n_events = 0
    for sym in syms:
        df = dataset.get(sym)
        if df is None or "Close" not in df.columns:
            continue
        idx = df.index
        if sym not in ev_panel.columns:
            continue
        ca = df["Close"].to_numpy(dtype=float)
        oa = (df["Open"].to_numpy(dtype=float) if "Open" in df.columns else ca)
        mvals = (market.reindex(idx).ffill().to_numpy(dtype=float)
                 if market is not None else None)
        ev = ev_panel[sym].reindex(idx, fill_value=False).to_numpy(dtype=bool)
        reg = (label_panel[sym].reindex(idx).to_numpy(dtype=float)
               if (label_panel is not None and sym in label_panel.columns) else None)
        for p in np.flatnonzero(ev):
            d = idx[p]
            if (start_ts is not None and d < start_ts) or (end_ts is not None and d > end_ts):
                continue
            n_events += 1
            r = (float(reg[p]) if reg is not None and np.isfinite(reg[p]) else None)
            for w in windows:
                got = _event_paths(ca, oa, p, w, basis, mvals)
                if got is not None:
                    collected[w].append((got[0], got[1], got[2], r))

    overall: dict = {}
    by_regime: dict | None = {} if label_panel is not None else None
    for w in windows:
        rows = collected[w]
        overall[str(w)] = summarize_events([x[0] for x in rows],
                                           [x[1] for x in rows], [x[2] for x in rows])
        if by_regime is not None:
            groups: dict = defaultdict(list)
            for end, mae, mfe, r in rows:
                if r is not None:
                    groups[r].append((end, mae, mfe))
            regimes = {str(k): summarize_events([t[0] for t in v], [t[1] for t in v],
                                                [t[2] for t in v]) for k, v in groups.items()}
            keys = sorted(groups.keys())
            pairwise = {}
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    pairwise[f"{keys[i]}_vs_{keys[j]}"] = two_sample_test(
                        pd.Series([t[0] for t in groups[keys[i]]]),
                        pd.Series([t[0] for t in groups[keys[j]]]))
            by_regime[str(w)] = {"by_regime": regimes, "pairwise": pairwise}

    return {"success": True, "axis": "time", "basis": basis,
            "windows": [str(w) for w in windows],
            "n_events": int(n_events), "overall": overall, "by_regime": by_regime}
