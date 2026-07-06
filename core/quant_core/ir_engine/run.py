"""StrategyIR 디스패치 + 펼침/기간분할/이벤트스터디 레이어.

명세 §7·§8. run_strategy_ir은 §5.4 루트 경계 계약을 강제한 뒤 **통합 실행 엔진**
(engine.run_unified, §7.6)에 위임한다 — 이벤트·스케줄을 단일 포지션 기반 일별 루프로
처리(진입×청산·롱숏·refill 임의 조합). 이 모듈은 그 위의 분석 레이어를 담는다:
  - run_query: 최상위 질문 디스패치(query 동사 + study 펼침)
  - run_sweep: 펼침(label/parameter/entity 축) → resultset
  - run_period_split: 시간순 폴드 OOS 일관성(study.axis=time_fold)
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
from .comparison import compare_by_partition
from .spec import PrescribeSpec, StrategyIR, Study
from .summarize import result_shape
from .sweep import (
    daily_returns, partition_by_label, summarize_returns,
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
    u, pos, st = strategy.universe, strategy.position, strategy.study
    if (u.screener or {}).get("condition") is not None:
        ft = _out_type(Node.model_validate(u.screener["condition"]))
        if ft != "condition":
            return f"스크리너 조건은 condition이어야 합니다 (현재: {ft or '알 수 없는 블록'})."
    if pos.exit.condition is not None and _out_type(pos.exit.condition) != "condition":
        return f"매도 조건은 condition이어야 합니다 (현재: {_out_type(pos.exit.condition) or '알 수 없는 블록'})."
    if pos.overlays.group_label is not None and _out_type(pos.overlays.group_label) != "label":
        return f"그룹 노출 라벨은 label이어야 합니다 (현재: {_out_type(pos.overlays.group_label) or '알 수 없는 블록'})."
    if st.label is not None and _out_type(st.label) != "label":
        return f"펼침 분할 라벨은 label이어야 합니다 (현재: {_out_type(st.label) or '알 수 없는 블록'})."
    if st.event is not None and _out_type(st.event) != "condition":
        return f"펼침 이벤트는 condition이어야 합니다 (현재: {_out_type(st.event) or '알 수 없는 블록'})."
    return None


# ── 디스패치 ──────────────────────────────────────────────────────────────────

def _apply_futures_roll(strategy: StrategyIR, dataset: dict) -> dict:
    """백테스트 선물 롤/조정 적용(E2) — SimSpec.roll_method/series_adjust가 지정되면 선물
    심볼의 연속물을 만기물 패널에서 재-stitch해 dataset에서 교체한다.

    롤을 데이터에 굽지 않고 백테스트 파라미터로 둔다: 미지정이면 기본 서빙뷰(카탈로그
    at_expiry+none, 데이터 계층이 이미 생성)를 그대로 쓰고, 지정 시에만 패널에서 재구성.
    패널 미수집이면 적용 불가 → 기본 서빙뷰 유지(honest boundary, 조용한 오적용 안 함).
    param_grid(simulation.roll_method) 스윕도 호출마다 재-stitch되어 자연 지원.
    """
    sim = strategy.simulation
    rm, sa = sim.roll_method, sim.series_adjust
    if rm is None and sa is None:
        return dataset
    from ..data.futures_roll import build_continuous
    from ..data_fetcher import load_futures_panel
    from ..exec_defaults import instrument_spec, is_futures
    out = None
    for sym in list(dataset):
        if not is_futures(sym):
            continue
        panel = load_futures_panel(sym)
        if panel is None or panel.empty:
            continue                                  # 패널 미수집 — 기본 서빙뷰 유지
        cont = build_continuous(
            panel, rm or instrument_spec(sym).default_roll or "at_expiry", sa or "none")
        if not cont.empty:
            if out is None:
                out = dict(dataset)
            out[sym] = cont
    return out if out is not None else dataset


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
    dataset = _apply_futures_roll(strategy, dataset)    # E2 — 선물 롤/조정(지정 시 패널서 재-stitch)
    # 통합 실행 엔진(§7.6)으로 위임 — 이벤트·스케줄을 단일 포지션 기반 일별 루프로 처리.
    # 이벤트 경로는 기존 run_backtest_ir/run_portfolio_ir와 동치(패리티 고정), 스케줄은
    # 정수주 회계로 전환(롯·현금드래그 반영). 진입×청산·롱숏·refill 임의 조합 가능.
    from .engine import run_unified
    return run_unified(strategy, dataset)


def run_query(strategy: StrategyIR, dataset: dict) -> dict:
    """최상위 질문 디스패치 — 동사(query) + 펼침(study)으로 경로 선택.

    성공 결과엔 canonical 형상 태그 ``result["shape"]``를 스탬프한다(P3 seam #1 정비).
    summarize(모델 텍스트)·웹 ChatResultView(차트)가 각자 순서의존으로 재추론하던 형상을
    **단일 키로 수렴** — 새 형상 추가 시 두 투영의 판별 체인을 동기화할 필요를 없앤다(드리프트
    차단). 미스탬프 결과(inspect 우회·레거시)는 소비측이 result_shape로 폴백(행동보존).
    (excel_export는 sweep 변종 period_split/condition/parameter를 별 시트로 더 잘게 나눠야
    해서 axis 기반 자체 디스패치를 유지한다 — 형상 태그보다 세분.)
    """
    err = _root_type_error(strategy)
    if err is not None:
        return _empty(err)
    res = _dispatch_query(strategy, dataset)
    if isinstance(res, dict) and res.get("success", True) and "shape" not in res:
        res["shape"] = result_shape(res)
    # 결과 품질 계약 — 모든 엔진 결과가 자기 품질(status/diagnostics/verdict)을 서술한다
    # (chat-reliability-redesign §3 · 뿌리 R1). 빈/퇴화/불가를 '성공'으로 흘려보내지 않도록.
    # 계약은 **부가 메타데이터·안전장치** — 분류가 실패해도 실제 결과 전달을 절대 깨지 않는다
    # (예외 시 status 없이 통과 → 소비자는 status 부재를 ok로 간주, B4 회귀 차단).
    if isinstance(res, dict) and res.get("success", True) and "status" not in res:
        from .result_status import classify_status
        try:
            res.update(classify_status(res))
        except Exception:   # noqa: BLE001 — 품질 주석이 결과를 깨면 안 됨(안전장치 자체의 fail-safe)
            pass
    return res


def _dispatch_query(strategy: StrategyIR, dataset: dict) -> dict:
    """query 동사 + study 펼침 → 실행 함수 라우팅(형상 스탬프 직전 단계)."""
    q = strategy.query
    if q == "select":
        return run_select(strategy, dataset)
    if q == "prescribe":
        return run_prescribe(strategy, dataset)
    if q == "breadth":
        return run_breadth(strategy, dataset)
    if q == "rotation":
        return run_rotation(strategy, dataset)
    if q == "describe":
        u = strategy.universe
        if u.kind == "single":
            return run_describe_report(strategy, dataset)
        if u.kind == "portfolio":
            return run_portfolio_diagnosis(strategy, dataset)
        return _run_signal_study(strategy, dataset)
    if q == "relate":
        st = strategy.study
        if st.event is not None:
            return _run_event_study(strategy, dataset)
        if st.relation_kind == "regression":
            return _run_regression_study(strategy, dataset)
        if st.relation_kind == "correlation":
            return _run_correlation_study(strategy, dataset)
        return _run_ic_study(strategy, dataset)
    st = strategy.study
    if st.axis == "time_fold":
        return run_period_split(strategy, dataset)
    if st.axis == "none":
        return run_strategy_ir(strategy, dataset)
    if st.reduction == "extremize":
        return run_extremize(strategy, dataset)
    return run_sweep(strategy, dataset)


# ── 유니버스 (펼침·이벤트스터디 공용) ─────────────────────────────────────────

def _label_panel(lab, idx, cols: list) -> pd.DataFrame:
    """라벨 노드 평가결과를 (일×종목) 패널로 정규화 — 비교 분할용.

    종목별 라벨(attribute, 컬럼=종목)은 reindex 그대로; 일별 라벨(bucket/calendar,
    컬럼=지표 1개)은 첫 컬럼을 보유 종목 전체로 broadcast한다. 이로써 섹터(종목축)·
    국면(시간축)이 같은 (일×종목) 패널로 통일돼 compare_by_partition이 셀 단위로 분할.
    """
    if isinstance(lab, pd.DataFrame) and set(cols).issubset(set(lab.columns)):
        return lab.reindex(index=idx, columns=cols)
    series = (lab.iloc[:, 0] if isinstance(lab, pd.DataFrame) else lab).reindex(idx)
    return pd.DataFrame({c: series for c in cols}, index=idx)


def _universe_symbols(strategy: StrategyIR, dataset: dict) -> list[str]:
    u = strategy.universe
    if u.kind in ("single", "list", "portfolio"):
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


def relevant_symbols(strategy: StrategyIR, dataset: dict) -> set[str]:
    """데이터 품질 평가용 *전략 관련* 심볼 — 거래가능 유니버스(_universe_symbols·매크로 제외)
    ∪ 신호/매도조건/그룹라벨/펼침/스크리너가 명시 참조한 크로스에셋(S&P500 등). dataset 실재만.

    'all' 유니버스는 dataset에 매크로·지수·크립토 *참조* 시계열까지 싣지만(컬럼 프로젝션이
    전 심볼 로드), 그 staleness/gap을 전부 경고하면 후보가 아닌 비트코인·GDP까지 경고해 결과를
    오해시킨다(진단 뿌리 R4 — 후보 아닌 심볼을 결함으로 표면화). 품질 평가를 전략이 실제 의존하는
    심볼로 한정해 misleading 경고를 원천 제거한다. single/list는 이미 부분집합 로드라 무변화.
    """
    syms = set(_universe_symbols(strategy, dataset))
    nodes = [strategy.signal, strategy.position.exit.condition,
             strategy.position.overlays.group_label,
             strategy.study.label, strategy.study.event, strategy.study.target_node]
    sc = (strategy.universe.screener or {}).get("condition")
    if sc is not None:
        try:
            nodes.append(Node.model_validate(sc))
        except Exception:                  # noqa: BLE001 — 잘못된 스크리너는 그냥 참조 미수집(검증경로가 소유)
            pass
    for nd in nodes:
        if nd is not None:
            syms |= referenced_symbols(nd)
    return {s for s in syms if s in dataset}


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
    st = strategy.study
    if st.axis == "none":
        return run_strategy_ir(strategy, dataset)

    if st.axis == "parameter":
        grid = st.param_grid
        if not grid or any(not ax.values for ax in grid):
            return _empty("파라미터 축은 param_grid(경로·값)가 필요합니다.")
        import itertools
        base = strategy.model_dump()
        axes_meta = [{"path": ax.path, "values": list(ax.values)} for ax in grid]
        buckets = {}
        for combo in itertools.product(*[ax.values for ax in grid]):
            d = copy.deepcopy(base)
            d["study"] = {"axis": "none"}; d["query"] = "simulate"
            for ax, v in zip(grid, combo):
                _set_path(d, ax.path, v)
            key = " | ".join(f"{ax.path.split('.')[-1]}={v}" for ax, v in zip(grid, combo))
            res = run_strategy_ir(StrategyIR.model_validate(d), dataset)
            buckets[key] = (summarize_returns(daily_returns(res["equity"]))
                            if res.get("success") else {"error": res.get("error")})
        return {"success": True, "axis": "parameter", "axes": axes_meta, "buckets": buckets}

    if st.axis == "entity":
        if not st.assets:
            return _empty("자산 축은 assets가 필요합니다.")
        base = strategy.model_dump()
        buckets = {}
        for a in st.assets:
            d = copy.deepcopy(base)
            d["study"] = {"axis": "none"}; d["query"] = "simulate"
            d["universe"] = {"kind": "single", "symbols": [a],
                             "screener": None, "exclude_macro": True}
            res = run_strategy_ir(StrategyIR.model_validate(d), dataset)
            buckets[a] = (summarize_returns(daily_returns(res["equity"]))
                          if res.get("success") else {"error": res.get("error")})
        return {"success": True, "axis": "asset", "buckets": buckets}

    if st.axis == "label":
        if st.label is None:
            return _empty("조건 축은 라벨 블록이 필요합니다.")
        res = run_strategy_ir(strategy, dataset)
        if not res.get("success"):
            return res
        weight = res.get("weight")
        if weight is None or weight.empty or weight.shape[1] == 0:
            return _empty("비교할 보유 비중이 없습니다(체결 0).")
        # 기여 패널 = 전일 비중 × 종목 일별수익(Brinson). Σ종목 = 포트 마크투마켓 일별수익.
        cols = list(weight.columns)
        closes = pd.DataFrame({s: dataset[s]["Close"] for s in cols if s in dataset})
        sym_ret = closes.reindex(weight.index).pct_change().fillna(0.0)
        contribution = (weight.shift(1).fillna(0.0) * sym_ret).reindex(columns=cols).fillna(0.0)
        # 라벨 노드 → (일×종목) 패널(종목별=섹터, 일별=국면 broadcast). 옛 단일컬럼 붕괴 대체.
        lab = evaluate(st.label, EvalContext.from_dataset(
            _scoped(dataset, _universe_symbols(strategy, dataset), st.label)))
        label_panel = _label_panel(lab, weight.index, cols)
        cmp = compare_by_partition(contribution, weight, label_panel)
        # 버킷 요약 = 고유성과(그 그룹만 거래 시 수익) 시리즈. overall = 마크투마켓 합.
        buckets = {g: summarize_returns(b["daily_standalone"].dropna())
                   for g, b in cmp["buckets"].items()}
        parts = {g: b["daily_standalone"].dropna() for g, b in cmp["buckets"].items()}
        return {"success": True, "axis": "condition",
                "overall": summarize_returns(cmp["overall_daily"]),
                "buckets": buckets,
                "compare": compare_partition(parts),   # 그룹 간 유의성
                "metrics": res["metrics"], "equity": res["equity"]}

    return _empty(f"미지원 펼침 축: {st.axis}")


# ── 최적화 (extremize 환원 — 최적해 + 과최적화 OOS 가드) ────────────────────────

def run_extremize(strategy: StrategyIR, dataset: dict) -> dict:
    """펼침 축의 셀 중 목적함수 최대/최소 셀(최적해)을 찾고 OOS 일관성으로 과최적화를 가드.

    axis=parameter: param_grid 데카르트곱 / axis=entity: assets — 각 셀을 simulate 백테스트해
    summarize_returns로 perf 산출, objective.metric를 direction대로 argmax/argmin. in-sample
    argmax는 과최적화 위험이라 oos_guard=True면 최적 셀을 시간폴드(run_period_split)로 재검해
    OOS 일관성을 표면화한다(견고한 최적 vs 우연한 spike 구분). 새 평가기 없이 기존 머신 재사용.
    """
    from .spec import Objective
    st = strategy.study
    obj = st.objective or Objective()
    base = strategy.model_dump()

    combos: list = []   # [(label, kind, payload)]  kind∈{"param","entity"}
    if st.axis == "parameter":
        grid = st.param_grid
        if not grid or any(not ax.values for ax in grid):
            return _empty("파라미터 최적화는 param_grid(경로·값)가 필요합니다.")
        import itertools
        for combo in itertools.product(*[ax.values for ax in grid]):
            patch = {ax.path: v for ax, v in zip(grid, combo)}
            label = " | ".join(f"{ax.path.split('.')[-1]}={v}" for ax, v in zip(grid, combo))
            combos.append((label, "param", patch))
    elif st.axis == "entity":
        if not st.assets:
            return _empty("종목 최적화는 assets(종목 목록)가 필요합니다.")
        for a in st.assets:
            combos.append((a, "entity", a))
    else:
        return _empty(f"extremize는 parameter 또는 entity 축이 필요합니다 (현재: {st.axis}).")

    def _build(kind, payload) -> StrategyIR:
        d = copy.deepcopy(base)
        d["study"] = {"axis": "none"}; d["query"] = "simulate"
        if kind == "entity":
            d["universe"] = {"kind": "single", "symbols": [payload],
                             "screener": None, "exclude_macro": True}
        else:
            for path, v in payload.items():
                _set_path(d, path, v)
        return StrategyIR.model_validate(d)

    cells: list = []   # [(label, perf, kind, payload)]
    for label, kind, payload in combos:
        res = run_strategy_ir(_build(kind, payload), dataset)
        if res.get("success"):
            cells.append((label, summarize_returns(daily_returns(res["equity"])), kind, payload))
    if not cells:
        return _empty("최적화할 유효 결과가 없습니다(모든 셀 실패).")

    sign = 1.0 if obj.direction == "max" else -1.0

    def _score(perf):
        v = perf.get(obj.metric)
        return sign * float(v) if (v is not None and v == v) else float("-inf")  # NaN=최악

    cells.sort(key=lambda c: _score(c[1]), reverse=True)
    best_label, best_perf, best_kind, best_payload = cells[0]
    out = {
        "success": True, "axis": ("asset" if st.axis == "entity" else "parameter"),
        "reduction": "extremize", "objective": obj.model_dump(),
        "best": {"label": best_label, "metric_value": best_perf.get(obj.metric), "perf": best_perf},
        "ranked": [{"label": l, "metric_value": p.get(obj.metric)} for l, p, _, _ in cells],
    }
    if obj.oos_guard:
        guard = _build(best_kind, best_payload)
        guard.study = Study(axis="time_fold", reduction="consistency", folds=4)
        ps = run_period_split(guard, dataset)
        out["oos_guard"] = ({"buckets": ps.get("buckets"), "consistency": ps.get("consistency")}
                            if ps.get("success") else {"error": ps.get("error")})
    return out


# ── 기간분할 (비전 §3.5·§6) — 워크포워드/OOS/시계열 k-fold ────────────────────

def _inactive_buckets(buckets: dict) -> list:
    """수익·변동이 모두 0인 구간(무체결) 키 — '0%는 무수익이 아니라 무체결(데이터·신호 결손)'을
    정직히 표면화하기 위함(예: 코스피200선물 데이터 공백 연도가 침묵의 0%로 보이던 것)."""
    return [k for k, b in buckets.items()
            if isinstance(b, dict) and b.get("n")
            and not b.get("std") and not b.get("cum_return")]


def run_period_split(strategy: StrategyIR, dataset: dict) -> dict:
    """전략을 1회 실행한 뒤 수익을 시간순 폴드로 나눠 OOS 일관성을 본다.

    walk_forward·kfold: 4폴드(시간순), oos: 인샘플/아웃샘플 2분할. 무작위 분할이
    아니라 항상 시간 순서 유지(§6.2 — 미래로 학습해 과거 검증하는 오류 차단).
    """
    res = run_strategy_ir(strategy, dataset)
    if not res.get("success"):
        return res
    rets = daily_returns(res["equity"])
    st = strategy.study
    buckets: dict = {}
    if st.split_period:
        # 달력 주기(연/분기/월) 그룹 — 엔진이 실데이터 index로 분할. 컴파일러는 전체기간의 실제 연도
        # 범위를 몰라 folds를 추측할 수 없다("연도별"→folds=252 라이브 오남용의 근본해법). 키=주기 라벨.
        freq = {"year": "Y", "quarter": "Q", "month": "M"}[st.split_period]
        periods = rets.index.to_period(freq)
        for p in periods.unique():
            grp = rets[periods == p]
            if len(grp):
                buckets[str(p)] = summarize_returns(grp)
        consistency = walk_forward_consistency(rets, n_folds=max(len(buckets), 1))
    elif st.split_dates:
        # 명시 날짜 경계 — 지정 시점으로 분할(세그먼트 라벨=실제 기간 span). 학습/검증을
        # 등분이 아닌 사용자 지정 시점으로 가른다(예: ["2018-01-01"] → 2010-17 / 2018-25).
        cuts = sorted(pd.Timestamp(d) for d in st.split_dates)
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
        n = st.folds                       # oos=2(인/아웃), walk_forward·kfold=4(시간순 등분)
        is_oos = n == 2
        folds = [f for f in np.array_split(rets.to_numpy(), n) if len(f) > 0]
        for i, f in enumerate(folds):
            label = ("인샘플" if i == 0 else "아웃샘플") if is_oos else f"구간{i + 1}"
            buckets[label] = summarize_returns(pd.Series(f))
        consistency = walk_forward_consistency(rets, n_folds=n)
    # 무거래 구간 표면화 — 0%가 '무수익'이 아니라 '무체결(데이터·신호 결손)'일 수 있음을 정직히 알린다.
    warnings = list(res.get("warnings") or [])
    inactive = _inactive_buckets(buckets)
    if inactive:
        warnings.append({"code": "inactive_buckets",
                         "message": (f"무거래 구간: {', '.join(map(str, inactive))} — 해당 기간 가격·신호 "
                                     "데이터가 결손됐을 수 있습니다(0%는 무수익이 아니라 무체결).")})
    return {"success": True, "axis": "period_split", "buckets": buckets,
            "consistency": consistency, "metrics": res["metrics"], "warnings": warnings}


# ── 신호값 분석 (target=signal) — 수익률이 아닌 신호 자체의 분포 ───────────────

def _run_signal_study(strategy: StrategyIR, dataset: dict) -> dict:
    """임의 score 노드의 *값* 분포를 (선택)국면 라벨별로 — 신호 자체를 연구한다.

    "반감기가 변동성 레짐별로 다른가(MR3)", "BTC-주식 상관이 긴축기에 1로 수렴하나(CR2)"
    처럼 손익이 아니라 신호값의 분포·왜도가 답인 질문용. 분포는 비율 스케일(pct=False).
    """
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("분석 유니버스에 종목이 없습니다.")
    node = strategy.study.target_node
    if node is None:
        return _empty("분석 노드(target_node)가 없습니다.")
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, node, strategy.study.label))
    panel = evaluate(node, ctx)
    if not isinstance(panel, pd.DataFrame):
        return _empty("분석 노드가 패널(시계열)을 산출하지 않습니다.")
    # 분석 대상(target)은 숫자 신호여야 분포가 성립 — 범주형(섹터 등)이 target에 오면
    # numpy 깊은 곳의 ValueError 대신 여기서 행동 가능한 안내로 거부한다.
    if not all(pd.api.types.is_numeric_dtype(dt) for dt in panel.dtypes):
        return _empty("분석 노드(target_node)는 숫자 신호여야 합니다. 섹터·업종 등 분류별 "
                      "분포는 분류를 study.label에 두고 target_node에는 숫자 신호를 두세요.")
    pv = panel.to_numpy(dtype=float)
    overall = distribution(pd.Series(pv.ravel()), pct=False)
    by_regime = None
    if strategy.study.label is not None:
        lp = evaluate(strategy.study.label, ctx)
        if isinstance(lp, pd.DataFrame):
            # 라벨은 범주형 계약(숫자 레짐·문자열 섹터 무관) — capabilities가 광고하는
            # label=attribute('Sector')를 이행한다. float 강제(np.unique)가 섹터 라벨에서
            # ValueError('자동차')로 터지던 것을 dtype-무관 동등비교 그룹핑으로 대체
            # (simulate axis=label의 compare_by_partition과 동일 관용구). _label_panel이
            # 시간축(단일컬럼 broadcast)·종목축(attribute) 라벨을 (일×종목)으로 통일.
            lv = _label_panel(lp, panel.index, list(panel.columns)).to_numpy(dtype=object)
            valid = np.isfinite(pv) & pd.notna(lv)
            parts = {str(g): pd.Series(pv[valid & (lv == g)])
                     for g in pd.unique(lv[valid])}
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
        return _empty("IC 분석은 종목이 2개 이상이어야 합니다. "
                      "단일종목의 예측력은 이벤트 스터디(신호 발생 후 수익 분포)로 분석하세요.")
    node = strategy.study.target_node
    if node is None:
        return _empty("분석 노드(target_node)가 없습니다.")
    windows = strategy.study.windows or [21]
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, node, strategy.study.label))
    factor = evaluate(node, ctx)
    if not isinstance(factor, pd.DataFrame):
        return _empty("팩터 노드가 패널(시계열)을 산출하지 않습니다.")
    close = pd.DataFrame({s: dataset[s]["Close"] for s in syms
                          if s in dataset and "Close" in dataset[s].columns}).reindex(factor.index)
    fr = factor.rank(axis=1)                       # 횡단 순위(Spearman용)
    label_series = None
    if strategy.study.label is not None:
        lp = evaluate(strategy.study.label, ctx)
        if isinstance(lp, pd.DataFrame) and len(lp.columns):
            # IC는 날짜별 횡단 통계라 국면분할 라벨은 시간축(전 종목 동일)이어야 한다.
            # 옛 코드는 첫 컬럼을 전 시장 국면으로 *가정*해 섹터 같은 종목축 라벨이
            # 조용히 오답(첫 종목의 섹터=전 시장 국면)이 됐다 — 가정을 검증으로 바꿔 fail-loud.
            if bool((lp.nunique(axis=1, dropna=True) > 1).any()):
                return _empty("IC 국면분할 라벨은 시간축(날짜의 함수 — 예: bucket·calendar)이어야 "
                              "합니다. 섹터·업종별 비교는 섹터별 신호 분포(describe+label)나 "
                              "유니버스를 섹터로 나눈 IC 재실행으로 분석하세요.")
            label_series = lp[lp.columns[0]]        # 시간축 검증됨 — 어느 컬럼이든 동일
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


# ── 상관행렬 (RELATE — 분산투자·페어·헤지 후보) ────────────────────────────────

def _run_correlation_study(strategy: StrategyIR, dataset: dict) -> dict:
    """유니버스 종목 일별수익 간 상관행렬(피어슨) — 분산투자·페어·헤지 후보.

    forward·예측이 아닌 동시점 수익 공동움직임. 종목 2+ 필요. windows[0] 지정 시 최근
    그 거래일, 없으면 전체 가용기간. matrix는 symbols 순서의 대칭 행렬(자기상관 1.0).
    """
    syms = _universe_symbols(strategy, dataset)
    closes = pd.DataFrame({s: dataset[s]["Close"].astype(float) for s in syms
                           if s in dataset and "Close" in dataset[s].columns})
    syms = list(closes.columns)
    if len(syms) < 2:
        return _empty("상관분석은 가격 데이터가 2종목 이상 필요합니다.")
    rets = closes.pct_change()
    window = strategy.study.windows[0] if strategy.study.windows else None
    if window:
        rets = rets.tail(int(window) + 1)
    corr = rets.corr()                       # 피어슨 상관(결측 쌍은 NaN)
    matrix = [[None if pd.isna(corr.iat[i, j]) else round(float(corr.iat[i, j]), 4)
               for j in range(len(syms))] for i in range(len(syms))]
    iu = np.triu_indices(len(syms), k=1)     # 비대각(상삼각) 쌍
    pairs = [(syms[i], syms[j], float(corr.iat[i, j]))
             for i, j in zip(*iu) if pd.notna(corr.iat[i, j])]
    pairs.sort(key=lambda p: p[2])
    finite = [p[2] for p in pairs]
    return {"success": True, "axis": "relation", "relation": "correlation",
            "symbols": syms, "matrix": matrix,
            "n_obs": int(rets.dropna(how="any").shape[0]),
            "avg_corr": round(sum(finite) / len(finite), 4) if finite else None,
            "most_correlated": [pairs[-1][0], pairs[-1][1], round(pairs[-1][2], 4)] if pairs else None,
            "least_correlated": [pairs[0][0], pairs[0][1], round(pairs[0][2], 4)] if pairs else None}


# ── 다중팩터 횡단 회귀 (RELATE 심화 — Fama-MacBeth) ────────────────────────────

def _fama_macbeth(betas: np.ndarray):
    """per-date 계수 (T기간×K팩터) → Fama-MacBeth 집계 (평균·표준오차·t·95% CI).

    날짜별 횡단 회귀 계수의 시계열을 평균하고, 그 시계열 분산으로 t값을 낸다(횡단 상관에
    강건한 표준 방법). T=1이면 se=0. 정확관계(분산 0)면 t는 무한대(완전 유의)로 표기.
    """
    mean = betas.mean(axis=0)
    T = betas.shape[0]
    sd = betas.std(axis=0, ddof=1) if T > 1 else np.zeros(betas.shape[1])
    se = sd / np.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, mean / se,
                     np.where(mean != 0, np.inf * np.sign(mean), 0.0))
    return mean, se, t, mean - 1.96 * se, mean + 1.96 * se


def _run_regression_study(strategy: StrategyIR, dataset: dict) -> dict:
    """다중 설명변수의 forward수익 횡단 예측력 — Fama-MacBeth 계수·t값·신뢰구간.

    "밸류·모멘텀·퀄리티 중 무엇이 다음 달 수익을 횡단으로 설명하나(다중 통제 후)"에 답한다.
    날짜별 횡단 OLS(절편+팩터들, numpy lstsq=OLS) → per-date 계수 → Fama-MacBeth 집계.
    forward수익은 미래참조라 분석 전용(IC 스터디와 동일 규약). 단일팩터=IC, 다중·통제=이것.
    """
    from ..blocks.node import referenced_columns
    syms = _universe_symbols(strategy, dataset)
    if len(syms) < 2:
        return _empty("횡단 회귀는 종목이 2개 이상이어야 합니다. "
                      "단일종목의 예측력은 이벤트 스터디(신호 발생 후 수익 분포)로 분석하세요.")
    nodes = list(strategy.study.factors)
    if not nodes:
        return _empty("다중 회귀는 설명변수(factors)가 1개 이상 필요합니다.")
    windows = strategy.study.windows or [21]
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, *nodes))
    panels = [evaluate(n, ctx) for n in nodes]
    if any(not isinstance(p, pd.DataFrame) for p in panels):
        return _empty("설명변수가 패널(종목×날짜)을 산출하지 않습니다.")
    idx = panels[0].index
    close = pd.DataFrame({s: dataset[s]["Close"] for s in syms
                          if s in dataset and "Close" in dataset[s].columns}).reindex(idx)
    K = len(nodes)
    names = []
    for i, n in enumerate(nodes):
        cols = sorted(referenced_columns(n))
        names.append(cols[0] if cols else f"f{i}")

    by_window: dict = {}
    for w in windows:
        fwd = close.shift(-int(w)) / close - 1.0
        betas = []
        for d in idx:
            yv = fwd.loc[d].to_numpy(dtype=float)
            Xcols = [panels[k].loc[d].reindex(close.columns).to_numpy(dtype=float)
                     for k in range(K)]
            X = np.column_stack(Xcols)
            mask = np.isfinite(yv) & np.isfinite(X).all(axis=1)
            if int(mask.sum()) < K + 2:        # 자유도 확보
                continue
            Xd = np.column_stack([np.ones(int(mask.sum())), X[mask]])   # 절편 + 팩터
            try:
                coef, *_ = np.linalg.lstsq(Xd, yv[mask], rcond=None)
            except Exception:                  # noqa: BLE001 — 특이행렬 등
                continue
            betas.append(coef[1:])             # 절편 제외 팩터 계수
        if len(betas) < 2:
            by_window[str(w)] = {"n_periods": len(betas), "factors": None,
                                 "note": "회귀 가능한 기간이 부족합니다(종목·결측 확인)."}
            continue
        mean, se, t, lo, hi = _fama_macbeth(np.array(betas))
        by_window[str(w)] = {
            "n_periods": int(len(betas)),
            "factors": [{"name": names[k], "coef": float(mean[k]), "se": float(se[k]),
                         "t_stat": (float(t[k]) if np.isfinite(t[k]) else None),
                         "t_inf": bool(not np.isfinite(t[k])),
                         "ci_low": float(lo[k]), "ci_high": float(hi[k])} for k in range(K)],
        }
    return {"success": True, "axis": "relation", "relation": "regression",
            "windows": [str(w) for w in windows], "factor_names": names, "by_window": by_window}


# ── 선택 (SELECT 동사 — as-of 횡단 랭킹 스크리닝) ─────────────────────────────

_COMPARE_MAX = 30    # 비교표 행 상한(가독) — 초과 시 sort_by(또는 market_cap) 상위만


def _run_compare(strategy: StrategyIR, dataset: dict) -> dict:
    """SELECT compare 모드 — 지정 종목을 display 지표로 나란히 비교(랭킹 아님·score 불요).

    '피어 비교' 같은 표형 질문이 종목마다 describe로 폭발(N콜×N라운드)하던 것을 **1콜로 접는다**.
    as_of PIT 스냅샷에서 각 종목의 display 컬럼 최신값을 뽑아 한 표로. 정렬은 sort_by(기본
    market_cap→display[0]→유니버스 순)로 중립('승자' 함의 없음). forward 추정은 서버 엣지가 덧댄다(P1).
    """
    from ..expression_parser import get_symbol_group, symbol_name
    from .columns import select_columns

    sel = strategy.select
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("비교할 종목이 없습니다.")
    cutoff = pd.Timestamp(sel.as_of) if (sel.as_of and sel.as_of != "latest") else None

    def _last(df, col):
        if df is None or col not in df.columns:
            return None
        s = df[col]
        if cutoff is not None:
            s = s.loc[s.index <= cutoff]
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else None

    def _build(sym: str) -> dict:
        df = dataset.get(sym)
        return {"symbol": sym, "code": sym, "name": symbol_name(sym),
                "sector": get_symbol_group(sym, "Sector"), "score": None,
                "last_price": _last(df, "Close"),   # forward 추정(P1·forward_pe) 계산용 — 서버 엣지가 소비
                "metrics": {col: _last(df, col) for col in sel.display}}

    rows = [_build(s) for s in syms]
    sort_col = sel.sort_by
    if sort_col is None:
        if any(r["metrics"].get("market_cap") is not None for r in rows):
            sort_col = "market_cap"
        elif sel.display:
            sort_col = sel.display[0]
    if sort_col:
        rows.sort(key=lambda r: (r["metrics"].get(sort_col) is not None,
                                 r["metrics"].get(sort_col) or 0.0), reverse=True)
    note = None
    if len(rows) > _COMPARE_MAX:
        note = f"{len(rows)}종목 중 상위 {_COMPARE_MAX}개만 표시(정렬: {sort_col or '유니버스 순'})."
        rows = rows[:_COMPARE_MAX]
    # 비교는 랭킹이 아니므로 score 컬럼 제외(빈 열 방지) — identity + display 지표만.
    cols = [c for c in select_columns(list(sel.display)) if c.get("kind") != "score"]
    out = {"success": True, "query": "select", "mode": "compare",
           "as_of": (str(cutoff)[:10] if cutoff is not None else "latest"),
           "universe_size": len(syms), "eligible_size": len(rows),
           "results": rows, "columns": cols, "sort_by": sort_col,
           "scoring": {"recipe": (f"비교 — {sort_col} 정렬" if sort_col else "비교 — 유니버스 순"),
                       "mode": "compare"}}
    if note:
        out["note"] = note
    return out


def run_select(strategy: StrategyIR, dataset: dict) -> dict:
    """SELECT 동사 — as-of 스냅샷에서 score를 횡단 랭크해 상위 종목 선별(스크리닝).

    시계열 시뮬 없음. signal(score)을 평가해 as_of 시점 단면을 랭크한다. universe.screener는
    자격 마스크(PIT), select.display는 결과에 붙일 지표 컬럼. 미래행 미참조(PIT).
    """
    from .engine import _screener_mask
    from ..expression_parser import get_symbol_group, symbol_name
    from .columns import score_recipe, select_columns

    sel = strategy.select
    if sel is None:
        return _empty("select 질의는 select 설정이 필요합니다.")
    if sel.mode == "compare":
        return _run_compare(strategy, dataset)          # 지정 종목 나란히 비교(score 불요)
    if _out_type(strategy.signal) != "score":
        return _empty("select(스크리닝)은 랭킹용 score 신호가 필요합니다.")
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("선별 유니버스에 종목이 없습니다.")
    screener = strategy.universe.screener or {}
    filt = Node.model_validate(screener["condition"]) if screener.get("condition") else None
    ds = _scoped(dataset, syms, strategy.signal, filt)
    ctx = EvalContext.from_dataset(ds)
    score = evaluate(strategy.signal, ctx)
    if not isinstance(score, pd.DataFrame) or score.empty:
        return _empty("score 신호가 패널(종목×날짜)을 산출하지 않습니다.")
    cols = [c for c in syms if c in score.columns]
    if not cols:
        return _empty("score가 유니버스 종목을 포함하지 않습니다.")
    score = score[cols]

    # as_of 스냅샷 (PIT — 미래행 미참조)
    if sel.as_of == "latest":
        # 마스터 인덱스 꼬리는 24/7 시리즈(암호화폐 등) 때문에 주식 마지막 종가일보다
        # 미래로 뻗을 수 있고 그 행은 score 전부 NaN — 마지막 '유효' 단면을 집는다.
        valid = score.dropna(how="all")
        if valid.empty:
            return _empty("score가 비결측 값을 가진 날짜가 없습니다.")
        asof = valid.index[-1]
    else:
        prior = score.index[score.index <= pd.Timestamp(sel.as_of)]
        if len(prior) == 0:
            return _empty(f"as_of {sel.as_of} 이전 데이터가 없습니다.")
        asof = prior[-1]
    row = score.loc[asof]

    # 자격 마스크(screener) 적용 — 같은 as_of 단면
    if filt is not None:
        elig = _screener_mask(screener, ctx, cols)
        elig_row = (elig.loc[asof] if asof in elig.index
                    else elig.reindex([asof]).iloc[0]).reindex(cols).fillna(False).astype(bool)
        row = row.where(elig_row)
    eligible = row.dropna()
    eligible_size = int(eligible.shape[0])

    def _build(sym: str) -> dict:
        df = dataset.get(sym)
        metrics = {}
        for col in sel.display:
            if df is not None and col in df.columns:
                sub = df.loc[df.index <= asof, col].dropna()
                metrics[col] = float(sub.iloc[-1]) if len(sub) else None
        sc = eligible.get(sym)
        return {"symbol": sym, "code": sym, "name": symbol_name(sym),   # ③ 티커→이름+코드
                "score": (float(sc) if sc is not None and pd.notna(sc) else None),
                "sector": get_symbol_group(sym, "Sector"), "metrics": metrics}

    ordered = eligible.sort_values(ascending=not sel.descending)
    cols = select_columns(list(sel.display))                       # ③ 자기서술 컬럼 메타
    scoring = score_recipe(strategy.signal.model_dump(), sel.descending)   # ③ 점수 산식(투명)

    # group_by: 그룹(섹터 등)별 top_n — 배터리 3 + 반도체 3 (정렬 후 그룹별 앞에서 N개)
    if sel.group_by:
        from collections import OrderedDict
        buckets: "OrderedDict[str, list]" = OrderedDict()
        cap = int(sel.top_n) if sel.top_n is not None else None
        for sym in ordered.index:
            g = get_symbol_group(sym, sel.group_by) or "기타"
            b = buckets.setdefault(g, [])
            if cap is None or len(b) < cap:
                b.append(sym)
        groups, results = [], []
        for g, gsyms in buckets.items():
            gres = [_build(s) for s in gsyms]
            groups.append({"group": g, "results": gres})
            results.extend(gres)
        return {"success": True, "query": "select", "as_of": str(asof)[:10],
                "universe_size": len(syms), "eligible_size": eligible_size,
                "group_by": sel.group_by, "groups": groups, "results": results,
                "columns": cols, "scoring": scoring}

    ranked = ordered
    if sel.top_n is not None:
        ranked = ranked.head(int(sel.top_n))
    elif sel.top_pct is not None:
        k = max(1, int(round(eligible_size * float(sel.top_pct) / 100.0)))
        ranked = ranked.head(k)
    results = [_build(sym) for sym in ranked.index]
    return {"success": True, "query": "select", "as_of": str(asof)[:10],
            "universe_size": len(syms), "eligible_size": eligible_size,
            "results": results, "columns": cols, "scoring": scoring}


# ── DESCRIBE 대상 확장 (P2 — 단일종목 360 리포트 + 포트폴리오 진단) ───────────

def run_describe_report(strategy: StrategyIR, dataset: dict) -> dict:
    """단일종목 360 리포트 — 한 종목의 가격·수익·리스크·밸류·섹터 스냅샷(DESCRIBE+단일대상).

    시계열 시뮬·신호 평가 없음(signal은 분석동사 명목값). 보유 데이터(가격/펀더멘털/분류)에서
    결정적 요약을 조립. 미수집 펀더멘털은 None으로 정직 표기(가짜 채움 금지). 데이터 기반 답변
    패러다임의 단일대상 슬라이스 — 뉴스/추정치/이벤트 기반 facet(왜 올랐나·성장전망·실적후확률)은 P3.
    """
    from ..expression_parser import get_symbol_group
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("리포트 대상 종목 데이터가 없습니다.")
    sym = syms[0]
    df = dataset.get(sym)
    if df is None or "Close" not in df.columns or df["Close"].dropna().empty:
        return _empty(f"{sym} 가격 데이터가 없습니다.")
    close = df["Close"].astype(float).dropna()
    asof = close.index[-1]
    last = float(close.iloc[-1])

    def _ret(days):
        if len(close) <= days:
            return None
        prev = float(close.iloc[-1 - days])
        return (last / prev - 1.0) if prev > 0 else None
    returns = {"1m": _ret(21), "3m": _ret(63), "6m": _ret(126), "12m": _ret(252)}

    win = close.iloc[-252:]
    hi_52w, lo_52w = float(win.max()), float(win.min())
    pct_from_high = (last / hi_52w - 1.0) if hi_52w > 0 else None

    rets = close.pct_change().dropna()
    rwin = rets.iloc[-252:]
    vol_ann = (float(rwin.std()) * (TRADING_DAYS ** 0.5)) if len(rwin) > 1 else None
    dd = close / close.cummax() - 1.0
    max_dd = float(dd.min()) if len(dd) else None

    fundamentals = {}
    for col in ("pb_ratio", "trailing_pe", "ev_ebitda"):
        if col in df.columns:
            s = df.loc[df.index <= asof, col].dropna()
            fundamentals[col] = float(s.iloc[-1]) if len(s) else None
        else:
            fundamentals[col] = None

    # 컨센서스(애널) — KR 라이브(main #149). 미커버(KR 외)는 None(가짜 채움 금지). PIT: asof 이전만.
    consensus = {}
    for col in ("consensus_target", "target_upside", "consensus_opinion",
                "analyst_count", "target_revision_pct", "days_since_report"):
        if col in df.columns:
            s = df.loc[df.index <= asof, col].dropna()
            consensus[col] = float(s.iloc[-1]) if len(s) else None
        else:
            consensus[col] = None

    # 수급(기관·외국인 순매수, 원) — 최신일 + 최근 20거래일 누적.
    flow = {}
    for col in ("inst_net_buy", "foreign_net_buy"):
        if col in df.columns:
            s = df.loc[df.index <= asof, col].dropna()
            flow[col] = float(s.iloc[-1]) if len(s) else None
            flow[f"{col}_20d"] = float(s.iloc[-20:].sum()) if len(s) else None
        else:
            flow[col] = flow[f"{col}_20d"] = None

    return {
        "success": True, "query": "describe", "report": "single",
        "symbol": sym, "sector": get_symbol_group(sym, "Sector"),
        "as_of": str(asof)[:10], "data_points": int(len(close)),
        "price": {"last": last, "returns": returns, "high_52w": hi_52w,
                  "low_52w": lo_52w, "pct_from_52w_high": pct_from_high},
        "risk": {"vol_annualized": vol_ann, "max_drawdown": max_dd},
        "fundamentals": fundamentals,
        "consensus": consensus,
        "flow": flow,
    }


def run_portfolio_diagnosis(strategy: StrategyIR, dataset: dict) -> dict:
    """포트폴리오 진단 — 보유 종목의 집중도·섹터노출·가중밸류·리스크 스냅샷(DESCRIBE+포트폴리오 대상).

    universe.kind="portfolio", symbols=보유, universe.weights(없으면 동일가중). 시뮬 없음.
    집중도(HHI·유효종목수)·섹터노출(분류)·가중 밸류·포트 변동성/평균상관을 보유 데이터로 결정적 계산.
    미수집은 coverage로 정직 표기. 실제 계좌 포지션 배선은 별개(엔진은 명시 holdings 입력).
    """
    from ..expression_parser import get_symbol_group
    u = strategy.universe
    holdings = [s for s in u.symbols if s in dataset and dataset[s] is not None
                and not dataset[s].empty and "Close" in dataset[s].columns]
    if not holdings:
        return _empty("진단할 보유 종목 데이터가 없습니다.")
    raw = u.weights or {}
    w = {s: float(raw[s]) for s in holdings if s in raw and float(raw[s]) > 0}
    if not w:
        w = {s: 1.0 for s in holdings}
    tot = sum(w.values())
    weights = {s: w.get(s, 0.0) / tot for s in holdings}
    asof = max(dataset[s]["Close"].dropna().index[-1] for s in holdings)

    wv = np.array([weights[s] for s in holdings], dtype=float)
    hhi = float((wv ** 2).sum())
    ws = sorted(wv, reverse=True)
    concentration = {"hhi": hhi, "effective_n": (float(1.0 / hhi) if hhi > 0 else None),
                     "top_weight": float(ws[0]), "top3_weight": float(sum(ws[:3]))}

    sector_exposure: dict = {}
    holdings_out = []
    for s in holdings:
        sec = get_symbol_group(s, "Sector")
        sector_exposure[sec] = sector_exposure.get(sec, 0.0) + weights[s]
        holdings_out.append({"symbol": s, "weight": weights[s], "sector": sec})

    def _wavg(col):
        num = wsum = 0.0
        for s in holdings:
            df = dataset[s]
            if col in df.columns:
                v = df.loc[df.index <= asof, col].dropna()
                if len(v):
                    num += weights[s] * float(v.iloc[-1]); wsum += weights[s]
        return (num / wsum) if wsum > 0 else None
    valuation = {"weighted_pb": _wavg("pb_ratio"), "weighted_pe": _wavg("trailing_pe")}

    closes = pd.DataFrame({s: dataset[s]["Close"].astype(float) for s in holdings})
    rets = closes.pct_change().dropna(how="any")
    risk = {"portfolio_vol_annualized": None, "avg_pairwise_corr": None}
    if rets.shape[0] > 1:
        cov = rets[holdings].cov().to_numpy() * TRADING_DAYS
        pvar = float(wv @ cov @ wv)
        risk["portfolio_vol_annualized"] = float(pvar ** 0.5) if pvar >= 0 else None
        if len(holdings) >= 2:
            cc = rets[holdings].corr().to_numpy()
            iu = np.triu_indices(len(holdings), k=1)
            vals = cc[iu][np.isfinite(cc[iu])]
            risk["avg_pairwise_corr"] = float(vals.mean()) if vals.size else None

    return {
        "success": True, "query": "describe", "report": "portfolio",
        "as_of": str(asof)[:10], "n_holdings": len(holdings), "holdings": holdings_out,
        "concentration": concentration, "sector_exposure": sector_exposure,
        "valuation": valuation, "risk": risk,
        "coverage": {"with_price": len(holdings),
                     "with_fundamentals": sum(1 for s in holdings if "pb_ratio" in dataset[s].columns
                                              and dataset[s]["pb_ratio"].dropna().shape[0] > 0)},
    }


# ── 처방 (PRESCRIBE — 포트폴리오 비중 최적화·추천) ────────────────────────────

def run_prescribe(strategy: StrategyIR, dataset: dict) -> dict:
    """PRESCRIBE — 포트폴리오 비중 최적화(추천). 위험기반 3종 + 최대샤프 동시 산출.

    일별수익에서 공분산·기대수익 추정(연율화). 제약=롱온리·비중합 1·종목당 max_weight 상한.
    min_variance/max_sharpe/risk_parity는 scipy SLSQP, equal_weight=1/N. max_sharpe는
    기대수익=과거평균이라 추정 노이즈가 큼(경고 동반) — 위험기반이 더 안정적.
    """
    from scipy.optimize import minimize
    ps = strategy.prescribe or PrescribeSpec()
    syms = _universe_symbols(strategy, dataset)
    closes = pd.DataFrame({s: dataset[s]["Close"].astype(float) for s in syms
                           if s in dataset and "Close" in dataset[s].columns})
    syms = list(closes.columns)
    if len(syms) < 2:
        return _empty("포트폴리오 추천은 가격 데이터가 2종목 이상 필요합니다.")
    rets = closes.pct_change().dropna(how="any")
    if ps.window:
        rets = rets.tail(int(ps.window))
    if rets.shape[0] < 20:
        return _empty("비중 추정에 충분한 데이터가 없습니다(최소 20거래일).")
    n = len(syms)
    cov = rets.cov().to_numpy() * TRADING_DAYS
    mu = rets.mean().to_numpy() * TRADING_DAYS
    cap = max(float(ps.max_weight), 1.0 / n) if ps.max_weight else 1.0   # 합1 가능하도록 하한
    bounds = [(0.0, cap)] * n
    cons = ({"type": "eq", "fun": lambda w: float(w.sum() - 1.0)},)
    w0 = np.full(n, 1.0 / n)

    def _vol(w):
        return float(np.sqrt(max(float(w @ cov @ w), 0.0)))

    def _neg_sharpe(w):
        v = _vol(w)
        return -float(w @ mu) / v if v > 0 else 0.0

    def _rp(w):                          # 리스크 패리티 — 종목별 위험기여(rc) 균등화
        rc = w * (cov @ w)
        return float(np.sum((rc - float(w @ cov @ w) / n) ** 2))

    def _solve(obj):
        r = minimize(obj, w0, method="SLSQP", bounds=bounds, constraints=cons,
                     options={"maxiter": 800, "ftol": 1e-10})
        w = np.clip(np.asarray(r.x, dtype=float), 0.0, None)
        return (w / w.sum()) if w.sum() > 0 else w0

    solved = {"min_variance": _solve(lambda w: float(w @ cov @ w)),
              "max_sharpe": _solve(_neg_sharpe),
              "risk_parity": _solve(_rp),
              "equal_weight": w0}

    def _metrics(w):
        v, er = _vol(w), float(w @ mu)
        return {"weights": {syms[i]: round(float(w[i]), 4) for i in range(n)},
                "exp_return": round(er, 4), "exp_vol": round(v, 4),
                "sharpe": round(er / v, 3) if v > 0 else None}

    return {"success": True, "query": "prescribe", "symbols": syms,
            "objectives": {k: _metrics(w) for k, w in solved.items()},
            "recommended": "max_sharpe", "n_obs": int(rets.shape[0]),
            "max_weight": (cap if ps.max_weight else None),
            "warnings": [{"code": "mean_return_estimate",
                          "message": "최대샤프 비중은 기대수익=과거평균 추정이라 노이즈가 큽니다 — "
                                     "위험기반(최소분산·리스크패리티)이 더 안정적입니다."}]}


# ── 시장 breadth (PHASE 5c — "시장이 왜/어떤가") ──────────────────────────────

def run_breadth(strategy: StrategyIR, dataset: dict) -> dict:
    """시장 breadth — 유니버스 종목들의 등락·MA 상회 비율·섹터 분산('지수가 왜 빠지나'의 what).

    최신 바 기준: 1일 등락 부호로 상승/하락 종목 수, 1·5·20일 평균수익, 20/60일선 상회 비율,
    상위/하위 종목, (섹터 컬럼 있으면) 섹터별 평균 1일수익. 종목 2+ 필요. why(거시·뉴스)는
    엔진 밖 사이드카(P4)·해석이 보강 — 여기선 결정적 시장 폭 수치만.
    """
    syms = _universe_symbols(strategy, dataset)
    rows = []
    for s in syms:
        df = dataset.get(s)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        c = df["Close"].astype(float).dropna()
        if len(c) < 21:
            continue
        ma60 = float(c.iloc[-60:].mean()) if len(c) >= 60 else None
        sec = None
        if "sector" in df.columns:
            sv = df["sector"].dropna()
            sec = str(sv.iloc[-1]) if len(sv) else None
        rows.append({
            "symbol": s,
            "r1": float(c.iloc[-1] / c.iloc[-2] - 1.0),
            "r5": float(c.iloc[-1] / c.iloc[-6] - 1.0) if len(c) >= 6 else None,
            "r20": float(c.iloc[-1] / c.iloc[-21] - 1.0),
            "above_ma20": bool(c.iloc[-1] > c.iloc[-20:].mean()),
            "above_ma60": (bool(c.iloc[-1] > ma60) if ma60 is not None else None),
            "sector": sec})
    if len(rows) < 2:
        return _empty("시장 breadth는 가용 종목이 2개 이상이어야 합니다.")
    n = len(rows)
    n_up = sum(1 for x in rows if x["r1"] > 0)
    n_down = sum(1 for x in rows if x["r1"] < 0)

    def _avg(key: str):
        vals = [x[key] for x in rows if x[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    ma60_rows = [x for x in rows if x["above_ma60"] is not None]
    ranked = sorted(rows, key=lambda x: x["r1"], reverse=True)
    sectors: dict = {}
    for x in rows:
        if x["sector"]:
            sectors.setdefault(x["sector"], []).append(x["r1"])
    sector_avg = sorted(([k, round(sum(v) / len(v), 4), len(v)] for k, v in sectors.items()),
                        key=lambda t: t[1])
    return {"success": True, "query": "breadth", "n": n,
            "n_up": n_up, "n_down": n_down, "n_flat": n - n_up - n_down,
            "pct_up": round(n_up / n, 4),
            "avg_r1": _avg("r1"), "avg_r5": _avg("r5"), "avg_r20": _avg("r20"),
            "pct_above_ma20": round(sum(1 for x in rows if x["above_ma20"]) / n, 4),
            "pct_above_ma60": (round(sum(1 for x in ma60_rows if x["above_ma60"]) / len(ma60_rows), 4)
                               if ma60_rows else None),
            "top_gainers": [[x["symbol"], round(x["r1"], 4)] for x in ranked[:5]],
            "top_losers": [[x["symbol"], round(x["r1"], 4)] for x in ranked[-5:][::-1]],
            "sector_breakdown": sector_avg[:12]}


# ── 섹터 순환매 (범용 heatmap 형상의 첫 소비자) ────────────────────────────────

_ROTATION_MAX_MONTHS = 8      # 히트맵 열(가독) 상한 — 최근 N개월
_ROTATION_MAX_SECTORS = 15    # 히트맵 행(가독) 상한 — 종목수 상위 N개 섹터


def run_rotation(strategy: StrategyIR, dataset: dict) -> dict:
    """섹터 순환매 — 섹터(행)×최근 월(열) 평균 수익률 히트맵. '자금이 어느 업종으로 도는가'.

    각 종목의 월말 종가 수익률을 섹터별로 평균해 섹터×월 행렬을 만든다. 발산색 히트맵으로
    렌더(양수=빨강·음수=파랑)해, '순환매'의 본질인 **리더십의 시간적 이동**을 드러낸다.
    데이터=거래소 종가 + 업종분류(FDR KRX-DESC 등) — 결정적·뉴스 무관(신뢰 데이터 소스 명시).
    범용 `shape="heatmap"` 계약(rows·cols·matrix·축라벨)을 emit — 렌더러/소비자는 형상만 본다.
    """
    from collections import Counter
    from ..expression_parser import get_symbol_group
    syms = _universe_symbols(strategy, dataset)
    monthly: dict[str, "pd.Series"] = {}    # symbol → 월별 수익률(index=PeriodIndex[M])
    sector_of: dict[str, str] = {}
    for s in syms:
        df = dataset.get(s)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        c = df["Close"].astype(float).dropna()
        if len(c) < 25 or not isinstance(c.index, pd.DatetimeIndex):
            continue
        m_last = c.groupby(c.index.to_period("M")).last()      # 월말 종가(resample 대체·비-deprecated)
        r = m_last.pct_change().dropna()
        if r.empty:
            continue
        monthly[s] = r
        sector_of[s] = get_symbol_group(s, "Sector")           # 표준 테마(반도체·2차전지…)·KSIC 폴백
    if len(monthly) < 2:
        return _empty("섹터 순환매는 가용 종목이 2개 이상이어야 합니다.")
    all_months = sorted(set().union(*[set(r.index) for r in monthly.values()]))
    cols_idx = all_months[-_ROTATION_MAX_MONTHS:]
    if len(cols_idx) < 2:
        return _empty("순환매 분석에 충분한 월별 이력이 없습니다(최소 2개월).")
    # 섹터 집합 = 종목수(≥2) 상위 N개(가독) · 폴백 버킷(기타/Other)은 분류가 있으면 제외.
    counts = Counter(sector_of[s] for s in monthly)
    ranked = [sec for sec, c in counts.most_common() if c >= 2 and sec not in ("기타", "Other")]
    if not ranked:
        ranked = [sec for sec, _ in counts.most_common()]      # 분류 전무 시 폴백 버킷이라도
    sectors = ranked[:_ROTATION_MAX_SECTORS]
    sec_syms = {sec: [s for s in monthly if sector_of[s] == sec] for sec in sectors}
    matrix: list[list] = []
    for sec in sectors:
        row = []
        for mo in cols_idx:
            vals = [float(monthly[s].loc[mo]) for s in sec_syms[sec]
                    if mo in monthly[s].index and pd.notna(monthly[s].loc[mo])]
            row.append(round(sum(vals) / len(vals) * 100, 2) if vals else None)
        matrix.append(row)
    # 최근월 수익률 내림차순으로 섹터 정렬(리더가 위로 — 순환 가독).
    def _last(i):
        return matrix[i][-1] if matrix[i][-1] is not None else float("-inf")
    order = sorted(range(len(sectors)), key=_last, reverse=True)
    sectors = [sectors[i] for i in order]
    matrix = [matrix[i] for i in order]
    col_labels = [str(mo) for mo in cols_idx]                  # "2026-06"
    leaders = []
    for j, lab in enumerate(col_labels):
        cand = [(sectors[i], matrix[i][j]) for i in range(len(sectors)) if matrix[i][j] is not None]
        if cand:
            top = max(cand, key=lambda t: t[1])
            leaders.append([lab, top[0], round(top[1], 2)])
    return {"success": True, "query": "rotation", "shape": "heatmap",
            "rows": sectors, "cols": col_labels, "matrix": matrix,
            "row_axis": "섹터", "col_axis": "기간(월)",
            "value_label": "월 평균수익률", "value_unit": "%", "color_scale": "diverging",
            "n_symbols": len(monthly), "n_periods": len(col_labels), "leaders": leaders,
            "note": f"{len(monthly)}개 종목을 {len(sectors)}개 섹터로 집계한 월별 평균수익률."}


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


def _collect_event_rows(strategy: StrategyIR, dataset: dict) -> dict:
    """이벤트(신호 참) 발생을 윈도별 경로(end/mae/mfe)·종목·일자까지 수집 — 집계 전 원자료.

    _run_event_study(요약)와 event_records(엑셀 증빙)가 **공유**해 동일 이벤트 집합을 보장한다
    (엑셀 AVERAGE 재계산 = 엔진 mean 패리티의 근거). 반환:
      {ok:False, error} | {ok:True, windows, basis, has_label, collected, events,
       n_events, by_symbol, by_year}
    collected[w]=[(end,mae,mfe,regime)] (요약·by_regime용·기존 형상 보존),
    events=[{symbol, date, regime, ends:{w:end_pct}}] (증빙 — end는 %로 환산).
    """
    from collections import defaultdict

    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return {"ok": False, "error": "이벤트 분석 유니버스에 종목이 없습니다."}
    windows = strategy.study.windows or [5, 10, 20]
    basis = strategy.study.event_basis
    ev_node = strategy.study.event or strategy.signal
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, ev_node, strategy.study.label))
    ev_panel = evaluate(ev_node, ctx)
    if not isinstance(ev_panel, pd.DataFrame):
        return {"ok": False, "error": "이벤트 신호가 패널을 산출하지 않습니다."}
    ev_panel = ev_panel.astype(bool)

    label_panel = None
    if strategy.study.label is not None:
        lp = evaluate(strategy.study.label, ctx)
        if isinstance(lp, pd.DataFrame):
            # 시간축(단일컬럼)·종목축(attribute 섹터) 라벨을 (일×종목)으로 통일 —
            # 옛 코드는 원패널 그대로라 시간축 라벨이 종목 컬럼 부재 시 조용히 무시됐다.
            label_panel = _label_panel(lp, ev_panel.index, list(ev_panel.columns))

    market = _market_index(dataset, syms) if basis == "excess" else None
    sim = strategy.simulation
    start_ts = pd.Timestamp(sim.start) if sim.start is not None else None
    end_ts = pd.Timestamp(sim.end) if sim.end is not None else None
    collected: dict[int, list] = {w: [] for w in windows}   # w → [(end, mae, mfe, regime)]
    events: list[dict] = []                                 # 이벤트별 원자료(증빙)
    by_symbol: dict[str, int] = defaultdict(int)   # 구성 분해 — 어느 종목서 몇 이벤트(#4c 무차별 pooling 투명화)
    by_year: dict[int, int] = defaultdict(int)     # 구성 분해 — 어느 해서 몇 이벤트(시기 편중 투명화)
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
        # dtype 보존(object) — 라벨은 범주형 계약(문자열 섹터 포함). float 강제가 이 부류의
        # 크래시(ValueError '자동차') 원인이었다.
        reg = (label_panel[sym].reindex(idx).to_numpy(dtype=object)
               if (label_panel is not None and sym in label_panel.columns) else None)
        for p in np.flatnonzero(ev):
            d = idx[p]
            if (start_ts is not None and d < start_ts) or (end_ts is not None and d > end_ts):
                continue
            n_events += 1
            by_symbol[sym] += 1
            by_year[int(d.year)] += 1
            rv = reg[p] if reg is not None else None
            # 숫자 레짐은 기존대로 float(하류 str(k) 버킷 키 "1.0" 형식 보존), 문자열은 그대로.
            r = (None if rv is None or pd.isna(rv)
                 else float(rv) if isinstance(rv, (int, float, np.integer, np.floating))
                 else str(rv))
            ends: dict[int, float] = {}
            for w in windows:
                got = _event_paths(ca, oa, p, w, basis, mvals)
                if got is not None:
                    collected[w].append((got[0], got[1], got[2], r))
                    ends[w] = got[0] * 100.0      # 증빙 원자료는 % (엑셀 AVERAGE = 엔진 mean)
            events.append({"symbol": sym,
                           "date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
                           "regime": r, "ends": ends})
    return {"ok": True, "windows": windows, "basis": basis, "has_label": label_panel is not None,
            "collected": collected, "events": events, "n_events": int(n_events),
            "by_symbol": dict(by_symbol), "by_year": dict(by_year)}


def event_records(strategy: StrategyIR | dict, dataset: dict, *, cap: int = 50_000) -> dict:
    """이벤트 스터디의 집계 전 원자료(이벤트별 종목·일자·윈도별 forward수익%) — 엑셀 증빙용.

    _collect_event_rows 공유라 _run_event_study 요약과 **동일 이벤트 집합**(엑셀 AVERAGE = 엔진
    mean 패리티). cap 초과 시 앞 cap개만(truncated=True) — 그땐 재계산이 전체와 불일치할 수 있어
    호출측이 표기한다.
    """
    if isinstance(strategy, dict):
        strategy = StrategyIR.model_validate(strategy)
    c = _collect_event_rows(strategy, dataset)
    if not c["ok"]:
        return {"ok": False, "error": c["error"], "windows": [], "events": []}
    events = c["events"]
    return {"ok": True, "windows": list(c["windows"]), "basis": c["basis"],
            "n_events": c["n_events"], "events": events[:cap], "truncated": len(events) > cap}


def _run_event_study(strategy: StrategyIR, dataset: dict) -> dict:
    """이벤트(신호 참) 발생일 기준 forward 수익 + 경로지표(MAE·MFE) + 국면 유의성.

    "돌파 후 반등이 유의한가", "변동성 확대가 mean reversion 선행지표인가", "갭하락 후
    당일 종가까지 반등하나(intraday)", "거래량 동반 돌파의 forward 낙폭(MAE)" 같은
    질문에 단일 메커니즘으로 답한다. basis로 종가/시가내재/초과수익 기준을 고른다.
    수집은 _collect_event_rows에 위임(엑셀 증빙과 동일 이벤트 집합 공유).
    """
    from collections import defaultdict

    c = _collect_event_rows(strategy, dataset)
    if not c["ok"]:
        return _empty(c["error"])
    windows, basis, collected = c["windows"], c["basis"], c["collected"]

    overall: dict = {}
    by_regime: dict | None = {} if c["has_label"] else None
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

    composition = {
        "by_symbol": dict(sorted(c["by_symbol"].items(), key=lambda kv: -kv[1])),   # 많은 순(편중 즉시 보임)
        "by_year": {str(y): c["by_year"][y] for y in sorted(c["by_year"])},          # 연도 오름차순
    }
    return {"success": True, "axis": "time", "basis": basis,
            "windows": [str(w) for w in windows], "n_events": int(c["n_events"]),
            "overall": overall, "by_regime": by_regime, "composition": composition}
