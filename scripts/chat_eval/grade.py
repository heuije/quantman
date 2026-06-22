"""채점 — 트랜스크립트(chat_eval_out/*.json)를 Tier1 결정적 + Tier2 LLM-심판으로 평가 → REPORT.md.

Tier1(코드·pass/fail): D1 라우팅 · D2 IR구조 · D5 수렴 · D6 거부(no_tools). 보조 플래그 D3 숫자
무환각(휴리스틱) · D4 중복 재실행 · D7 시각화 중복(리치 렌더러 shape인데 답변에 마크다운 표).
Tier2(claude -p 심판·0~5): J1 해석충실 J2 응답성 J3 무환각 J4 협의 J5 쉬운말요약 J6 정직 J7 시각화중복/가독성.

run과 분리 — 저장된 트랜스크립트만 읽어 채점하므로 --regrade로 LLM 호출 없이 루브릭 재튜닝 가능.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "chat_eval_out"

_ERROR_MARKERS = ("분석 중 오류가 발생했어요", "요청을 완료하지 못했어요")

# 결과 형상 → 렌더러 설명(심판에게 "무엇이 이미 차트/카드로 떴는지" 알려 J7 판정 근거 제공).
SHAPE_RENDERER = {
    "describe_single": "단일종목 360 카드(가격·기간수익·변동성·MDD·밸류에이션·준실시간 시세·뉴스)",
    "describe_portfolio": "포트폴리오 진단 카드(집중도 HHI·섹터노출·포트 변동성)",
    "select": "랭킹 표 + 막대차트",
    "correlation_matrix": "상관 히트맵",
    "prescribe": "목적함수별 비중 트리맵 + 포트 지표",
    "breadth": "시장폭 패널(등락 수·MA상회·섹터별 강약)",
    "news_research": "뉴스 다이제스트 카드(인용 포함)",
    "inspect": "원시 지표 시계열 라인차트",
    "relation": "팩터-forward수익 관계 차트/표",
    "ic": "정보계수(IC) 표",
    "regression": "다중회귀 계수·t값 표",
}


def _by_path(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


_MISSING = object()


def _has_md_table(text: str) -> bool:
    """마크다운 표 감지 — 파이프 2개 이상 줄이 2줄 이상, 또는 구분선(|---)."""
    if re.search(r"\|\s*-{2,}", text):
        return True
    rows = [ln for ln in text.splitlines() if ln.count("|") >= 2]
    return len(rows) >= 2


def _numbers(s: str) -> set[str]:
    """텍스트의 숫자 토큰(쉼표·% 제거, 절댓값 문자열)."""
    out = set()
    for m in re.findall(r"-?\d[\d,]*\.?\d*", s):
        v = m.replace(",", "").lstrip("-").rstrip(".")
        if v and v not in ("0", "1", "2", "3", "5", "10", "12", "20", "60"):  # 흔한 파라미터 제외
            out.add(v)
    return out


def _flatten_numbers(obj, acc: set[str]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        acc.add(str(abs(round(obj, 2))).rstrip("0").rstrip("."))
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_numbers(v, acc)
    elif isinstance(obj, str):
        for n in _numbers(obj):
            acc.add(n)


def _collect(t: dict) -> dict:
    tools_used: list[str] = []
    irs: list[dict] = []
    shapes: list[str] = []
    final_texts: list[str] = []
    result_numbers: set[str] = set()
    per_turn_ir_sigs: list[list[str]] = []
    for turn in t.get("turns", []):
        last_text = None
        sigs: list[str] = []
        for p in turn.get("parts", []):
            ty = p.get("type")
            if ty == "tool_use":
                tools_used.append(p.get("name"))
            elif ty == "tool_result":
                r = p.get("result") or {}
                if isinstance(r, dict):
                    if isinstance(r.get("ir"), dict):
                        irs.append(r["ir"])
                        sigs.append(json.dumps(r["ir"], sort_keys=True, ensure_ascii=False, default=str))
                    if r.get("shape"):
                        shapes.append(r["shape"])
                    _flatten_numbers(r, result_numbers)
            elif ty == "text":
                last_text = p.get("text")
        final_texts.append(last_text or "")
        per_turn_ir_sigs.append(sigs)
    return {"tools_used": tools_used, "irs": irs, "shapes": shapes,
            "final_texts": final_texts, "result_numbers": result_numbers,
            "per_turn_ir_sigs": per_turn_ir_sigs}


def tier1(scenario: dict, col: dict) -> dict:
    exp = scenario.get("expect", {})
    checks: dict = {}

    # D1 라우팅 — 기대 도구 전부 등장
    want = exp.get("tools") or []
    if want:
        checks["D1_routing"] = all(w in col["tools_used"] for w in want)

    # D6 거부/순수협의 — 도구 호출 없어야
    if exp.get("no_tools"):
        checks["D6_no_tools"] = len(col["tools_used"]) == 0

    # D2 IR 구조 — 어떤 ir이 모든 어설션을 만족
    ir_asserts = exp.get("ir") or {}
    if ir_asserts:
        def _ok(ir):
            for path, want_v in ir_asserts.items():
                got = _by_path(ir, path)
                if got is _MISSING:
                    return False
                if callable(want_v):
                    if not want_v(got):
                        return False
                elif got != want_v:
                    return False
            return True
        checks["D2_ir"] = any(_ok(ir) for ir in col["irs"])

    # D5 수렴 — 매 턴 비-에러 최종답변
    conv = all(ft and not any(m in ft for m in _ERROR_MARKERS) for ft in col["final_texts"])
    checks["D5_converge"] = conv

    # ── 보조 플래그(pass/fail 아님·심판 참고) ──
    flags: dict = {}
    # D4 중복 재실행 — 한 턴 내 동일 ir 서명 반복
    flags["D4_dup_rerun"] = any(len(s) != len(set(s)) for s in col["per_turn_ir_sigs"])
    # D7 시각화 중복 — 리치 렌더러 shape 있는데 답변에 마크다운 표
    rich = any(sh for sh in col["shapes"] if sh != "news_research")
    flags["D7_viz_redundant"] = bool(rich and any(_has_md_table(ft) for ft in col["final_texts"]))
    # D3 숫자 무환각(휴리스틱) — 답변 숫자 중 결과에 없는 개수
    ans_nums: set[str] = set()
    for ft in col["final_texts"]:
        ans_nums |= _numbers(ft)
    unmatched = sorted(n for n in ans_nums if n not in col["result_numbers"])
    flags["D3_unmatched_numbers"] = len(unmatched)

    passed = all(v for v in checks.values())
    return {"checks": checks, "flags": flags, "pass": passed}


_JUDGE_SYSTEM = """너는 퀀트 분석 챗봇의 품질 심사관이다. 주어진 시나리오·도구·결과형상·답변을 보고
아래 7개 기준을 각 0~5점(정수)으로 채점하고, 해당 없으면 null. 챗봇의 계약: 숫자는 도구결과에서만,
백테스트≠예측, 못하는 건 정직히, 모호하면 기본값+선택지로 협의, 답변은 쉬운말 요약으로 시작,
**차트/카드로 이미 렌더되는 데이터를 텍스트 표로 중복하지 말 것**.

