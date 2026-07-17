"""전략 연구소 챗봇 agent 루프 + 영속/컨텍스트 헬퍼.

DB는 논리적 턴(parts: text/tool_use/tool_result, full payload)을 저장하고, Anthropic 와이어
포맷으로 복원할 때 tool_result는 compact 요약으로 환원한다(chat_lab_spec §5).

공개 API:
  stream_chat_turn — agent 루프를 이벤트로 흘리는 제너레이터(단일 소스, /chat/stream).
  run_chat_turn — 위를 소진해 parts를 반환하는 비스트리밍 진입점(/chat/message).
  _persist, _history_to_wire — 헬퍼(단위 테스트·내부 직접 호출용).
"""
from __future__ import annotations

import json
import logging
import os
import time

from quant_core.ir_engine import classify_status
from sqlmodel import Session, select

from ..models import ChatTurnMetric, Conversation, Message
from ..serialize import clean_json
from .context import attach_context
from .tools import (TOOL_SCHEMAS, attach_methodology, compact_summary, run_adjust, run_simulate,
                    run_tool, save_strategy_tool)
from .prompt import chat_system_prompt

_log = logging.getLogger("app.chat.agent")


def _persist(session: Session, conversation_id: int, role: str, parts: list) -> None:
    session.add(Message(conversation_id=conversation_id, role=role, parts=parts))
    session.commit()


def _history_to_wire(session: Session, conversation_id: int) -> list[dict]:
    """저장된 메시지 → Anthropic messages 배열. tool_result는 compact로 환원하고,
    도구 라운드 경계를 복원해 엄격 교대(assistant tool_use → user tool_result → assistant text)."""
    rows = session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(Message.id)).all()
    wire: list[dict] = []
    for m in rows:
        if m.role == "user":
            text = next((p["text"] for p in m.parts if p.get("type") == "text"), "")
            wire.append({"role": "user", "content": text})
            continue
        a_content: list[dict] = []      # 현재 assistant 블록(text + tool_use)
        results: list[dict] = []        # 현재 라운드의 tool_result(compact)
        for p in m.parts:
            t = p.get("type")
            if t in ("text", "tool_use") and results:
                # 이전 라운드 닫기: assistant 블록 + tool_result user 블록 방출 후 새 라운드 시작
                wire.append({"role": "assistant", "content": a_content})
                wire.append({"role": "user", "content": results})
                a_content = []
                results = []
            if t == "text":
                a_content.append({"type": "text", "text": p["text"]})
            elif t == "tool_use":
                a_content.append({"type": "tool_use", "id": p["id"],
                                  "name": p["name"], "input": p.get("input") or {}})
            elif t == "tool_result":
                results.append({"type": "tool_result", "tool_use_id": p["tool_use_id"],
                                "content": compact_summary(p.get("name", ""), p.get("result") or {})})
        if a_content:
            wire.append({"role": "assistant", "content": a_content})
        if results:
            wire.append({"role": "user", "content": results})
    return wire


MAX_TOOL_ROUNDS = 12    # 한 사용자 턴당 도구 라운드 상한(무한루프 가드). 복합 요청(수급+차트+
                        # 밸류+추천 등 여러 분석을 한 턴에)이 8라운드에 막혀 미완되던 것을 완화 —
                        # 답변 완결성 우선(비용보다 '구체적으로 답한다'가 목표).

# 상한 소진 시 부분결과 강제 종합 지시(Phase 1a) — 도구 없는 마지막 1콜에 주입.
_SYNTH_DIRECTIVE = (
    "도구를 더 쓰지 말고, 지금까지 모은 결과만으로 사용자 질문에 최선의 답을 종합해 주세요. "
    "확인된 것부터 결론을 제시하고, 부족한 부분은 무엇이 왜 부족한지 한 줄로 밝힌 뒤 가능한 다음 단계를 제안하세요.")


def _inject_directive(messages: list[dict], text: str) -> None:
    """마지막 user 턴에 지시 텍스트를 덧붙인다 — 역할 교대(user→user 금지) 유지.
    상한 소진 직후 마지막 메시지는 user(tool_result 리스트)라 그 안에 text 블록을 더한다."""
    if messages and messages[-1].get("role") == "user":
        c = messages[-1]["content"]
        if isinstance(c, list):
            c.append({"type": "text", "text": text})
            return
        if isinstance(c, str):
            messages[-1]["content"] = [{"type": "text", "text": c + "\n\n" + text}]
            return
    messages.append({"role": "user", "content": text})


