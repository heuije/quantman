"""챗봇 성능 분석 CLI — 측정+진단 환경. railway run으로 prod Neon 조회.

사용:
    railway run python -m app.chat_analytics stats --days 7
    railway run python -m app.chat_analytics transcripts --days 7 --suspect
    (로컬: QP_DB_URL 미설정 시 SQLite — 빈 데이터)

집계(stats)·트랜스크립트(transcripts)는 순수 함수(session 인자)로 분리해 단위 테스트하고,
argparse glue는 from .db import engine으로 세션을 열어 호출·출력만 한다(manage.py 패턴).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .db import engine
from .models import ChatTurnMetric, Conversation, Message


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _pct(vals: list[int], p: int) -> int:
    """하위-순위 분위수(numpy 없이). 빈 리스트는 0."""
    if not vals:
        return 0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    return int(s[int(k)])


def compute_stats(session: Session, days: int = 7) -> dict:
    """최근 days일(KST 무관·UTC 윈도) 챗봇 turn 지표 집계."""
    rows = session.exec(
        select(ChatTurnMetric).where(ChatTurnMetric.created_at >= _since(days))).all()
    n = len(rows)
    if not n:
        return {"turns": 0, "days": days}
    inp = [r.input_tokens for r in rows]
    out = [r.output_tokens for r in rows]
    cr = [r.cache_read_tokens for r in rows]
    lat = [r.latency_ms for r in rows]
    ttft = [r.ttft_ms for r in rows if r.ttft_ms is not None]
    tools = Counter(t for r in rows for t in (r.tool_names or []))
    rounds = Counter(r.n_rounds for r in rows)
    # 결과 품질 계약 — 빈/퇴화/불가 결과 비율(ok=크래시여부와 직교, 품질 신호).
    statuses = Counter(getattr(r, "result_status", None) for r in rows if getattr(r, "result_status", None))
    bad = sum(1 for r in rows if getattr(r, "result_status", None) not in (None, "ok"))
    total_in, total_cr = sum(inp), sum(cr)
    return {
        "turns": n,
        "users": len({r.user_id for r in rows}),
        "days": days,
        "input_tok": {"p50": _pct(inp, 50), "p90": _pct(inp, 90), "max": max(inp)},
        "output_tok": {"p50": _pct(out, 50), "p90": _pct(out, 90), "max": max(out)},
        "cache_read_tok": {"p50": _pct(cr, 50), "p90": _pct(cr, 90), "max": max(cr)},
        "cache_hit_rate": round(total_cr / (total_in + total_cr), 3) if (total_in + total_cr) else 0.0,
        "latency_ms": {"p50": _pct(lat, 50), "p90": _pct(lat, 90), "max": max(lat)},
        "ttft_ms": ({"p50": _pct(ttft, 50), "p90": _pct(ttft, 90), "max": max(ttft)}
                    if ttft else None),
        "tools": dict(tools.most_common()),
        "rounds_dist": dict(sorted(rounds.items())),
        "error_rate": round(sum(1 for r in rows if not r.ok) / n, 3),
        "result_status_dist": dict(statuses.most_common()),
        "bad_result_rate": round(bad / n, 3),
    }


def format_stats(st: dict) -> str:
    if not st.get("turns"):
        return f"(최근 {st['days']}일: 챗봇 turn 데이터 없음)"
    def trio(d): return f"p50={d['p50']} p90={d['p90']} max={d['max']}"
    lines = [
        f"=== 챗봇 성능 (최근 {st['days']}일) ===",
        f"  turns={st['turns']}  users={st['users']}  error_rate={st['error_rate']}"
        f"  bad_result_rate={st.get('bad_result_rate', 0)}",
        f"  result_status {st.get('result_status_dist') or '(없음)'}",
        f"  input_tok   {trio(st['input_tok'])}",
        f"  output_tok  {trio(st['output_tok'])}",
        f"  cache_read  {trio(st['cache_read_tok'])}  hit_rate={st['cache_hit_rate']}",
        f"  latency_ms  {trio(st['latency_ms'])}",
        f"  ttft_ms     {trio(st['ttft_ms']) if st['ttft_ms'] else '(없음)'}",
        f"  tools       {st['tools'] or '(없음)'}",
        f"  rounds_dist {st['rounds_dist']}",
    ]
    return "\n".join(lines)


_HEDGES = ("할 수 없", "지원하지 않", "확인이 어렵", "확인할 수 없", "제공할 수 없", "알 수 없")
_NEGATIONS = ("아니", "그게 아니", "그거 말고")


def _text_of(parts: list) -> str:
    return " ".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _is_suspect(metric, answer: str, next_user_text: str | None) -> bool:
    """미답변 후보 표층 휴리스틱(별도 API 0) — 판정 아닌 우선순위 신호."""
    if metric is not None and metric.n_tool_calls == 0:
        return True
    if any(h in answer for h in _HEDGES):
        return True
    if next_user_text and any(neg in next_user_text for neg in _NEGATIONS):
        return True
    return False


def _render_part(p: dict) -> str:
    t = p.get("type")
    if t == "text":
        return f"  [봇] {p.get('text', '')}"
    if t == "tool_use":
        return f"  [도구] {p.get('name')}({p.get('input')})"
    if t == "tool_result":
        return f"  [결과] {p.get('name')}: {p.get('result')}"
    return ""


def render_transcripts(session: Session, days: int = 7, limit: int | None = None,
                       conv_id: int | None = None, suspect: bool = False) -> str:
    """대화별·턴별 가독 트랜스크립트(정확도 채점용). full 도구결과 포함 → 근거 대조 가능.

    suspect=True면 미답변 후보 턴만(표층 휴리스틱). limit은 대화 수 상한.
    """
    q = select(Conversation)
    if conv_id is not None:
        q = q.where(Conversation.id == conv_id)
    else:
        q = q.where(Conversation.created_at >= _since(days)).order_by(Conversation.id.desc())
    convs = session.exec(q).all()

    blocks: list[str] = []
    shown = 0
    for conv in convs:
        if limit is not None and shown >= limit:
            break
        msgs = session.exec(select(Message).where(Message.conversation_id == conv.id)
                            .order_by(Message.id)).all()
        metrics = session.exec(select(ChatTurnMetric)
                               .where(ChatTurnMetric.conversation_id == conv.id)
                               .order_by(ChatTurnMetric.id)).all()
        # user→assistant 쌍을 턴으로 묶고, 생성순 metric을 1:1 매핑.
        users = [m for m in msgs if m.role == "user"]
        assts = [m for m in msgs if m.role == "assistant"]
        turn_lines: list[str] = []
        for i, um in enumerate(users):
            am = assts[i] if i < len(assts) else None
            met = metrics[i] if i < len(metrics) else None
            answer = _text_of(am.parts) if am else ""
            next_user = users[i + 1].parts[0].get("text") if i + 1 < len(users) else None
            if suspect and not _is_suspect(met, answer, next_user):
                continue
            lines = [f"  [유저] {um.parts[0].get('text', '')}"]
            if am:
                lines += [_render_part(p) for p in am.parts if _render_part(p)]
            if met:
                lines.append(f"  · {met.input_tokens}+{met.output_tokens}tok "
                             f"cache_read={met.cache_read_tokens} {met.latency_ms}ms "
                             f"rounds={met.n_rounds} tools={met.tool_names} ok={met.ok}")
            turn_lines.append("\n".join(lines))
        if turn_lines:
            blocks.append(f"=== conv #{conv.id} (user {conv.user_id}) ===\n"
                          + "\n  ---\n".join(turn_lines))
            shown += 1
    return "\n\n".join(blocks) if blocks else "(해당 트랜스크립트 없음)"


def main_cli(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="챗봇 성능 분석 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="토큰·지연·도구 집계")
    p_stats.add_argument("--days", type=int, default=7)

    p_tr = sub.add_parser("transcripts", help="채점용 가독 트랜스크립트")
    p_tr.add_argument("--days", type=int, default=7)
    p_tr.add_argument("--limit", type=int, default=None, help="대화 수 상한")
    p_tr.add_argument("--conv", type=int, default=None, help="특정 대화 id")
    p_tr.add_argument("--suspect", action="store_true", help="미답변 후보만(휴리스틱)")

    args = ap.parse_args(argv)
    with Session(engine) as s:
        if args.cmd == "stats":
            print(format_stats(compute_stats(s, days=args.days)))
        elif args.cmd == "transcripts":
            print(render_transcripts(s, days=args.days, limit=args.limit,
                                     conv_id=args.conv, suspect=args.suspect))


if __name__ == "__main__":
    main_cli()
