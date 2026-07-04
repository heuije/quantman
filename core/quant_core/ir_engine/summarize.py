"""인사이트 엔진 결과 → 모델 컨텍스트용 'shape 파생' 텍스트 투영.

챗 에이전트가 도구결과를 모델에 되먹일 때 쓰는 요약. 기존 compact_summary(도구이름 기반·
형상 맹목)의 근본 대체: **결과의 실제 형상(result_shape)에서** 모델이 답하기에 충분한 핵심
구조를 직렬화한다 — buckets/windows/factors/top-N 등 **분할 결과를 포함**해 모델이 '연도별·
파라미터별·팩터별'을 한 번에 보고 답하게 한다(못 본 답을 찾아 재실행하던 헛돌이 차단).

원칙: 숫자는 결과에서만(지어내기 금지). 토큰 가드 — 행 상한·소수 자릿수.

P3(seam #1 정비): 엔진(run_query)이 성공 결과에 result["shape"]를 스탬프한다 → 이 모듈과
웹 ChatResultBody는 그 키를 **단일 정본**으로 분기(아래 result_shape는 미스탬프 결과만 순서의존
폴백). excel_export는 sweep 변종을 더 잘게 나눠 axis 자체 디스패치를 유지(형상 태그보다 세분).
"""
from __future__ import annotations

from typing import Any


def result_shape(result: Any) -> str:
    """엔진 결과 dict → canonical 형상 태그.

    엔진(run_query)이 스탬프한 result["shape"]가 있으면 그것이 정본. 없으면(inspect 우회·
    레거시·직접 호출) 아래 순서의존 판별로 폴백(행동보존).
    ⚠ 폴백 순서: axis+buckets(분할) 판별을 equity(단일 백테스트)보다 **앞**에 둔다 — 국면대조는
    top-level equity를 함께 실어 보내므로 뒤에 두면 일반 백테스트로 오인된다(#169 교훈).
    """
    if not isinstance(result, dict):
        return "unknown"
    stamped = result.get("shape")
    if isinstance(stamped, str) and stamped:
        return stamped
    if result.get("query") == "select":
        return "select"
    if result.get("query") == "prescribe":
        return "prescribe"
    if result.get("query") == "breadth":
        return "breadth"
    if result.get("report") == "single":
        return "describe_single"
    if result.get("report") == "portfolio":
        return "describe_portfolio"
    if result.get("reduction") == "extremize":
        return "extremize"
    axis = result.get("axis")
    if axis == "relation":
        rel = result.get("relation")
        if rel == "regression":
            return "relate_regression"
        if rel == "correlation":
            return "correlation_matrix"
        return "relate_ic"
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


def _composition_line(result: Any, top: int = 5) -> str:
    """이벤트/풀 분석의 구성 분해(종목 상위·연도)를 한 줄로 — n=수천 단일풀이 '어느 종목·언제'서
    왔는지 모델·사용자가 보게 한다(증상 #4c 섹터 무차별 pooling). composition 없으면 빈 문자열."""
    comp = result.get("composition") if isinstance(result, dict) else None
    if not isinstance(comp, dict):
        return ""
    parts: list[str] = []
    by_sym = comp.get("by_symbol") or {}
    if by_sym:
        items = list(by_sym.items())
        head = ", ".join(f"{s} {c}건" for s, c in items[:top])
        more = f" 외 {len(items) - top}종목" if len(items) > top else ""
        parts.append(f"종목 {head}{more}")
    by_year = comp.get("by_year") or {}
    if by_year:
        yrs = list(by_year.items())
        peak_y, peak_c = max(yrs, key=lambda kv: kv[1])
        parts.append(f"연도 {yrs[0][0]}~{yrs[-1][0]}(최다 {peak_y} {peak_c}건)")
    return "\n  구성: " + " · ".join(parts) if parts else ""


_BULK_WARN_CODES = {"stale_data", "data_gap", "missing_data"}