def _progress_line(name: str, inp: dict) -> str:
    """도구 호출 → 사람이 읽는 진행 라인(결정적·LLM 미사용·$0). 긴 턴의 침묵을 '무응답'으로
    오인하지 않게 '지금 무엇을 하는지'를 계속 보여준다(Phase 1b·④ 진행 스트리밍)."""
    inp = inp or {}
    if name == "describe":
        return f"📄 {inp.get('symbol', '종목')} 종합 리포트 조회"
    if name == "inspect":
        cols = [str(c) for c in (inp.get("columns") or [])][:3]
        return f"📈 {inp.get('symbol', '종목')} 지표 조회" + (f" ({', '.join(cols)})" if cols else "")
    if name == "screen":
        secs = inp.get("sectors") or ([inp["sector"]] if inp.get("sector") else [])
        where = ("·".join(str(s) for s in secs) + " ") if secs else ""
        return f"🔎 {where}종목 스크리닝"
    if name == "compare":
        n = len(inp.get("symbols") or [])
        return f"📊 {n}종목 나란히 비교"
    if name == "simulate":
        return "🧪 백테스트·분석 실행"
    if name == "adjust_analysis":
        return "🔧 직전 분석 값 조정·재계산"
    if name == "research_news":
        return "📰 뉴스 조사"
    if name == "resolve_symbol":
        return f"🔍 '{inp.get('query', '')}' 심볼 찾기"
    if name == "save_strategy":
        return "💾 전략 저장"
    return f"⚙️ {name} 실행"

# 결과 품질 계약 — 턴 메트릭에 '가장 나쁜' 결과상태를 기록(모니터링·error_rate 품질 반영).
_STATUS_RANK = {"ok": 0, "empty": 1, "data_insufficient": 2, "degenerate": 3, "infeasible": 4}


def _worse(a: str | None, b: str | None) -> str | None:
    """두 결과상태 중 더 심각한 것(랭크 높은 쪽). None은 무시."""
    cands = [s for s in (a, b) if s in _STATUS_RANK]
    return max(cands, key=lambda s: _STATUS_RANK[s]) if cands else (a or b)


# ── 실패 fail-soft (Wave 2 T3) — 크래시·도구 예외를 막다른길 대신 구조화 ──────────
# 도구 함수(run_simulate 등)는 *예상된* 실패를 {success:False}로 반환한다. 여기로 오는 건
# 가드를 빠져나간 *예기치 못한* 예외 — anthropic API(일시적·재시도 유효)와 엔진·데이터 raise
# (결정적·재시도 무익)를 갈라 복구 제안을 다르게 준다(막다른 '잠시 후 다시'의 증상 #4a 근본).
_FAILURE_COPY = {
    "transient": ("일시적인 연결 문제로", "잠시 후 다시 시도해 주세요."),
    # 내부 처리 오류를 사용자 '조건'(종목·기간·복잡도) 탓으로 돌리지 않는다 — 유효한 요청도
    # 이 메시지를 받으면 "내 질문이 과했나" 오해한다(희제 실사용 신고). 정직히 재시도만 권한다.
    "analysis": ("오류가 생겨", "잠시 후 다시 시도해 주세요."),
}


def _classify_failure(exc: BaseException) -> str:
    """예외 → 실패 부류. *재시도가 유효한* 일시적 오류(연결·타임아웃·429·5xx)만 transient.
    BadRequest(400) 등 4xx 클라이언트 오류는 재시도 무익 → analysis로 분류해 '잠시 후 다시'로
    감추지 않고 표면화한다(과거 anthropic.APIError 전부를 transient로 묶어, thinking-블록 400 같은
    *지속성* 버그를 '일시적 연결 문제'로 은폐한 부류를 차단·메트릭 error로 포착)."""
    import anthropic
    _retryable = (anthropic.APIConnectionError, anthropic.APITimeoutError,
                  anthropic.RateLimitError, anthropic.InternalServerError)
    return "transient" if isinstance(exc, _retryable) else "analysis"


