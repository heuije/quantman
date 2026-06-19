"""인사이트 엔진 결과 → 모델 컨텍스트용 'shape 파생' 텍스트 투영.

챗 에이전트가 도구결과를 모델에 되먹일 때 쓰는 요약. 기존 compact_summary(도구이름 기반·
형상 맹목)의 근본 대체: **결과의 실제 형상(result_shape)에서** 모델이 답하기에 충분한 핵심
구조를 직렬화한다 — buckets/windows/factors/top-N 등 **분할 결과를 포함**해 모델이 '연도별·
파라미터별·팩터별'을 한 번에 보고 답하게 한다(못 본 답을 찾아 재실행하던 헛돌이 차단).

원칙: 숫자는 결과에서만(지어내기 금지). 토큰 가드 — 행 상한·소수 자릿수.

형상 판별식은 엑셀 증빙(excel_export.build_strategy_excel)·웹 차트(ChatResultBody)와 **동일
순서**다(3투영 일관). P3에서 엔진이 result["shape"]를 정본으로 스탬프하면 셋 모두 그 키로 수렴한다.
"""
from __future__ import annotations

from typing import Any


def result_shape(result: Any) -> str:
    """엔진 결과 dict → canonical 형상 태그. excel_export·ChatResultBody와 동일 판별 순서.

    ⚠ axis+buckets(분할) 판별을 equity(단일 백테스트)보다 **앞**에 둔다 — 국면대조는
    top-level equity를 함께 실어 보내므로 뒤에 두면 일반 백테스트로 오인된다(#169 교훈).
    """
    if not isinstance(result, dict):
        return "unknown"
    if result.get("query") == "select":
        return "select"
    if result.get("report") == "single":
        return "describe_single"
    if result.get("report") == "portfolio":
        return "describe_portfolio"
    if result.get("reduction") == "extremize":
        return "extremize"
    axis = result.get("axis")
    if axis == "relation":
        return "relate_regression" if result.get("relation") == "regression" else "relate_ic"
    if axis == "time":
        return "event_study"
    if axis == "signal":
        return "signal_dist"
    if axis and result.get("buckets"):
        return "sweep"            # parameter·asset·condition·period_split — '축+버킷' 전부
    if result.get("query") == "inspect":
        return "inspect"
    if result.get("equity") is not None:
        return "simulate"
    return "unknown"


