"""
조건부 확률 분석 엔진 + 조건 평가 프레임워크.

조건 구조 (신버전):
  condition = {
    "left":  Operand,                 # 좌변 (반드시 시계열)
    "op":    ">"|">="|"<"|"<="|"between"|"cross_up"|"cross_down",
    "right": Operand,                 # 우변 (지표/숫자/이력통계)
    "modifier": {"kind": "streak"|"within", "days": N} | None,
  }
  Operand = {"kind": "indicator", "symbol", "indicator"}
          | {"kind": "constant", "value": float | [min,max]}
          | {"kind": "history", "symbol", "indicator",
             "stat": "min"|"max"|"mean"|"percentile"|"lag",
             "window": N, "percentile": 0~100}

구버전 조건 {symbol, indicator, op, value}도 자동 인식·변환한다.
분석 엔진(run_analysis)과 백테스트 엔진이 build_signal_mask를 공유한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional

from .strategy import SELF_SYMBOL, is_self_ref

# scipy는 run_analysis에서만 쓰므로 지연 import한다.
# (로컬앱 패키징 시 scipy ~135MB를 번들에서 제외하기 위함)

OP_LABELS = {
    ">":  "초과",
    ">=": "이상",
    "<":  "미만",
    "<=": "이하",
    "between":    "범위",
    "cross_up":   "상향돌파",
    "cross_down": "하향돌파",
}

_STAT_LABELS = {
    "min": "최솟값", "max": "최댓값", "mean": "평균",
    "percentile": "백분위", "lag": "전값",
}


# ── 조건 정규화 (구버전 호환) ─────────────────────────────────────────────────

def _normalize_condition(cond: dict) -> dict:
    """구버전 {symbol, indicator, op, value} 조건을 신버전 구조로 변환한다."""
    if "left" in cond:
        return cond
    return {
        "left": {"kind": "indicator",
                 "symbol": cond.get("symbol"),
                 "indicator": cond.get("indicator")},
        "op": cond.get("op", ">"),
        "right": {"kind": "constant", "value": cond.get("value")},
        "modifier": cond.get("modifier"),
    }


# ── 피연산자 해석 ────────────────────────────────────────────────────────────

def _resolve_operand(data: dict[str, pd.DataFrame], operand: Optional[dict],
                      current_symbol: Optional[str] = None):
    """피연산자를 시계열(Series) 또는 상수(float/list)로 해석. 실패 시 None.

    Phase 41 — operand.symbol == SELF_SYMBOL이면 current_symbol로 치환.
    current_symbol이 None인데 placeholder를 만나면 평가 불가(None) 반환.
    """
    if not operand:
        return None
    kind = operand.get("kind", "indicator")

    if kind == "constant":
        return operand.get("value")

    sym = operand.get("symbol")
    if sym == SELF_SYMBOL:
        if not current_symbol:
            return None       # placeholder인데 종목 context 없음 — 평가 불가
        sym = current_symbol
    indic = operand.get("indicator")
    if sym not in data or data[sym].empty or indic not in data[sym].columns:
        return None
    series = data[sym][indic]

    if kind == "indicator":
        return series

    if kind == "history":
        stat = operand.get("stat")
        try:
            win = int(operand.get("window") or 0)
        except (TypeError, ValueError):
            return None
        if win <= 0:
            return None
        if stat == "min":
            return series.rolling(win).min()
        if stat == "max":
            return series.rolling(win).max()
        if stat == "mean":
            return series.rolling(win).mean()
        if stat == "percentile":
            q = float(operand.get("percentile") or 50) / 100.0
            return series.rolling(win).quantile(min(max(q, 0.0), 1.0))
        if stat == "lag":
            return series.shift(win)
    return None


# ── 연산자 적용 ──────────────────────────────────────────────────────────────

def _apply_op(left: pd.Series, op: str, right) -> Optional[pd.Series]:
    """좌변 시계열에 연산자·우변을 적용해 boolean Series를 반환한다."""
    if op == "between":
        if not isinstance(right, (list, tuple)) or len(right) < 2:
            return None
        lo, hi = right[0], right[1]
        return (left >= lo) & (left <= hi)

    if op in ("cross_up", "cross_down"):
        left_prev = left.shift(1)
        right_prev = right.shift(1) if isinstance(right, pd.Series) else right
        if op == "cross_up":
            return (left_prev <= right_prev) & (left > right)
        return (left_prev >= right_prev) & (left < right)

    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    return None


def _apply_modifier(mask: pd.Series, modifier: Optional[dict]) -> pd.Series:
    """수식어(지속성·최근성)를 boolean 마스크에 적용한다."""
    if not modifier:
        return mask
    kind = modifier.get("kind")
    try:
        days = int(modifier.get("days") or 1)
    except (TypeError, ValueError):
        return mask
    if days <= 1:
        return mask
    m = mask.astype(float)
    if kind == "streak":          # N일 연속 참
        return m.rolling(days).sum() >= days
    if kind == "within":          # 최근 N일 내 1회 이상 참
        return m.rolling(days).sum() >= 1
    return mask


def _condition_mask(data: dict[str, pd.DataFrame],
                    cond: dict,
                    current_symbol: Optional[str] = None) -> Optional[pd.Series]:
    """단일 조건의 boolean 마스크를 계산한다. 해석 불가 시 None.

    Phase 41 — current_symbol을 좌·우변 placeholder 치환에 사용.
    """
    cond = _normalize_condition(cond)
    left = _resolve_operand(data, cond.get("left"), current_symbol)
    if not isinstance(left, pd.Series):
        return None                       # 좌변은 반드시 시계열

    op = cond.get("op")
    right = _resolve_operand(data, cond.get("right"), current_symbol)
    if right is None:
        return None

    # 좌·우변이 모두 시계열이면 공통 인덱스로 정렬
    if isinstance(right, pd.Series):
        idx = left.index.intersection(right.index)
        if idx.empty:
            return None
        left_s, right_s = left.reindex(idx), right.reindex(idx)
    else:
        left_s, right_s = left, right

    mask = _apply_op(left_s, op, right_s)
    if mask is None:
        return None
    mask = _apply_modifier(mask, cond.get("modifier"))
    return mask.fillna(False).astype(bool)


def describe_condition(cond: dict, current_symbol: Optional[str] = None) -> str:
    """조건을 사람 친화적 한 줄로 표현. Phase 38.11 — 신호 미충족 사유 표시용.

    예: "RSI(14) 30 미만" / "MA5 cross_up MA20 (3일 연속)" / "Close > 어제 Close"

    Phase 41 — placeholder symbol(SELF_SYMBOL)을 만나면 current_symbol로 표시.
    current_symbol이 None이면 "[이 종목]"으로 라벨링.
    """
    cond = _normalize_condition(cond)
    op = cond.get("op", "?")
    op_label = OP_LABELS.get(op, op)

    def _operand_str(o: dict | None) -> str:
        if not o:
            return "?"
        kind = o.get("kind", "indicator")
        if kind == "constant":
            v = o.get("value")
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                return f"[{v[0]}, {v[1]}]"
            return str(v)
        sym = o.get("symbol") or "?"
        if sym == SELF_SYMBOL:
            sym = current_symbol or "[이 종목]"
        ind = o.get("indicator") or "?"
        base = f"{sym}.{ind}" if sym not in ("", None) else ind
        if kind == "history":
            stat = o.get("stat") or ""
            win = o.get("window")
            stat_label = _STAT_LABELS.get(stat, stat)
            if stat == "percentile":
                pct = o.get("percentile", 50)
                return f"{base}의 {win}일 {stat_label}({pct}%)"
            return f"{base}의 {win}일 {stat_label}"
        return base

    left_s = _operand_str(cond.get("left"))
    right_s = _operand_str(cond.get("right"))
    mod = cond.get("modifier") or {}
    mod_suffix = ""
    if isinstance(mod, dict) and mod.get("kind"):
        days = int(mod.get("days") or 1)
        if days > 1:
            kind = mod["kind"]
            mod_suffix = f" ({days}일 연속)" if kind == "streak" else f" (최근 {days}일 내)"
    return f"{left_s} {op_label} {right_s}{mod_suffix}"


def explain_buy_signal(
    data: dict[str, pd.DataFrame],
    conditions: list[dict],
    logic: str,
    current_symbol: Optional[str] = None,
) -> dict:
    """가장 최근 거래일에 대해 조건별 평가 결과를 사람 친화적으로 반환.

    Phase 38.11: trader/preview_engine이 "왜 매수 신호 False였나"를 사용자에게
    명시할 수 있도록 조건별 통과/미통과 + AND/OR 결합 결과를 함께 노출.

    Phase 41 — current_symbol을 [이 종목] placeholder 치환에 사용. 좌변에
    placeholder가 있는데 current_symbol이 None이면 그 조건은 평가 불가(passed=None).

    Returns:
        {
          "passed": bool,                # 최종 신호 평가
          "logic": "AND"|"OR",
          "details": [
            {"label": "RSI(14) 30 미만", "passed": True | False | None,
             "reason": "..." (None일 때 원인)}
          ],
          "summary": "RSI(14) 30 미만 ✓, MA5 > MA20 ✗",
        }
    """
    if not conditions:
        return {"passed": False, "logic": logic, "details": [],
                "summary": "조건 없음"}

    details = []
    final_mask: Optional[pd.Series] = None
    for cond in conditions:
        label = describe_condition(cond, current_symbol)
        m = _condition_mask(data, cond, current_symbol)
        if m is None or m.empty:
            details.append({"label": label, "passed": None,
                              "reason": "데이터 부족 또는 지표 누락"})
            continue
        last = bool(m.iloc[-1])
        details.append({"label": label, "passed": last, "reason": None})
        if final_mask is None:
            final_mask = m
        else:
            common = final_mask.index.intersection(m.index)
            if logic == "AND":
                final_mask = (final_mask.reindex(common) & m.reindex(common))
            else:
                final_mask = (final_mask.reindex(common) | m.reindex(common))

    passed = False
    if final_mask is not None and not final_mask.empty:
        passed = bool(final_mask.iloc[-1])

    # 한 줄 요약 — 통과 ✓, 미통과 ✗, 평가불가 ?
    def _glyph(p):
        return "✓" if p is True else ("✗" if p is False else "?")
    summary = ", ".join(f"{d['label']} {_glyph(d['passed'])}" for d in details)

    return {"passed": passed, "logic": logic, "details": details,
            "summary": summary}


def _partition_conditions(conditions: list[dict]) -> tuple[list[dict], list[dict]]:
    """조건을 (공통, 종목별)로 분리.

    Phase 41 — 좌·우변 중 어느 쪽이든 SELF_SYMBOL placeholder를 참조하면
    "종목별 조건"으로 분류. 둘 다 명시적 종목이면 "공통 조건".
    """
    common: list[dict] = []
    per: list[dict] = []
    for c in conditions:
        n = _normalize_condition(c)
        if is_self_ref(n.get("left")) or is_self_ref(n.get("right")):
            per.append(c)
        else:
            common.append(c)
    return common, per


def explain_buy_signal_per_symbol(
    data: dict[str, pd.DataFrame],
    conditions: list[dict],
    logic: str,
    target_symbols: list[str],
) -> dict:
    """공통 조건 1회 평가 + 종목별 조건 각 종목 평가 + AND/OR 결합.

    Phase 41 — 자동 선택 / 수동 다중 매수에서 종목별 조건 평가 결과를 종합.
    AND: 공통 통과 AND 종목별 통과인 종목만 매수 후보
    OR : 공통 통과 OR 종목별 통과인 종목 매수 후보 (공통이 통과하면 모든 종목 통과)

    Returns:
        {
          "common": explain_buy_signal 결과 | None (공통 조건 없으면 None),
          "per_symbol": {sym: {passed, details, summary}, ...},
          "passed_symbols": [최종 매수 후보],
          "logic": "AND" | "OR",
        }
    """
    common, per_cond = _partition_conditions(conditions)
    common_ex = explain_buy_signal(data, common, logic) if common else None

    per: dict[str, dict] = {}
    passed_symbols: list[str] = []
    for sym in target_symbols:
        sym_ex = (explain_buy_signal(data, per_cond, logic, sym)
                   if per_cond else None)
        if common_ex is None and sym_ex is None:
            passed, details, summary = False, [], "조건 없음"
        elif common_ex is None:
            passed = sym_ex["passed"]
            details = sym_ex["details"]
            summary = sym_ex["summary"]
        elif sym_ex is None:
            passed = common_ex["passed"]
            details = common_ex["details"]
            summary = common_ex["summary"]
        else:
            if logic == "AND":
                passed = common_ex["passed"] and sym_ex["passed"]
            else:
                passed = common_ex["passed"] or sym_ex["passed"]
            details = common_ex["details"] + sym_ex["details"]
            summary = ", ".join(
                s for s in (common_ex["summary"], sym_ex["summary"]) if s)
        per[sym] = {"passed": passed, "details": details, "summary": summary}
        if passed:
            passed_symbols.append(sym)

    return {"common": common_ex, "per_symbol": per,
             "passed_symbols": passed_symbols, "logic": logic}


def build_signal_mask(
    data: dict[str, pd.DataFrame],
    conditions: list[dict],
    logic: str,
    current_symbol: Optional[str] = None,
) -> pd.Series:
    """
    조건 집합을 만족하는 날짜의 boolean 마스크를 반환한다.
    조건이 없거나 종목·지표 데이터가 부족하면 빈 Series를 반환한다.
    분석 엔진(run_analysis)과 백테스트 엔진이 동일한 조건 정의를 공유하기 위한 헬퍼.

    Phase 41 — current_symbol을 [이 종목] placeholder 치환에 사용. None이면
    placeholder를 만났을 때 데이터 부족으로 처리되어 빈 Series가 돌아온다.
    """
    if not conditions:
        return pd.Series(dtype=bool)

    masks = []
    for cond in conditions:
        m = _condition_mask(data, cond, current_symbol)
        if m is None:
            return pd.Series(dtype=bool)
        masks.append(m)

    common_idx = masks[0].index
    for m in masks[1:]:
        common_idx = common_idx.intersection(m.index)
    if common_idx.empty:
        return pd.Series(dtype=bool)

    masks = [m.reindex(common_idx) for m in masks]
    combined = masks[0]
    for m in masks[1:]:
        combined = (combined & m) if logic == "AND" else (combined | m)
    return combined.fillna(False).astype(bool)


def run_analysis(
    data: dict[str, pd.DataFrame],       # symbol → 지표 포함 DataFrame
    conditions: list[dict],              # 조건 목록
    logic: str,                          # "AND" | "OR"
    target_symbol: str,
    target_indicator: str,
    forward_days: int = 1,
    lookback_years: Optional[int] = None,
) -> dict:
    """
    조건 발생일 기준 forward_days 후 target_indicator의 분포를 분석한다.

    Returns:
        dict with keys: n_samples, prob_positive, mean, median, q25, q75, std,
                        p_value, t_stat, distribution (Series), condition_dates (Index)
    """
    if not conditions:
        return _empty_result("조건을 1개 이상 설정하세요.")

    if target_symbol not in data or data[target_symbol].empty:
        return _empty_result(f"'{target_symbol}' 데이터 없음")

    target_df = data[target_symbol]
    if target_indicator not in target_df.columns:
        return _empty_result(f"'{target_indicator}' 지표가 {target_symbol}에 없음")

    # 조건 마스크 (백테스트와 동일한 엔진)
    mask = build_signal_mask(data, conditions, logic)
    if mask.empty:
        return _empty_result("조건의 종목·지표 설정을 확인하세요.")

    # 조건 충족일 → target 인덱스와 교집합
    condition_dates = mask.index[mask]
    if lookback_years:
        cutoff = target_df.index.max() - pd.DateOffset(years=lookback_years)
        condition_dates = condition_dates[condition_dates >= cutoff]
    condition_dates = condition_dates.intersection(target_df.index)
    if len(condition_dates) == 0:
        return _empty_result("조건을 만족하는 날짜가 없음")

    # forward_days 후 값
    future_values = target_df[target_indicator].shift(-forward_days)
    outcome = future_values.loc[condition_dates].dropna()
    if outcome.empty:
        return _empty_result(f"forward {forward_days}일 후 데이터 없음 (기간 끝 부분)")

    n = len(outcome)
    mean_val = outcome.mean()
    median_val = outcome.median()
    q25 = outcome.quantile(0.25)
    q75 = outcome.quantile(0.75)
    std_val = outcome.std()
    prob_positive = (outcome > 0).mean() * 100 if n > 0 else np.nan

    # t-test vs 0 (통계적 유의성) — scipy는 여기서만 필요하므로 지연 import
    if n >= 5 and std_val > 0:
        from scipy import stats
        t_stat, p_value = stats.ttest_1samp(outcome, 0)
    else:
        t_stat, p_value = np.nan, np.nan

    return {
        "success":         True,
        "n_samples":       n,
        "prob_positive":   prob_positive,
        "mean":            mean_val,
        "median":          median_val,
        "q25":             q25,
        "q75":             q75,
        "std":             std_val,
        "t_stat":          t_stat,
        "p_value":         p_value,
        "distribution":    outcome,
        "condition_dates": pd.DatetimeIndex(condition_dates),
        "error":           None,
    }


def run_temporal_stability(
    data: dict[str, pd.DataFrame],
    conditions: list[dict],
    logic: str,
    target_symbol: str,
    target_indicator: str,
    forward_days: int = 1,
    windows: list[int] = [3, 5, 10],  # 최근 N년
) -> pd.DataFrame:
    """
    각 lookback window별로 분석을 실행하여 시간 안정성을 확인.
    Returns DataFrame: index=window, columns=[n_samples, mean, prob_positive, p_value]
    """
    rows = []
    for yrs in windows:
        r = run_analysis(data, conditions, logic, target_symbol, target_indicator,
                         forward_days, lookback_years=yrs)
        rows.append({
            "기간": f"최근 {yrs}년",
            "샘플수": r.get("n_samples", 0),
            "평균": r.get("mean", np.nan),
            "양수확률(%)": r.get("prob_positive", np.nan),
            "p-value": r.get("p_value", np.nan),
        })

    # 전체 기간
    r_all = run_analysis(data, conditions, logic, target_symbol, target_indicator, forward_days)
    rows.insert(0, {
        "기간": "전체",
        "샘플수": r_all.get("n_samples", 0),
        "평균": r_all.get("mean", np.nan),
        "양수확률(%)": r_all.get("prob_positive", np.nan),
        "p-value": r_all.get("p_value", np.nan),
    })
    return pd.DataFrame(rows).set_index("기간")


def describe_operand(operand: Optional[dict], indicator_label_fn) -> str:
    """피연산자를 사람이 읽을 수 있는 문구로 변환한다."""
    if not operand:
        return "?"
    kind = operand.get("kind", "indicator")
    if kind == "constant":
        v = operand.get("value")
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return f"{v[0]}~{v[1]}"
        return str(v)
    sym = operand.get("symbol", "")
    lbl = indicator_label_fn(operand.get("indicator", ""))
    if kind == "history":
        stat = operand.get("stat")
        stat_lbl = _STAT_LABELS.get(stat, stat or "")
        if stat == "percentile":
            stat_lbl = f"백분위{operand.get('percentile')}"
        return f"{sym} {lbl}의 {operand.get('window')}일 {stat_lbl}"
    return f"{sym}의 {lbl}"


def build_query_description(
    conditions: list[dict],
    logic: str,
    target_symbol: str,
    target_indicator: str,
    forward_days: int,
    indicator_label_fn,
    op_label_fn=None,
) -> str:
    """사람이 읽을 수 있는 쿼리 설명문 생성."""
    parts = []
    for raw in conditions:
        c = _normalize_condition(raw)
        left = describe_operand(c.get("left"), indicator_label_fn)
        right = describe_operand(c.get("right"), indicator_label_fn)
        op_lbl = OP_LABELS.get(c.get("op"), c.get("op"))
        text = f"{left}이(가) {right} {op_lbl}"
        mod = c.get("modifier")
        if mod:
            d = mod.get("days")
            text += (f" [{d}일 연속]" if mod.get("kind") == "streak"
                     else f" [최근 {d}일 내]")
        parts.append(text)

    join_str = " AND " if logic == "AND" else " OR "
    tgt_lbl = indicator_label_fn(target_indicator)
    return (
        f"[{join_str.join(parts)}] 일 때, "
        f"{forward_days}거래일 후 {target_symbol}의 {tgt_lbl}"
    )


def _empty_result(error_msg: str) -> dict:
    return {
        "success":         False,
        "n_samples":       0,
        "prob_positive":   np.nan,
        "mean":            np.nan,
        "median":          np.nan,
        "q25":             np.nan,
        "q75":             np.nan,
        "std":             np.nan,
        "t_stat":          np.nan,
        "p_value":         np.nan,
        "distribution":    pd.Series(dtype=float),
        "condition_dates": pd.DatetimeIndex([]),
        "error":           error_msg,
    }
