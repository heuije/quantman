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

def _resolve_operand(data: dict[str, pd.DataFrame], operand: Optional[dict]):
    """피연산자를 시계열(Series) 또는 상수(float/list)로 해석. 실패 시 None."""
    if not operand:
        return None
    kind = operand.get("kind", "indicator")

    if kind == "constant":
        return operand.get("value")

    sym = operand.get("symbol")
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
                    cond: dict) -> Optional[pd.Series]:
    """단일 조건의 boolean 마스크를 계산한다. 해석 불가 시 None."""
    cond = _normalize_condition(cond)
    left = _resolve_operand(data, cond.get("left"))
    if not isinstance(left, pd.Series):
        return None                       # 좌변은 반드시 시계열

    op = cond.get("op")
    right = _resolve_operand(data, cond.get("right"))
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


def build_signal_mask(
    data: dict[str, pd.DataFrame],
    conditions: list[dict],
    logic: str,
) -> pd.Series:
    """
    조건 집합을 만족하는 날짜의 boolean 마스크를 반환한다.
    조건이 없거나 종목·지표 데이터가 부족하면 빈 Series를 반환한다.
    분석 엔진(run_analysis)과 백테스트 엔진이 동일한 조건 정의를 공유하기 위한 헬퍼.
    """
    if not conditions:
        return pd.Series(dtype=bool)

    masks = []
    for cond in conditions:
        m = _condition_mask(data, cond)
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