def _f(v: Any, nd: int = 2) -> str:
    """숫자 → 소수 nd자리. None/NaN/비수치 → '—'. (이미 % 단위인 엔진값 그대로 포맷.)"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    return "—" if x != x else f"{x:.{nd}f}"


def _pct(v: Any, nd: int = 1) -> str:
    """분수(0~1, 비중·노출) → 백분율 문자열. 비수치 → '—'."""
    try:
        return f"{float(v) * 100:.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _win(d: Any, w: Any) -> dict:
    """윈도 키가 int(엔진 raw) 또는 str(JSON 직렬화 후) 양쪽일 수 있어 둘 다 시도."""
    if not isinstance(d, dict):
        return {}
    for key in (w, str(w)):
        if key in d and isinstance(d[key], dict):
            return d[key]
    return {}


def _bucket_line(k: str, b: Any) -> str:
    if not isinstance(b, dict):
        return f"  {k}: —"
    if b.get("error"):
        return f"  {k}: 오류({b['error']})"
    return (f"  {k}: 누적 {_f(b.get('cum_return'))}% · CAGR {_f(b.get('cagr'))}% · "
            f"샤프 {_f(b.get('sharpe'))} · MDD {_f(b.get('mdd'))}% (n={b.get('n', '—')})")


def summarize_result(result: Any, *, max_rows: int = 40) -> str:
    """결과를 형상별로 '모델이 답하기에 충분한' 텍스트로 투영. 숫자는 결과에서만."""
    if not isinstance(result, dict):
        return "[결과 없음]"
    if not result.get("success", True):
        return f"[실패] {result.get('error', '알 수 없는 오류')}"
    shape = result_shape(result)

    if shape == "simulate":
        m = result.get("metrics") or {}
        parts = [f"{lbl} {_f(m.get(k))}{u}" for k, lbl, u in (
            ("cagr", "CAGR", "%"), ("sharpe", "샤프", ""), ("mdd", "MDD", "%"),
            ("total_return", "누적", "%")) if m.get(k) is not None]
        for k, lbl, u in (("bench_cagr", "벤치CAGR", "%"), ("win_rate", "승률", "%")):
            if m.get(k) is not None:
                parts.append(f"{lbl} {_f(m.get(k), 1 if k == 'win_rate' else 2)}{u}")
        if m.get("n_trades") is not None:
            parts.append(f"거래 {m.get('n_trades')}회")
        return "[백테스트] " + " · ".join(parts)

    if shape == "sweep":
        axis = result.get("axis")
        buckets = result.get("buckets") or {}
        items = list(buckets.items())
        lbl = {"parameter": "파라미터별", "asset": "종목별", "entity": "종목별",
               "condition": "조건별", "period_split": "기간별"}.get(axis, "구간별")
        m = result.get("metrics") or {}
        head = f"[{lbl} 분할분석] {len(items)}개 구간"
        if m.get("cagr") is not None:
            head += f" · 전체 CAGR {_f(m.get('cagr'))}% 샤프 {_f(m.get('sharpe'))}"
        extra: list[str] = []
        ov = result.get("overall")
        if isinstance(ov, dict) and ov.get("cum_return") is not None:
            extra.append(f"전체(포트): 누적 {_f(ov.get('cum_return'))}% · 샤프 {_f(ov.get('sharpe'))}")
        cons = result.get("consistency") or {}
        if cons.get("n_folds"):
            extra.append(f"일관성: 양(+) {cons.get('positive_folds')}/{cons.get('n_folds')} 구간")
        pw = (result.get("compare") or {}).get("pairwise") or {}
        sig = [p for p, t in pw.items() if isinstance(t, dict)
               and isinstance(t.get("p_value"), (int, float)) and t["p_value"] < 0.05]
        if sig:
            extra.append(f"유의차(p<0.05): {', '.join(map(str, sig))}")
        for w in (result.get("warnings") or []):     # 무거래/데이터결손 등 경고를 모델에 표면화
            msg = w.get("message") if isinstance(w, dict) else str(w)
            if msg:
                extra.append(f"⚠ {msg}")
        lines = [_bucket_line(k, b) for k, b in items[:max_rows]]
        if len(items) > max_rows:
            lines.append(f"  …외 {len(items) - max_rows}개 구간(생략)")
        return head + ("\n" + "\n".join(extra) if extra else "") + "\n" + "\n".join(lines)

    if shape == "extremize":
        obj = result.get("objective") or {}
        best = result.get("best") or {}
        ranked = result.get("ranked") or []
        lines = [f"  {i}. {r.get('label', '?')}: {_f(r.get('metric_value'), 4)}"
                 for i, r in enumerate(ranked[:max_rows], 1)]
        og = result.get("oos_guard") or {}
        if og.get("consistency") is not None:
            guard = f"\nOOS 일관성(양구간 비율): {_f(og.get('consistency'))}"
        elif og.get("error"):
            guard = f"\nOOS 가드: {og['error']}"
        else:
            guard = ""
        return (f"[최적화] 목적={obj.get('metric')}({obj.get('direction')}) · "
                f"최적={best.get('label', '?')}(목적값 {_f(best.get('metric_value'), 4)})"
                + guard + "\n" + "\n".join(lines))

    if shape == "select":
        rows = result.get("results") or []
        top = ", ".join(f"{r.get('symbol')}({_f(r.get('score'), 3)})" for r in rows[:max_rows])
        return (f"[스크리닝] 후보 {result.get('universe_size', '?')}개 중 {len(rows)}개 선별 "
                f"(as-of {result.get('as_of', '?')}): {top}")

    if shape == "describe_single":
        p = result.get("price") or {}
        rets = p.get("returns") or {}
        risk = result.get("risk") or {}
        f = result.get("fundamentals") or {}
        return (f"[종목분석] {result.get('symbol')}({result.get('sector', '')}) · "
                f"현재가 {_f(p.get('last'))} · 1M {_f(rets.get('1m'))}% · 12M {_f(rets.get('12m'))}% · "
                f"52주 {_f(p.get('low_52w'))}~{_f(p.get('high_52w'))} · 연변동성 {_f(risk.get('vol_annualized'))}% · "
                f"MDD {_f(risk.get('max_drawdown'))}% · PBR {_f(f.get('pb_ratio'))} · PER {_f(f.get('trailing_pe'))} · "
                f"EV/EBITDA {_f(f.get('ev_ebitda'))}")

    if shape == "describe_portfolio":
        c = result.get("concentration") or {}
        v = result.get("valuation") or {}
        risk = result.get("risk") or {}
        sx = result.get("sector_exposure") or {}
        sectors = ", ".join(f"{k} {_pct(val)}%" for k, val in list(sx.items())[:5])
        return (f"[포트진단] {result.get('n_holdings', '?')}종목 · HHI {_f(c.get('hhi'), 4)} "
                f"(유효 {_f(c.get('effective_n'))}종목) · 최대비중 {_pct(c.get('top_weight'))}% · "
                f"가중PBR {_f(v.get('weighted_pb'))} · 연변동성 {_f(risk.get('portfolio_vol_annualized'))}%"
                + (f" · 섹터: {sectors}" if sectors else ""))

    if shape == "relate_ic":
        bw = result.get("by_window") or {}
        lines = []
        for w in result.get("windows") or []:
            wd = _win(bw, w)
            o = wd.get("overall") or {}
            lines.append(f"  {w}일: 평균IC {_f(o.get('mean'), 4)} · t {_f(o.get('t_stat'))} · "
                         f"p {_f(o.get('p_value'), 4)} · IR {_f(wd.get('ir'))}")
        return "[팩터 IC] 신호↔미래수익 횡단상관(p<0.05=유의)\n" + "\n".join(lines)

    if shape == "relate_regression":
        bw = result.get("by_window") or {}
        lines = [f"  설명변수: {', '.join(result.get('factor_names') or [])}"]
        for w in result.get("windows") or []:
            for fac in (_win(bw, w).get("factors") or []):
                lines.append(f"  {w}일 {fac.get('name')}: coef {_f(fac.get('coef'), 4)} · t {_f(fac.get('t_stat'))}")
        return "[다중팩터 회귀 Fama-MacBeth] 계수 양(+)·t유의=미래수익과 양의 관계\n" + "\n".join(lines)

    if shape == "event_study":
        overall = result.get("overall") or {}
        lines = []
        for w in result.get("windows") or []:
            o = _win(overall, w)
            lines.append(f"  +{w}일: 평균 {_f(o.get('mean'))}% · p {_f(o.get('p_value'), 4)} · "
                         f"양(+) {_f(o.get('prob_positive'), 1)}% · MAE {_f(o.get('mean_mae'))}% · MFE {_f(o.get('mean_mfe'))}%")
        return (f"[이벤트] {result.get('n_events', '?')}건 · 기준 {result.get('basis', 'close')} (forward 수익)\n"
                + "\n".join(lines))

    if shape == "signal_dist":
        o = result.get("overall") or {}
        q = o.get("quantiles") or {}
        return (f"[신호분포] n={o.get('n', '?')} · 평균 {_f(o.get('mean'), 4)} · 표준편차 {_f(o.get('std'), 4)} · "
                f"분위 q05 {_f(q.get('q05'), 4)} / q50 {_f(q.get('q50'), 4)} / q95 {_f(q.get('q95'), 4)}")

    if shape == "inspect":
        cols = result.get("columns") or []
        series = result.get("series") or {}
        dates = result.get("dates") or []
        last = []
        for col in cols:
            vals = [x for x in (series.get(col) or []) if x is not None]
            if vals:
                last.append(f"{col}={_f(vals[-1], 6)}")
        rng = f"{dates[0]}~{dates[-1]}" if dates else "?"
        return (f"[원시 시계열] {result.get('symbol')} {rng} ({len(dates)}일) · "
                f"최근값 {', '.join(last) if last else '없음'}")

    return "[분석 완료]"
