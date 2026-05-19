"""
조건부 확률 분석 엔진 (Phase 2).

쿼리 구조:
  conditions: [{"symbol": str, "indicator": str, "op": str, "value": float}, ...]
  logic: "AND" | "OR"
  target_symbol: str
  target_indicator: str
  forward_days: int   # 조건 발생 후 N거래일 뒤의 target_indicator 값을 측정
  lookback_years: int | None

결과:
  n_samples, prob_positive, mean, median, q25, q75, std, p_value, distribution
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional

# scipy는 run_analysis에서만 쓰므로 지연 import한다.
# (로컬앱 패키징 시 scipy ~135MB를 번들에서 제외하기 위함)

OPS = {
    ">":  lambda x, v: x > v,
    ">=": lambda x, v: x >= v,
    "<":  lambda x, v: x < v,
    "<=": lambda x, v: x <= v,
    "between": lambda x, v: (x >= v[0]) & (x <= v[1]),
}

OP_LABELS = {
    ">": "초과",
    ">=": "이상",
    "<": "미만",
    "<=": "이하",
    "between": "범위",
}


def _apply_op(series: pd.Series, op: str, value) -> pd.Series:
    fn = OPS.get(op)
    if fn is None:
        raise ValueError(f"지원하지 않는 연산자: {op}")
    return fn(series, value).fillna(False)


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
        sym, indic, op, val = cond["symbol"], cond["indicator"], cond["op"], cond["value"]
        if sym not in data or data[sym].empty or indic not in data[sym].columns:
            return pd.Series(dtype=bool)
        masks.append(_apply_op(data[sym][indic], op, val))

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
    Returns:
        dict with keys: n_samples, prob_positive, mean, median, q25, q75, std,
                        p_value, t_stat, distribution (Series), condition_dates (Index)
    """
    if not conditions:
        return _empty_result("조건을 1개 이상 설정하세요.")

    if target_symbol not in data or data[target_symbol].empty:
        return _empty_result(f"'{target_symbol}' 데이터 없음")

    # 날짜 범위 제한
    target_df = data[target_symbol].copy()
    if lookback_years:
        cutoff = target_df.index.max() - pd.DateOffset(years=lookback_years)
        target_df = target_df[target_df.index >= cutoff]

    # target_indicator 시프트: forward_days 후의 값
    if target_indicator not in target_df.columns:
        return _empty_result(f"'{target_indicator}' 지표가 {target_symbol}에 없음")

    future_values = target_df[target_indicator].shift(-forward_days)

    # 각 조건의 마스크 계산
    masks = []
    for cond in conditions:
        sym   = cond["symbol"]
        indic = cond["indicator"]
        op    = cond["op"]
        val   = cond["value"]

        if sym not in data or data[sym].empty:
            return _empty_result(f"'{sym}' 데이터 없음")
        if indic not in data[sym].columns:
            return _empty_result(f"'{indic}' 지표가 {sym}에 없음")

        cond_series = data[sym][indic]

        # 날짜 범위 맞추기
        if lookback_years:
            cond_series = cond_series[cond_series.index >= cutoff]

        # 인덱스 정렬
        common_idx = target_df.index.intersection(cond_series.index)
        if common_idx.empty:
            return _empty_result("조건과 대상의 날짜가 겹치지 않음")

        cond_series = cond_series.reindex(common_idx)
        mask = _apply_op(cond_series, op, val)
        masks.append(mask)

    # 공통 인덱스에서 조합
    common_idx = masks[0].index
    for m in masks[1:]:
        common_idx = common_idx.intersection(m.index)

    masks_aligned = [m.reindex(common_idx) for m in masks]

    if logic == "AND":
        combined_mask = masks_aligned[0]
        for m in masks_aligned[1:]:
            combined_mask = combined_mask & m
    else:  # OR
        combined_mask = masks_aligned[0]
        for m in masks_aligned[1:]:
            combined_mask = combined_mask | m

    condition_dates = common_idx[combined_mask]

    if condition_dates.empty:
        return _empty_result("조건을 만족하는 날짜가 없음")

    # 해당 날짜의 future_values 추출
    future_aligned = future_values.reindex(common_idx)
    outcome = future_aligned.loc[condition_dates].dropna()

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
        "condition_dates": condition_dates,
        "error":           None,
        # 임계값 확률은 호출부에서 계산 (threshold 파라미터 없이 범용 유지)
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
    for c in conditions:
        lbl = indicator_label_fn(c["indicator"])
        op_lbl = OP_LABELS.get(c["op"], c["op"])
        val = c["value"]
        if c["op"] == "between":
            val_str = f"{val[0]} ~ {val[1]}"
        else:
            val_str = str(val)
        parts.append(f"{c['symbol']}의 {lbl}이(가) {val_str}{op_lbl}")

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