기준:
- J1 해석충실: 도구결과 수치를 왜곡·오독 없이 해석했나
- J2 응답성: 사용자 질문에 실제로 답했나
- J3 무환각: 도구가 안 준 사실·숫자를 지어내지 않았나
- J4 협의: 모호한 질문에 빈 되묻기 대신 기본값+선택지를 제안했나(해당 시)
- J5 쉬운말요약: 평이한 요약으로 시작했나
- J6 정직: 백테스트를 예측이라 하지 않고, 한계를 정직히 밝혔나
- J7 시각화중복/가독성: 이미 차트/카드로 뜬 데이터를 텍스트 표로 불필요하게 반복하지 않고 간결한가

반드시 JSON 객체 하나만 출력(코드펜스·설명 금지):
{"scores":{"J1":n,"J2":n,"J3":n,"J4":n,"J5":n,"J6":n,"J7":n},"overall":n,"pass":true/false,"notes":"한두 문장 근거"}"""


def _judge(scenario: dict, col: dict) -> dict:
    from backend import ClaudeCodeBackend, _extract_json
    shapes = sorted(set(col["shapes"]))
    rend = "\n".join(f"  - {s}: {SHAPE_RENDERER.get(s, '(구조화 차트/표 렌더)')}" for s in shapes) or "  (없음)"
    answers = "\n\n".join(f"[턴{i + 1} 답변]\n{x}" for i, x in enumerate(col["final_texts"]))
    user = (f"## 시나리오: {scenario['name']}\n"
            f"사용자: {' / '.join(scenario['turns'])}\n\n"
            f"## 호출된 도구: {', '.join(col['tools_used']) or '(없음)'}\n"
            f"## 결과 형상(이미 차트/카드로 렌더됨·사용자가 봄):\n{rend}\n\n"
            f"## 챗봇 답변:\n{answers}\n\n"
            f"## 이 시나리오 평가 포커스: {scenario.get('expect', {}).get('judge_focus', '')}\n\n"
            f"위 루브릭으로 채점해 JSON만 출력하라.")
    try:
        msg = ClaudeCodeBackend().messages.create(
            model="claude-sonnet-4-6",
            system=[{"type": "text", "text": _JUDGE_SYSTEM}],
            messages=[{"role": "user", "content": user}])
        txt = msg.content[0].text if msg.content else ""
        return _extract_json(txt) or {"error": "판정 JSON 파싱 실패", "raw": txt[:200]}
    except Exception as e:  # noqa: BLE001 — 심판 실패가 채점 전체를 막지 않게
        return {"error": f"{type(e).__name__}: {e}"}


def grade_all(only: str | None = None, judge: bool = True) -> Path:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # backend·corpus
    try:
        import corpus
        _cur_expect = {s["name"]: s.get("expect", {}) for s in corpus.SCENARIOS}
    except Exception:   # noqa: BLE001 — 코퍼스 없으면 트랜스크립트 저장본 사용
        _cur_expect = {}
    files = sorted(OUT.glob("*.json"))
    files = [f for f in files if not f.name.startswith("_")]
    if only:
        files = [f for f in files if only in f.stem]
    rows = []
    for f in files:
        t = json.loads(f.read_text(encoding="utf-8"))
        if t.get("error"):
            rows.append({"name": t["name"], "run_error": t["error"]})
            continue
        col = _collect(t)
        # expect는 현재 코퍼스(SSOT)에서 — 루브릭/기대 수정 후 --regrade가 재실행 없이 반영되게.
        expect = _cur_expect.get(t["name"], t.get("expect", {}))
        scen = {"name": t["name"], "expect": expect,
                "turns": [tn.get("user", "") for tn in t.get("turns", [])]}
        t1 = tier1(scen, col)
        j = _judge(scen, col) if judge else None
        rows.append({"name": t["name"], "tier1": t1, "judge": j,
                     "tools": col["tools_used"], "shapes": sorted(set(col["shapes"]))})
    if judge:
        import backend as _backend                  # 심판 호출 쿼터 집계
        (OUT / "_usage_grade.json").write_text(
            json.dumps(_backend.USAGE_LOG, ensure_ascii=False), encoding="utf-8")
    report = _render_report(rows, judge)
    fp = OUT / "REPORT.md"
    fp.write_text(report, encoding="utf-8")
    _print_summary(rows)
    print(f"\n리포트 → {fp}")
    return fp


def _verdict(row: dict) -> str:
    if row.get("run_error"):
        return "💥"
    t1 = row["tier1"]["pass"]
    j = row.get("judge") or {}
    jp = j.get("pass")
    if t1 and jp is not False:
        return "✅"
    return "❌"


def _render_report(rows: list[dict], judge: bool) -> str:
    n = len(rows)
    n_pass = sum(1 for r in rows if _verdict(r) == "✅")
    js = [r["judge"].get("overall") for r in rows
          if r.get("judge") and isinstance(r["judge"].get("overall"), (int, float))]
    avg = round(sum(js) / len(js), 2) if js else "—"
    usage = _usage_summary()
    lines = ["# 챗봇 LLM 평가 리포트", "",
             f"시나리오 {n}개 · 통과 {n_pass} · 평균 심판점수 {avg}/5 · "
             f"LLM {'심판 포함' if judge else '결정적만'} · {usage}",
             "", "> Tier1=결정적 pass/fail · 심판=claude -p(구독·API $0). 플래그 ⚠는 심판 참고용.",
             "", "| 시나리오 | 판정 | 도구 | 형상 | Tier1 | 심판 | 비고 |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("run_error"):
            lines.append(f"| **{r['name']}** | 💥 | — | — | — | — | 실행오류: {r['run_error'][:60]} |")
            continue
        t1 = r["tier1"]
        checks = " ".join(f"{k.split('_')[0]}{'✅' if v else '❌'}" for k, v in t1["checks"].items())
        fl = t1["flags"]
        notes = []
        if fl["D7_viz_redundant"]:
            notes.append("D7 표중복⚠")
        if fl["D4_dup_rerun"]:
            notes.append("D4 재실행⚠")
        if fl["D3_unmatched_numbers"]:
            notes.append(f"D3 미매칭{fl['D3_unmatched_numbers']}")
        j = r.get("judge") or {}
        jcell = (f"{j.get('overall')}/5" if isinstance(j.get("overall"), (int, float))
                 else ("오류" if j.get("error") else "—"))
        jnote = j.get("notes", "") or j.get("error", "")
        lines.append(f"| **{r['name']}** | {_verdict(r)} | {', '.join(r['tools']) or '—'} | "
                     f"{', '.join(r['shapes']) or '—'} | {checks} | {jcell} | "
                     f"{'; '.join(notes)}{(' · ' + jnote) if jnote else ''} |")
    # 상세
    lines += ["", "## 시나리오별 상세", ""]
    for r in rows:
        if r.get("run_error"):
            lines += [f"### {r['name']} 💥", f"- 실행오류: {r['run_error']}", ""]
            continue
        t1, j = r["tier1"], (r.get("judge") or {})
        lines += [f"### {r['name']} {_verdict(r)}",
                  f"- 도구: {', '.join(r['tools']) or '—'} · 형상: {', '.join(r['shapes']) or '—'}",
                  f"- Tier1: {t1['checks']} · 플래그: {t1['flags']}"]
        if j:
            lines.append(f"- 심판: overall={j.get('overall')} scores={j.get('scores')} — {j.get('notes', j.get('error', ''))}")
        lines.append("")
    return "\n".join(lines)


def _usage_summary() -> str:
    parts = []
    total_in = total_out = 0
    for nm in ("_usage_run.json", "_usage_grade.json"):
        fp = OUT / nm
        if fp.exists():
            try:
                u = json.loads(fp.read_text(encoding="utf-8"))
                total_in += sum(x.get("in", 0) + x.get("cache_create", 0) + x.get("cache_read", 0) for x in u)
                total_out += sum(x.get("out", 0) for x in u)
                parts.append(f"{nm.split('_')[1].split('.')[0]}={len(u)}콜")
            except Exception:  # noqa: BLE001
                pass
    if not parts:
        return "토큰 집계 없음"
    return f"호출 {' '.join(parts)} · 입력~{total_in // 1000}k 출력~{total_out // 1000}k (구독 쿼터·API $0)"


def _print_summary(rows: list[dict]) -> None:
    for r in rows:
        v = _verdict(r)
        extra = ""
        if not r.get("run_error"):
            j = r.get("judge") or {}
            extra = f"심판 {j.get('overall', '—')}/5"
        print(f"  {v} {r['name']:30} {extra}")
    n_pass = sum(1 for r in rows if _verdict(r) == "✅")
    print(f"\n통과 {n_pass}/{len(rows)}")