def _failure_message(klass: str, had_partial: bool) -> str:
    """턴 크래시 시 사용자 fail-soft 답변 — 원인 부류 + (부분결과 안내) + 복구 제안."""
    cause, recover = _FAILURE_COPY.get(klass, _FAILURE_COPY["analysis"])
    partial = " 위에 표시된 중간 결과는 참고하실 수 있어요." if had_partial else ""
    return f"분석 도중 {cause} 멈췄어요.{partial} {recover}"


def _tool_failure_result(tool_name: str, exc: BaseException) -> dict:
    """도구가 예기치 못한 예외로 죽었을 때 모델에 줄 구조화 결과(턴은 계속 — #4a 근본).
    한 도구의 raise가 전체 대화를 막다른길로 만들던 부류를 닫는다(엔진 raise도 여기로 수렴)."""
    _, recover = _FAILURE_COPY.get(_classify_failure(exc), _FAILURE_COPY["analysis"])
    return {"success": False, "status": "infeasible",
            "error": f"'{tool_name}' 분석 실행 중 문제가 생겼습니다. {recover}",
            "verdict": recover}


def _dispatch_tool(session: Session, conversation_id: int, name: str, inp: dict) -> dict:
    """도구 이름 → 엔진/도구 실행. side-effect 도구(simulate·save·adjust)는 대화 소유자·대화맥락이
    필요하고 나머지는 순수 run_tool. 예상된 실패는 각 함수가 {success:False}로 반환한다."""
    if name in ("simulate", "save_strategy", "adjust_analysis"):
        conv = session.get(Conversation, conversation_id)
        uid = conv.user_id if conv else None
        if name == "simulate":
            return run_simulate(session, uid, inp)
        if name == "adjust_analysis":
            return run_adjust(session, conversation_id, inp)
        return save_strategy_tool(session, uid, conversation_id, inp)
    return run_tool(name, inp)


def _ir_sig(ir) -> str | None:
    """IR 서명 — 한 턴 내 '동일 분석 재실행'(③제어 중복 헛돌이) 감지용."""
    if not isinstance(ir, dict):
        return None
    try:
        return json.dumps(ir, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _block_to_wire(b) -> dict:
    """Anthropic 응답 content 블록(SDK 객체) → 다음 호출용 와이어 dict."""
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})}
    return {"type": t}


def _mark_cache_breakpoint(messages: list[dict]) -> None:
    """히스토리 마지막 블록에 cache_control(ephemeral)을 달아 다음 턴이 이전 대화를 캐시 읽기
    (~0.1x)로 재사용하게 한다 — 멀티턴마다 전체 히스토리를 풀가로 재전송하던 O(n²) 입력 비용을
    제거한다(prompt caching, system 블록과 합쳐 최대 2 breakpoint). 빈 히스토리(첫 턴)는 무시.
    문자열 content(user 메시지)는 블록 리스트로 승격해야 마커를 붙일 수 있다."""
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content,
                            "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}


def _log_usage(conversation_id: int, usage) -> None:
    """Anthropic usage를 로깅 — 캐시 적중(cache_read)·토큰 절감을 실측·검증하기 위함(원칙4 검증
    인프라). 스트리밍 응답 메시지엔 usage가 있으나 테스트 fake엔 없으므로 None이면 건너뛴다."""
    if usage is None:
        return
    _log.info("[chat usage] conv=%s in=%s out=%s cache_write=%s cache_read=%s", conversation_id,
              getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
              getattr(usage, "cache_creation_input_tokens", "?"),
              getattr(usage, "cache_read_input_tokens", "?"))


