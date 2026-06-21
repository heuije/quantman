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
import time

from sqlmodel import Session, select

from ..models import ChatTurnMetric, Conversation, Message
from ..serialize import clean_json
from .context import attach_context
from .tools import (TOOL_SCHEMAS, compact_summary, run_adjust, run_simulate, run_tool,
                    save_strategy_tool)
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


MAX_TOOL_ROUNDS = 8     # 한 사용자 턴당 도구 라운드 상한(무한루프·비용 가드)


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
                         acc: dict, ttft_ms, latency_ms: int, ok: bool) -> None:
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
            stop_reason=acc["stop"], ok=ok))
        session.commit()
    except Exception:   # noqa: BLE001 — 지표 누락 허용·대화 보존(원칙: 외부 한계 fallback)
        _log.exception("[chat metric] 적재 실패 conv=%s", conversation_id)
        session.rollback()


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
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    model = model or settings.CHAT_MODEL
    system = [{"type": "text", "text": chat_system_prompt(),
               "cache_control": {"type": "ephemeral"}}]

    messages = _history_to_wire(session, conversation_id)
    _mark_cache_breakpoint(messages)      # ① 히스토리 prompt caching(멀티턴 입력 캐시 재사용)
    messages.append({"role": "user", "content": user_text})
    _persist(session, conversation_id, "user", [{"type": "text", "text": user_text}])

    assistant_parts: list[dict] = []      # full payload(영속·렌더용)
    # ── 성능 계측(chat-perf) — 턴 종료 시 ChatTurnMetric 1행 ──
    t0 = time.perf_counter()
    acc = {"in": 0, "out": 0, "cr": 0, "cw": 0, "rounds": 0, "tools": [], "stop": None}
    seen_sigs: set[str] = set()      # ③제어 — 이번 턴에 본 분석 IR 서명(중복 재실행 감지)
    ttft_ms = None
    completed = False
    ok = True
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            with client.messages.stream(model=model, max_tokens=4096, system=system,
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
                yield ("tool_use", {"id": b.id, "name": b.name, "input": inp})
                if b.name in ("simulate", "save_strategy", "adjust_analysis"):
                    conv = session.get(Conversation, conversation_id)
                    uid = conv.user_id if conv else None
                    if b.name == "simulate":
                        full = run_simulate(session, uid, inp)
                    elif b.name == "adjust_analysis":
                        full = run_adjust(session, conversation_id, inp)
                    else:
                        full = save_strategy_tool(session, uid, conversation_id, inp)
                else:
                    full = run_tool(b.name, inp)
                # P4 context 사이드카 — describe/select 결과를 준실시간 시세·뉴스로 enrich
                # (엔진 밖·골든 무누출·best-effort). 모델 해석·웹 맥락 카드용.
                full = attach_context(full)
                # 도구 결과의 NaN/inf→None — JSON은 NaN을 표현 못 해 브라우저 JSON.parse·
                # Postgres JSONB가 깨진다(/ir 백테스트 경로와 동일한 clean_json 재사용).
                full = clean_json(full)
                assistant_parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
                assistant_parts.append({"type": "tool_result", "tool_use_id": b.id,
                                        "name": b.name, "result": full})
                yield ("tool_result", {"tool_use_id": b.id, "name": b.name, "result": full})
                content = compact_summary(b.name, full)
                sig = _ir_sig(full.get("ir")) if isinstance(full, dict) else None
                if sig is not None:
                    if sig in seen_sigs:        # ③제어 — 한 턴 내 동일 IR 재실행 = 헛돌이
                        content = ("⚠ 직전과 동일한 분석입니다(같은 IR). 재실행하지 말고 이 결과로 답하세요. "
                                   "값을 바꾸려면 adjust_analysis를 쓰세요.\n" + content)
                    seen_sigs.add(sig)
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
            messages.append({"role": "user", "content": tool_results})
    except Exception:   # noqa: BLE001 — 외부 LLM·도구 호출 실패는 대화에 오류 답변으로 표면화(고아 방지)
        ok = False
        _log.exception("[chat] turn failed for conversation %s", conversation_id)
        err = "분석 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
        assistant_parts.append({"type": "text", "text": err})
        yield ("delta", {"text": err})
    if ok and not completed:
        # 라운드 상한을 도구호출로 소진 — 최종 답변 없이 끝나는 무응답 방지(graceful).
        # 위임 후 simulate가 1라운드로 성공해 이 경로는 드묾(방어선·추가 LLM콜 0).
        msg = "요청을 완료하지 못했어요(분석이 길어졌습니다). 조금 더 구체적으로 말씀해 주시겠어요?"
        assistant_parts.append({"type": "text", "text": msg})
        yield ("delta", {"text": msg})
    _persist(session, conversation_id, "assistant", assistant_parts)
    _persist_turn_metric(session, conversation_id, model, acc, ttft_ms,
                         int((time.perf_counter() - t0) * 1000), ok)
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