def _warning_lines(result: Any, max_bulk: int = 4) -> list[str]:
    """경고를 모델용 '⚠ ...' 줄로 — 데이터 결손/공백(stale_data·data_gap·missing_data)은 'all'
    유니버스에서 수십~수백 개 쏟아져 모델 컨텍스트를 범람시키므로 max_bulk개만 보이고 나머지는
    한 줄로 요약한다(R1 — 결과가 자기 품질을 *간결히* 서술). 그 외 경고(사이징·PIT·무거래·선물
    자본 등 소수·중요)는 전부 보존한다. 결과 dict의 warnings 원본은 불변(result_status·UI용)."""
    bulk: list[str] = []
    other: list[str] = []
    for w in (result.get("warnings") or []):
        if isinstance(w, dict):
            msg, code = w.get("message"), w.get("code")
        else:
            msg, code = str(w), None
        if not msg:
            continue
        (bulk if code in _BULK_WARN_CODES else other).append(msg)
    lines = [f"⚠ {m}" for m in other]
    lines += [f"⚠ {m}" for m in bulk[:max_bulk]]
    if len(bulk) > max_bulk:
        lines.append(f"⚠ 외 {len(bulk) - max_bulk}개 후보 심볼 데이터 결손/공백 — "
                     "broad 유니버스에서 해당 구간만 자동 제외(선별·개별 거래엔 영향 없음)")
    return lines


def _context_block(result: Any) -> str:
    """P4 사이드카(준실시간 시세·뉴스)를 모델 식단에 표면화 — 서버가 result["context"]에 붙인다
    (엔진 밖·골든 무누출). context 없으면 빈 문자열(엔진 단독 실행·다른 형상)."""
    ctx = result.get("context") if isinstance(result, dict) else None
    if not isinstance(ctx, dict):
        return ""
    parts: list[str] = []
    quotes = ctx.get("quotes") or {}
    qs = ", ".join(f"{c} {_f(v.get('price'), 0)}({_f(v.get('chg'))}%)"
                   for c, v in list(quotes.items())[:6] if isinstance(v, dict))
    if qs:
        parts.append(f"준실시간 시세 {qs}")
    mkt = ctx.get("market") or {}            # 거시 시장 스냅샷(KR·US 지수+VIX) — breadth 해석용
    ms = ", ".join(f"{k} {_f(v.get('price'))}({_f(v.get('chg'))}%)"
                   for k, v in list(mkt.items())[:6] if isinstance(v, dict))
    if ms:
        parts.append(f"시장 현재가 {ms}")
    news = ctx.get("news") or []
    heads = " / ".join(n.get("title", "").strip() for n in news[:5]
                       if isinstance(n, dict) and n.get("title"))
    if heads:
        parts.append(f"최근뉴스(왜 움직였나): {heads}")
    # 추정실적(FnGuide) — 연도별 추정 EPS를 모델 식단에 표면화한다. 웹 카드엔 다년도(확정+추정 E)가
    # 뜨는데 이 블록이 빠지면 모델은 추정치를 프롬프트 설명으로만 알아 "다음해만/범위 초과"라 잘못
    # 거절한다(데이터엔 forward E연도가 있는데). E연도 라벨로 줘 모델이 그대로 읽어 답하게 한다.
    est = ctx.get("estimates") or {}
    ann, fwd = est.get("annual") or {}, est.get("forward") or {}
    eps_seq = [f"{str(y).split('/')[0]}{'E' if e else ''} {_f(v)}"
               for y, e, v in zip(ann.get("years") or [], ann.get("is_estimate") or [],
                                  ann.get("eps") or []) if v is not None]
    if eps_seq:
        tail = [s for s in (
            f"추정매출성장 {_f(fwd['rev_growth'])}%" if fwd.get("rev_growth") is not None else "",
            f"추정영업이익성장 {_f(fwd['op_growth'])}%" if fwd.get("op_growth") is not None else "",
            f"forward PER {_f(fwd['forward_pe'])}" if fwd.get("forward_pe") is not None else "") if s]
        parts.append("추정실적(FnGuide·E=추정) 연도별EPS(원): " + " · ".join(eps_seq)
                     + (" · " + " · ".join(tail) if tail else ""))
    if not parts:
        return ""
    return ("\n[맥락·준실시간] " + " · ".join(parts)
            + "\n(시세·뉴스는 준실시간 참고 — 분석 수치·등락률은 종가 기준)")