def _accumulate_usage(acc: dict, usage) -> None:
    """라운드별 Anthropic usage를 턴 누적기에 더한다(usage 없으면 무동작)."""
    if usage is None:
        return
    acc["in"] += getattr(usage, "input_tokens", 0) or 0
    acc["out"] += getattr(usage, "output_tokens", 0) or 0
    acc["cr"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    acc["cw"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def _persist_turn_metric(session: Session, conversation_id: int, model: str,
                         acc: dict, ttft_ms, latency_ms: int, ok: bool,
                         result_status: str | None = None,
                         result_shape: str | None = None) -> None:
    """턴별 ChatTurnMetric 1행 적재(chat-perf 측정 환경). user_id는 대화 소유자.

    적재 실패가 대화 응답을 깨지 않도록 격리한다 — DB 일시오류 시 지표 누락은 허용하고
    대화는 보존(외부 시스템 한계라 fallback 정당). 대화 영속(_persist)은 이미 끝난 뒤다.
    """
    try:
        conv = session.get(Conversation, conversation_id)
        session.add(ChatTurnMetric(
            conversation_id=conversation_id,
            user_id=conv.user_id if conv else None,
            latency_ms=latency_ms, ttft_ms=ttft_ms,
            input_tokens=acc["in"], output_tokens=acc["out"],
            cache_read_tokens=acc["cr"], cache_write_tokens=acc["cw"],
            n_rounds=acc["rounds"], n_tool_calls=len(acc["tools"]),
            tool_names=list(acc["tools"]), model=model,
            stop_reason=acc["stop"], ok=ok, result_status=result_status,
            result_shape=result_shape))
        session.commit()
    except Exception:   # noqa: BLE001 — 지표 누락 허용·대화 보존(원칙: 외부 한계 fallback)
        _log.exception("[chat metric] 적재 실패 conv=%s", conversation_id)
        session.rollback()


def _load_cc_backend():
    """scripts/chat_eval의 검증된 claude -p 구독 백엔드(ClaudeCodeBackend) 단일 출처 로드.
    로컬 전용 — 프로덕션에선 호출되지 않는다(_default_chat_client 플래그 가드)."""
    import sys
    from pathlib import Path
    be = str(Path(__file__).resolve().parents[3] / "scripts" / "chat_eval")
    if be not in sys.path:
        sys.path.insert(0, be)
    from backend import ClaudeCodeBackend
    return ClaudeCodeBackend


def _default_chat_client():
    """챗 턴의 기본 LLM 클라이언트. 기본 = 프로덕션 Anthropic API(키).

    로컬 전용 구독 모드(env QP_CHAT_LOCAL_SUBSCRIPTION=1)면 claude -p 구독 백엔드($0)를 끼운다:
      · 오케스트레이터 루프          = 반환한 ClaudeCodeBackend()
      · NL→IR 컴파일·뉴스 다이제스트 = anthropic.Anthropic 몽키패치(그쪽이 자기 클라이언트 생성)
    둘 다 본인 Pro/Max 구독 쿼터(API 비용 0) — `claude setup-token`의 CLAUDE_CODE_OAUTH_TOKEN 필요.
    ⚠ 로컬 개발 전용. 프로덕션(Railway)은 플래그 미설정 → 항상 API 키 경로(동작 byte-identical)."""
    from ..config import settings
    if os.environ.get("QP_CHAT_LOCAL_SUBSCRIPTION") == "1":
        ccb = _load_cc_backend()
        import anthropic
        anthropic.Anthropic = ccb            # compile_nl·뉴스 다이제스트도 구독 경유($0)
        return ccb()
    import anthropic
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def stream_chat_turn(session: Session, conversation_id: int, user_text: str,
                     *, client=None, model: str | None = None):
    """한 사용자 메시지에 대해 agent 루프를 **스트리밍**으로 실행하는 제너레이터(단일 소스).

    아래 이벤트 튜플을 발생 순서대로 yield한다:
      ("delta", {"text": ...})              모델 서술 토큰(점진 표시)
      ("tool_use", {"id","name","input"})   도구 호출 시작(🔧 실행 중)
      ("tool_result", {"tool_use_id","name","result"})  도구 결과(full payload — 인라인 렌더)
      ("done", {"parts": [...]})            턴 종료 — 영속된 assistant parts(비스트리밍 drain용)

    도구 호출·결과·서술을 DB에 영속한다(run_chat_turn은 이 제너레이터를 소진해 parts를 반환).
    """
    from ..config import settings
    if client is None:
        client = _default_chat_client()      # 기본=API키 · 로컬 구독 모드면 claude -p($0)
    model = model or settings.CHAT_MODEL
    system = [{"type": "text", "text": chat_system_prompt(),
               "cache_control": {"type": "ephemeral"}}]

    messages = _history_to_wire(session, conversation_id)
    _mark_cache_breakpoint(messages)      # ① 히스토리 prompt caching(멀티턴 입력 캐시 재사용)
    messages.append({"role": "user", "content": user_text})
    _persist(session, conversation_id, "user", [{"type": "text", "text": user_text}])
    # 관측성(#P2-c) — 턴 시작을 즉시 로그. 턴이 무흔적으로 죽어도(워커 OOM·하드킬로 finally조차 못 돎)
    # 최소한 '언제 어떤 질문으로 시작했는지'는 남는다(conv#43형 무응답 드롭 재발 시 근본원인 조회 기점).
    _log.info("[chat] turn start conv=%s msg=%r", conversation_id, (user_text or "")[:120])

    assistant_parts: list[dict] = []      # full payload(영속·렌더용)
    # ── 성능 계측(chat-perf) — 턴 종료 시 ChatTurnMetric 1행 ──
    t0 = time.perf_counter()
    acc = {"in": 0, "out": 0, "cr": 0, "cw": 0, "rounds": 0, "tools": [], "stop": None}
    seen_sigs: set[str] = set()      # ③제어 — 이번 턴에 본 분석 IR 서명(중복 재실행 감지)
    ttft_ms = None
    completed = False
    ok = True
    worst_status: str | None = None      # 결과 품질 계약 — 턴 내 가장 나쁜 결과상태(메트릭)
    turn_shape: str | None = None        # 학습 hook(P4) — 이 턴이 선택한 첫 분석법(result_shape)
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            # ── DB 커넥션 반납 — LLM 왕복(수 초~수 분) 동안 어떤 풀 커넥션도 쥐지 않는다 ──────
            # 직전 라운드의 도구(simulate/adjust/save)가 session.get·읽기로 연 트랜잭션을 여기서
            # 커밋해 반납한다. 이 반납이 없으면 요청 세션이 턴 내내 커넥션을 점유해, 동시 챗 부하가
            # 풀(size 5+overflow 10)을 고갈시키고 /auth·/health 포함 전 엔드포인트가 30s 타임아웃→500
            # 난다(챗 부하가 전 서비스를 죽이는 격리 실패). preview cron의 C1(preview_engine.py)과
            # 동일 부류 — 도구 내부가 커밋하든 말든 여기서 무조건 반납해 부류를 한 곳에서 닫는다.
            # 다음 DB 접근은 fresh checkout이라 pool_pre_ping(db.py)이 stale 연결을 자동 보호한다.
            session.commit()
            # 진행 라벨(표시 전용·비영속) — LLM 라운드는 스트림 델타가 없을 수 있어(로컬 shim은
            # 텍스트를 끝에 일괄 방출) 이 이벤트가 없으면 화면이 수 분간 정지된 듯 보인다.
            yield ("progress", {"label": f"모델 응답 생성 중 (라운드 {acc['rounds'] + 1})"})
            with client.messages.stream(model=model, max_tokens=8192, system=system,
                                        thinking={"type": "disabled"},   # Sonnet5는 thinking 기본ON→응답에
                                        # thinking블록 포함→멀티턴 히스토리 재구성 시 유실되면 400. 오케스트레이터는
                                        # thinking 불필요(4.6 검증동작)이라 끈다(라운드마다 thinking토큰 낭비도 제거).
                                        tools=TOOL_SCHEMAS, messages=messages) as stream:
                for delta in stream.text_stream:
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = int((time.perf_counter() - t0) * 1000)
                        yield ("delta", {"text": delta})
                resp = stream.get_final_message()
            _accumulate_usage(acc, getattr(resp, "usage", None))        # 토큰 누적
            _log_usage(conversation_id, getattr(resp, "usage", None))   # ② 라운드별 로그(유지)
            acc["rounds"] += 1
            acc["stop"] = resp.stop_reason

            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    assistant_parts.append({"type": "text", "text": b.text})
            messages.append({"role": "assistant",
                             "content": [_block_to_wire(b) for b in resp.content]})

            if resp.stop_reason != "tool_use":
                completed = True
                break

            tool_results: list[dict] = []
            for b in resp.content:
                if getattr(b, "type", None) != "tool_use":
                    continue
                inp = dict(b.input or {})
                acc["tools"].append(b.name)
                yield ("tool_use", {"id": b.id, "name": b.name, "input": inp,
                                    "progress": _progress_line(b.name, inp)})
                try:
                    full = _dispatch_tool(session, conversation_id, b.name, inp)
                    # P4 context 사이드카 — describe/select 결과를 준실시간 시세·뉴스로 enrich
                    # (엔진 밖·골든 무누출·best-effort). 모델 해석·웹 맥락 카드용.
                    full = attach_context(full)
                    # 도구 결과의 NaN/inf→None — JSON은 NaN을 표현 못 해 브라우저 JSON.parse·
                    # Postgres JSONB가 깨진다(/ir 백테스트 경로와 동일한 clean_json 재사용).
                    full = clean_json(full)
                except Exception as exc:   # noqa: BLE001 — 한 도구의 예기치 못한 raise(엔진 버그 등)가
                    # 턴을 막다른길로 만들지 않게 구조화 결과로 환원(#4a). 모델은 다른 결과와 함께
                    # 답하거나 이 실패를 구체적으로 설명·조정 제안한다(턴은 ok 유지·status로 표면).
                    _log.exception("[chat] tool %s raised for conversation %s", b.name, conversation_id)
                    full = _tool_failure_result(b.name, exc)
                # 결과 품질 계약 백스톱 — run_query를 안 거치는 결과(inspect·news·save)도 status를 채운다.
                # 계약은 안전장치 — 분류 실패가 턴을 깨지 않도록 격리(B4 회귀 차단).
                if isinstance(full, dict) and full.get("success", True) and "status" not in full:
                    try:
                        full.update(classify_status(full))
                    except Exception:   # noqa: BLE001 — 품질 주석 실패가 대화를 깨면 안 됨
                        _log.exception("[chat] classify_status 실패 conv=%s", conversation_id)
                try:
                    full = attach_methodology(full)   # 백테스트면 structured 방법론 동봉(웹 패널·#7·#1)
                except Exception:   # noqa: BLE001 — 방법론 주석은 부가 정보: 실패해도 턴을 깨지 않는다
                    _log.exception("[chat] attach_methodology 실패 conv=%s", conversation_id)
                if isinstance(full, dict):
                    worst_status = _worse(worst_status, full.get("status"))
                    if turn_shape is None and full.get("shape"):
                        turn_shape = full.get("shape")   # 첫 분석결과의 method(P4 학습 hook)
                assistant_parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
                assistant_parts.append({"type": "tool_result", "tool_use_id": b.id,
                                        "name": b.name, "result": full})
                yield ("tool_result", {"tool_use_id": b.id, "name": b.name, "result": full})
                try:
                    content = compact_summary(b.name, full)
                except Exception:   # noqa: BLE001 — compact 요약은 *모델 컨텍스트용 표시 헬퍼*다.
                    # 실제 결과(full payload)는 이미 UI로 yield됐으므로, 요약 렌더가 어떤 형상에서
                    # 터져도 최소 요약으로 대체해 턴을 계속한다 — 결과가 멀쩡한데 요약 예외 하나가
                    # 턴을 통째로 막다른길("처리 중 오류")로 만들던 부류를 근본 차단(희제 실사용 신고).
                    _log.exception("[chat] compact_summary 실패 conv=%s tool=%s", conversation_id, b.name)
                    ok_flag = full.get("success", True) if isinstance(full, dict) else True
                    content = (f"[{b.name}] 결과를 받았습니다(요약 생성 실패 — 결과 카드를 참고해 해석하세요)."
                               if ok_flag else f"[{b.name} 실패]")
                sig = _ir_sig(full.get("ir")) if isinstance(full, dict) else None
                if sig is not None:
                    if sig in seen_sigs:        # ③제어 — 한 턴 내 동일 IR 재실행 = 헛돌이
                        content = ("⚠ 직전과 동일한 분석입니다(같은 IR). 재실행하지 말고 이 결과로 답하세요. "
                                   "값을 바꾸려면 adjust_analysis를 쓰세요.\n" + content)
                    seen_sigs.add(sig)
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
            messages.append({"role": "user", "content": tool_results})
    except GeneratorExit:
        # 관측 백스톱(#P2-c) — 클라이언트 끊김/타임아웃으로 스트림이 중단되면 제너레이터가 close되어
        # GeneratorExit가 현재 yield로 던져진다(BaseException이라 아래 except Exception 미포착). 이때
        # assistant·metric을 남기는 :452~454가 실행되지 못해 **아무 흔적 없이 죽던 부류**(conv#43:
        # user 메시지만 있고 assistant·metric 0)의 근본. 경고 로그 + 부분결과 영속 + '중단' 메트릭을
        # 남기고 GeneratorExit를 재raise한다(제너레이터 규약 — 여기서 yield 금지).
        elapsed = int((time.perf_counter() - t0) * 1000)
        _log.warning("[chat] turn aborted (client disconnect/timeout) conv=%s rounds=%s tools=%s "
                     "elapsed=%sms parts=%s", conversation_id, acc["rounds"], acc["tools"],
                     elapsed, len(assistant_parts))
        try:
            if assistant_parts:
                _persist(session, conversation_id, "assistant", assistant_parts)   # 부분 흔적 보존
            acc["stop"] = acc["stop"] or "aborted"
            _persist_turn_metric(session, conversation_id, model, acc, ttft_ms, elapsed,
                                 False, worst_status or "aborted", turn_shape)      # 항상 메트릭 1행
        except Exception:   # noqa: BLE001 — 중단 트레이스 영속 실패가 GeneratorExit를 가리지 않게
            _log.exception("[chat] abort 트레이스 영속 실패 conv=%s", conversation_id)
        raise
    except Exception as exc:   # noqa: BLE001 — 도구 밖(LLM 스트림·영속 등) 예기치 못한 실패의 최후
        # 방어선. 막다른 '잠시 후 다시' 대신 실패 부류별 복구 + (있으면) 부분결과 안내를 표면화하고,
        # 메트릭에 'error'로 분류해 bad_result_rate가 크래시를 포착하게 한다(#4a).
        ok = False
        _log.exception("[chat] turn failed for conversation %s", conversation_id)
        worst_status = "error"      # 턴-레벨 크래시(분석 품질 status와 별개·가장 심각)
        had_partial = any(p.get("type") == "tool_result"
                          and isinstance(p.get("result"), dict) and p["result"].get("success")
                          for p in assistant_parts)
        err = _failure_message(_classify_failure(exc), had_partial)
        assistant_parts.append({"type": "text", "text": err})
        yield ("delta", {"text": err})
    if ok and not completed:
        # 라운드 상한을 도구호출로 소진 — 비답변("분석이 길어졌습니다")이 아니라 **지금까지 모은
        # 결과로 부분결과를 강제 종합**한다(무응답 방지·chat-latency-context-redesign Phase 1a).
        # 도구 없는 1콜(tools 미전달 → 모델이 텍스트만 낼 수밖에 없음)로 최선의 답을 짓게 한다.
        # 상한 소진은 드문 경로라 이 추가 1콜은 무시할 만하다(무응답 > 1콜의 비용).
        _inject_directive(messages, _SYNTH_DIRECTIVE)
        yield ("progress", {"label": "부분 결과 종합 중"})
        try:
            synth_text = ""
            with client.messages.stream(model=model, max_tokens=8192, system=system,
                                        thinking={"type": "disabled"}, messages=messages) as stream:
                for delta in stream.text_stream:
                    if delta:
                        synth_text += delta
                        yield ("delta", {"text": delta})
                fin = stream.get_final_message()
            _accumulate_usage(acc, getattr(fin, "usage", None))
            acc["rounds"] += 1
            acc["stop"] = getattr(fin, "stop_reason", None)
            if synth_text.strip():
                assistant_parts.append({"type": "text", "text": synth_text})
                completed = True
        except Exception:   # noqa: BLE001 — 종합 콜마저 실패하면 최소 안내로 폴백(막다른길 방지)
            _log.exception("[chat] 부분결과 종합 콜 실패 conv=%s", conversation_id)
        if not completed:
            msg = ("분석이 예상보다 길어져 완결하지 못했어요. 지금까지 확인한 범위로 답을 드리려 했으나 "
                   "그마저 어려워, 종목·기간·비교 기준을 조금 좁혀 다시 여쭤봐 주시면 바로 완성하겠습니다.")
            assistant_parts.append({"type": "text", "text": msg})
            yield ("delta", {"text": msg})
    _persist(session, conversation_id, "assistant", assistant_parts)
    _persist_turn_metric(session, conversation_id, model, acc, ttft_ms,
                         int((time.perf_counter() - t0) * 1000), ok, worst_status, turn_shape)
    yield ("done", {"parts": assistant_parts})


def run_chat_turn(session: Session, conversation_id: int, user_text: str,
                  *, client=None, model: str | None = None) -> list[dict]:
    """비스트리밍 진입점 — 스트리밍 코어를 소진해 이번 턴 assistant parts(full payload)를 반환.
    영속은 stream_chat_turn 안에서 일어난다(단일 소스)."""
    parts: list[dict] = []
    for kind, payload in stream_chat_turn(session, conversation_id, user_text,
                                          client=client, model=model):
        if kind == "done":
            parts = payload["parts"]
    return parts