def summarize_result(result: Any, *, max_rows: int = 40) -> str:
    """결과를 형상별로 '모델이 답하기에 충분한' 텍스트로 투영. 숫자는 결과에서만."""
    if not isinstance(result, dict):
        return "[결과 없음]"
    if not result.get("success", True):
        return f"[실패] {result.get('error', '알 수 없는 오류')}"
    shape = result_shape(result)

    if shape == "news_research":          # 뉴스 리서치 = 이미 Haiku 다이제스트 텍스트(모델용)
        # + 시황/왜움직였나 답변이 지수 현재가를 앞세우도록 시장 스냅샷(context.market) 표면화 —
        # 뉴스가 비어도 모델이 "지수레벨 없음"이라 하지 않고 스냅샷으로 답하게(IP1 배선갭).
        return str(result.get("digest") or "[뉴스 리서치 결과 없음]") + _context_block(result)

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
        out = "[백테스트] " + " · ".join(parts)
        for line in _warning_lines(result):            # 경고 표면화(Phase 0.5) — bulk 결손은 캡
            out += f"\n{line}"
        return out

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
        extra += _warning_lines(result)              # 무거래/데이터결손 등 — bulk 결손은 캡
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
        recipe = (result.get("scoring") or {}).get("recipe")   # 점수 산식 노출(산식 확인 불가 방지)
        formula = f" · 점수산식: {recipe}" if recipe else ""
        return (f"[스크리닝] 후보 {result.get('universe_size', '?')}개 중 {len(rows)}개 선별 "
                f"(as-of {result.get('as_of', '?')}){formula}: {top}" + _context_block(result))

    if shape == "describe_single":
        p = result.get("price") or {}
        rets = p.get("returns") or {}
        risk = result.get("risk") or {}
        f = result.get("fundamentals") or {}
        c = result.get("consensus") or {}
        fl = result.get("flow") or {}
        base = (f"[종목분석] {result.get('symbol')}({result.get('sector', '')}) · "
                f"현재가 {_f(p.get('last'))} · 1M {_pct(rets.get('1m'))}% · 12M {_pct(rets.get('12m'))}% · "
                f"52주 {_f(p.get('low_52w'))}~{_f(p.get('high_52w'))} · 연변동성 {_pct(risk.get('vol_annualized'))}% · "
                f"MDD {_pct(risk.get('max_drawdown'))}% · PBR {_f(f.get('pb_ratio'))} · PER {_f(f.get('trailing_pe'))} · "
                f"EV/EBITDA {_f(f.get('ev_ebitda'))}")
        if c.get("consensus_target") is not None:
            base += (f" · 컨센서스 목표가 {_f(c.get('consensus_target'))}"
                     f"(상승여력 {_pct(c.get('target_upside'))}%·애널 {_f(c.get('analyst_count'), 0)}명"
                     f"·의견 {_f(c.get('consensus_opinion'))}[-1매도~+1매수])")
        ib, fb = fl.get("inst_net_buy_20d"), fl.get("foreign_net_buy_20d")
        if ib is not None or fb is not None:
            base += (f" · 최근20일순매수 기관 {_f(ib / 1e8) if ib is not None else '—'}억"
                     f"·외국인 {_f(fb / 1e8) if fb is not None else '—'}억")
        return base + _context_block(result)

    if shape == "describe_portfolio":
        c = result.get("concentration") or {}
        v = result.get("valuation") or {}
        risk = result.get("risk") or {}
        sx = result.get("sector_exposure") or {}
        sectors = ", ".join(f"{k} {_pct(val)}%" for k, val in list(sx.items())[:5])
        return (f"[포트진단] {result.get('n_holdings', '?')}종목 · HHI {_f(c.get('hhi'), 4)} "
                f"(유효 {_f(c.get('effective_n'))}종목) · 최대비중 {_pct(c.get('top_weight'))}% · "
                f"가중PBR {_f(v.get('weighted_pb'))} · 연변동성 {_pct(risk.get('portfolio_vol_annualized'))}%"
                + (f" · 섹터: {sectors}" if sectors else "") + _context_block(result))

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

    if shape == "breadth":
        lines = [f"[시장 breadth] {result.get('n', '?')}종목 · 상승 {result.get('n_up', '?')}/"
                 f"하락 {result.get('n_down', '?')} (상승비율 {_pct(result.get('pct_up'))}%) · "
                 f"평균 1일 {_pct(result.get('avg_r1'))}% / 5일 {_pct(result.get('avg_r5'))}% / "
                 f"20일 {_pct(result.get('avg_r20'))}%",
                 f"  20일선 위 {_pct(result.get('pct_above_ma20'))}% · "
                 f"60일선 위 {_pct(result.get('pct_above_ma60'))}%"]
        sb = result.get("sector_breakdown") or []
        if sb:
            worst = ", ".join(f"{k} {_pct(v)}%" for k, v, _ in sb[:3])
            best = ", ".join(f"{k} {_pct(v)}%" for k, v, _ in sb[-3:][::-1])
            lines.append(f"  섹터 약세: {worst} · 강세: {best}")
        return "\n".join(lines) + _context_block(result)   # 시장 스냅샷(지수·VIX 현재가) 표면화

    if shape == "heatmap":                 # 범용 히트맵(섹터 순환매 등) — 행×열 격자 + 기간별 선두
        rows = result.get("rows") or []
        cols = result.get("cols") or []
        r_ax, c_ax = result.get("row_axis", "행"), result.get("col_axis", "열")
        lines = [f"[{r_ax}×{c_ax} 히트맵] {len(rows)} {r_ax} × {len(cols)} {c_ax} · "
                 f"{result.get('value_label', '값')}({result.get('value_unit', '')})"]
        leaders = result.get("leaders") or []
        if leaders:
            path = " → ".join(f"{lab} {sec}({v:+.1f}%)" for lab, sec, v in leaders)
            lines.append(f"  기간별 선두(순환): {path}")
        if result.get("n_symbols"):
            lines.append(f"  {result['n_symbols']}종목을 {len(rows)}개 {r_ax}로 집계.")
        return "\n".join(lines) + _context_block(result)

    if shape == "prescribe":
        objs = result.get("objectives") or {}
        rec = result.get("recommended") or "max_sharpe"
        lbl = {"min_variance": "최소분산", "max_sharpe": "최대샤프",
               "risk_parity": "리스크패리티", "equal_weight": "동일가중"}
        lines = [f"[포트추천] {len(result.get('symbols') or [])}종목 "
                 f"(n={result.get('n_obs', '?')}일) · 추천={lbl.get(rec, rec)}"]
        for key in ("max_sharpe", "min_variance", "risk_parity", "equal_weight"):
            o = objs.get(key)
            if not isinstance(o, dict):
                continue
            w = o.get("weights") or {}
            top = sorted(w.items(), key=lambda kv: -(kv[1] or 0))[:6]
            ws = ", ".join(f"{s} {_pct(v)}%" for s, v in top)
            lines.append(f"  {lbl.get(key, key)}: 기대변동성 {_pct(o.get('exp_vol'))}% · "
                         f"샤프 {_f(o.get('sharpe'))} · 비중 {ws}")
        lines += _warning_lines(result)
        return "\n".join(lines)

    if shape == "correlation_matrix":
        syms = result.get("symbols") or []
        mc, lc = result.get("most_correlated"), result.get("least_correlated")
        out = (f"[상관행렬] {len(syms)}종목 일별수익 피어슨 상관 · 평균 {_f(result.get('avg_corr'), 3)} "
               f"(n={result.get('n_obs', '?')}일) — 상관 낮을수록 분산효과 큼")
        if isinstance(mc, list) and len(mc) == 3:
            out += f"\n  최고 동행: {mc[0]}↔{mc[1]} {_f(mc[2], 3)}"
        if isinstance(lc, list) and len(lc) == 3:
            out += f"\n  최저(분산·헤지 후보): {lc[0]}↔{lc[1]} {_f(lc[2], 3)}"
        return out

    if shape == "event_study":
        overall = result.get("overall") or {}
        lines = []
        for w in result.get("windows") or []:
            o = _win(overall, w)
            lines.append(f"  +{w}일: 평균 {_f(o.get('mean'))}% · p {_f(o.get('p_value'), 4)} · "
                         f"양(+) {_f(o.get('prob_positive'), 1)}% · MAE {_f(o.get('mean_mae'))}% · MFE {_f(o.get('mean_mfe'))}%")
        head = f"[이벤트] {result.get('n_events', '?')}건 · 기준 {result.get('basis', 'close')} (forward 수익)"
        head += _composition_line(result)        # 풀 구성(종목·연도) — 무차별 pooling 투명화(#4c)
        return head + "\n" + "\n".join(lines)

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
