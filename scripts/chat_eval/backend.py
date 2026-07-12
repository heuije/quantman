"""ClaudeCodeBackend — `claude -p`(Pro/Max 구독) 백엔드로 Anthropic SDK를 흉내내는 shim.

챗봇이 쓰는 SDK 표면만 구현한다:
  .messages.stream(...)  — 오케스트레이터 tool-use 루프 (server/app/chat/agent.py)
  .messages.create(...)  — NL→IR 컴파일 (ir_compiler.py·강제 tool_use) + 뉴스 다이제스트 (평문)

인증: CLAUDE_CODE_OAUTH_TOKEN 환경변수(`claude setup-token` 발급). **API 비용 0**(구독 쿼터).
프로덕션 run_chat_turn(client=)·anthropic.Anthropic 몽키패치에 drop-in으로 끼운다(프로덕션 무변경).

한계: claude -p는 native tool-use가 아니라 *프롬프트형 구조화 출력*으로 결정을 흉내낸다 — 프롬프트·
도구설명·NL→IR·라우팅 로직 검증엔 충분하나 프로덕션 모델과 byte-identical은 아니다(설계서 §11).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

# Windows에선 bare "claude"가 PATHEXT 미해석으로 subprocess에서 안 잡힌다(.CMD) → 풀패스 해석.
_CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
# 600s = 실 anthropic SDK 기본 timeout(10분)과 정합. 240s는 단일-턴 시절 값 —
# 무거운 IR 컴파일(대형 JSON)·max-turns 3·부하 겹침에서 실측 초과(07-12 컴파일 타임아웃).
_TIMEOUT = int(os.environ.get("CHAT_EVAL_CLAUDE_TIMEOUT", "600"))
_MAX_RETRIES = 2               # claude -p 간헐 빈응답 재시도 횟수(총 3회) — 실 anthropic SDK 기본과 정합
_RETRY_BACKOFF_SEC = 0.5       # 재시도 간 대기(초)·시도마다 선형 증가

# 프로덕션 시스템 프롬프트는 native tool-use(emit_strategy·screen 등 도구 호출)를 지시한다.
# claude -p엔 그 도구들이 없어 모델이 tool_use를 시도하면 max_turns로 실패한다(검증됨).
# → tools 모드일 때 시스템 프롬프트 끝에 "도구 호출 불가, JSON 텍스트로만" 오버라이드를 덧붙여
#   결정을 평문 JSON으로 받는다(설계서 §11 프롬프트형 결정).
_NOTOOL_ADDENDUM = (
    "\n\n=== 실행 환경 출력 규칙 (위 모든 지시보다 우선) ===\n"
    "너는 이 환경에서 **도구/함수를 실제로 호출할 수 없다**(호출 가능한 도구가 없다). "
    "위에서 도구 호출(emit_strategy·screen·simulate 등)을 지시하더라도, 도구 호출 메커니즘을 "
    "쓰지 말고 **요청된 JSON 객체 하나만 평문 텍스트로** 출력하라. 코드펜스·설명·여는 말 없이 "
    "오직 JSON 한 개만 응답한다.")

# 호출별 누적 usage(쿼터 리포트용). 러너가 읽어 REPORT에 집계.
USAGE_LOG: list[dict] = []


class _RetryableClaudeError(RuntimeError):
    """일시적 claude -p 실패(빈 응답·is_error·출력 파싱 실패) — 재시도 대상.
    타임아웃·토큰 누락은 이 예외를 쓰지 않아 즉시 전파된다(비재시도)."""


# ── SDK 블록/메시지 흉내 (프로덕션이 접근하는 속성만) ──────────────────────────

@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    name: str
    input: dict
    id: str = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:20]}")
    type: str = "tool_use"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Message:
    content: list
    stop_reason: str
    usage: _Usage


class _Stream:
    """`with client.messages.stream(...) as s:` 컨텍스트매니저. 하니스는 스트리밍 불필요 →
    text_stream은 비어 있고 최종 메시지는 get_final_message()로 한 번에 준다."""

    def __init__(self, message: _Message):
        self._m = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        # 최종 메시지의 text 블록을 delta로 흘려보낸다 → agent 루프의 `for delta in text_stream:
        # yield ("delta",…)`가 실행돼 웹(onDelta)에 최종 텍스트가 표시된다(프로덕션 실 API 스트리밍과
        # 동일한 관찰가능 동작). tool_use 블록은 제외 → 도구 라운드는 안 흘리고 텍스트 라운드만.
        # (shim은 claude -p 블로킹 후 전체 텍스트를 한 번에 확보 → 한 델타로 방출.)
        for b in self._m.content:
            if getattr(b, "type", None) == "text" and b.text:
                yield b.text

    def get_final_message(self) -> _Message:
        return self._m


# ── 모델 별칭·직렬화 헬퍼 ─────────────────────────────────────────────────────

def _model_alias(model: str | None) -> str:
    """프로덕션 모델 id → claude -p --model 별칭(프로덕션 티어 매칭)."""
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "claude-sonnet-5"   # 프로덕션 CHAT_MODEL과 티어 일치. "sonnet" 별칭은 Sonnet 4.6로 풀려 프로덕션(Sonnet 5)과 어긋남(로컬 검증용 정합).


def _join_system(system) -> str:
    """system(블록 리스트 또는 문자열) → 단일 텍스트(--system-prompt-file용)."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for b in system:
        if isinstance(b, dict):
            parts.append(str(b.get("text", "")))
        else:
            parts.append(str(getattr(b, "text", b)))
    return "\n".join(parts)


def _block_text(block) -> str:
    if isinstance(block, dict):
        return str(block.get("text", ""))
    return str(getattr(block, "text", ""))


def _serialize_messages(messages: list) -> str:
    """Anthropic 와이어 messages → claude -p가 읽을 트랜스크립트 텍스트."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for blk in (content or []):
            t = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", None)
            if t == "text":
                lines.append(f"[{role}] {_block_text(blk)}")
            elif t == "tool_use":
                name = blk.get("name") if isinstance(blk, dict) else getattr(blk, "name", "")
                inp = blk.get("input") if isinstance(blk, dict) else getattr(blk, "input", {})
                lines.append(f"[{role}·도구호출 {name}] {json.dumps(inp, ensure_ascii=False)}")
            elif t == "tool_result":
                tc = blk.get("content") if isinstance(blk, dict) else getattr(blk, "content", "")
                lines.append(f"[도구결과] {tc}")
    return "\n".join(lines)


# ── 결정 프롬프트 구성 ────────────────────────────────────────────────────────

def _build_prompt(messages: list, tools, tool_choice) -> str:
    """(messages, tools, tool_choice) → claude -p stdin 프롬프트.

    세 모드:
      · tools 없음(뉴스): 대화만 — 모델이 평문으로 응답.
      · tool_choice 강제(NL→IR): 강제 도구 input을 JSON으로만 출력.
      · tools 자유(오케스트레이터): {action:tool|text} JSON으로만 출력.
    """
    convo = _serialize_messages(messages)
    if not tools:
        return convo   # 평문 응답(다이제스트 등) — system 지침이 형식을 결정

    forced = None
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        forced = tool_choice.get("name")

    schemas = json.dumps(
        [{"name": t.get("name"), "description": t.get("description"),
          "input_schema": t.get("input_schema")} for t in tools],
        ensure_ascii=False)

    if forced:
        return (f"<conversation>\n{convo}\n</conversation>\n\n"
                f"<tool_schema>\n{schemas}\n</tool_schema>\n\n"
                f"'{forced}'가 받을 input을 위 스키마에 맞춰 **JSON 객체 하나로만 평문 출력**하라. "
                f"도구를 실제로 호출하지 말 것 — 오직 JSON 텍스트만(설명·코드펜스 금지).")
    return (f"<conversation>\n{convo}\n</conversation>\n\n"
            f"<available_tools>\n{schemas}\n</available_tools>\n\n"
            f"위 system 지침에 따라 다음 행동 하나를 결정해 **JSON 객체 하나로만 평문 출력**하라. "
            f"도구를 실제로 호출하지 말 것 — 아래 형식의 JSON 텍스트만(설명·코드펜스 금지):\n"
            f'- 도구 사용: {{"action":"tool","name":"<도구명>","input":{{...}}}}\n'
            f'- 최종 답변: {{"action":"text","text":"<사용자에게 보일 답변>"}}')


# ── claude -p 호출·파싱 ───────────────────────────────────────────────────────

def _extract_json(text: str) -> dict | None:
    """모델 출력에서 첫 균형 JSON 객체 추출(코드펜스·앞뒤 텍스트 허용)."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    depth, start = 0, -1   # 중괄호 균형으로 첫 객체 스캔
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(s[start:i + 1])
                except (ValueError, TypeError):
                    start = -1
    return None


class _Messages:
    def __init__(self, backend: "ClaudeCodeBackend"):
        self._b = backend

    def create(self, *, model=None, system=None, messages=None, tools=None,
               tool_choice=None, max_tokens=4096, **_kw) -> _Message:
        return self._b._respond(model, system, messages or [], tools, tool_choice)

    def stream(self, *, model=None, system=None, messages=None, tools=None,
               tool_choice=None, max_tokens=4096, **_kw) -> _Stream:
        return _Stream(self._b._respond(model, system, messages or [], tools, tool_choice))


class ClaudeCodeBackend:
    """anthropic.Anthropic drop-in(필요한 표면만). 생성 kwargs(api_key 등)는 무시."""

    def __init__(self, **_kw):
        self.messages = _Messages(self)

    def _respond(self, model, system, messages, tools, tool_choice) -> _Message:
        sys_text = _join_system(system)
        if tools:                              # native tool-use 차단 오버라이드(위 상수 주석 참조)
            sys_text += _NOTOOL_ADDENDUM
        prompt = _build_prompt(messages, tools, tool_choice)
        result_text, usage = self._call(sys_text, prompt, _model_alias(model))

        if not tools:                                  # 평문(다이제스트)
            return _Message([_TextBlock(result_text)], "end_turn", usage)

        forced = (isinstance(tool_choice, dict) and tool_choice.get("type") == "tool"
                  and tool_choice.get("name")) or None
        if forced:                                     # NL→IR: 강제 도구 input
            payload = _extract_json(result_text) or {}
            return _Message([_ToolUseBlock(name=forced, input=payload)], "tool_use", usage)

        decision = _extract_json(result_text) or {}    # 오케스트레이터: action 분기
        if decision.get("action") == "tool" and decision.get("name"):
            return _Message([_ToolUseBlock(name=decision["name"],
                                           input=decision.get("input") or {})], "tool_use", usage)
        text = decision.get("text") if decision.get("action") == "text" else result_text
        return _Message([_TextBlock(str(text))], "end_turn", usage)

    def _call(self, sys_text: str, prompt: str, model: str) -> tuple[str, _Usage]:
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not token:
            raise RuntimeError(
                "CLAUDE_CODE_OAUTH_TOKEN 미설정 — `claude setup-token`으로 구독 토큰을 발급하고 "
                "환경변수로 설정하세요(설계서 §9).")
        # 실 anthropic SDK는 일시 오류를 기본 재시도한다. claude -p(구독 shim)도 공유 구독·CLI
        # 히컵으로 간헐 빈응답(is_error·빈 result·출력 파싱 실패)을 내므로, 동일 신뢰성을 갖도록
        # 일시 실패만 bounded 재시도한다. 타임아웃·토큰누락은 비재시도. dev 하니스 전용·프로덕션 무관.
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._call_once(sys_text, prompt, model, token)
            except _RetryableClaudeError as e:
                if attempt >= _MAX_RETRIES:
                    raise RuntimeError(
                        f"claude -p {_MAX_RETRIES + 1}회 시도 모두 실패 — {e}") from e
                time.sleep(_RETRY_BACKOFF_SEC * (attempt + 1))

    def _call_once(self, sys_text: str, prompt: str, model: str,
                   token: str) -> tuple[str, _Usage]:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(sys_text)
            sys_file = f.name
        try:
            # ANTHROPIC_API_KEY가 env에 있으면 claude -p가 OAuth보다 그걸 우선해 API모드로 간다
            # (하니스는 compile_nl 존재가드 통과용 더미 키를 쓴다) → 서브프로세스 env에선 제거.
            env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            # --max-turns 3: _NOTOOL_ADDENDUM에도 모델이 native tool_use를 시도하는 샘플이 있다
            # (동일 쿼리 실측: 1턴 순응 샘플과 '1턴째 도구시도(거부)→2턴째 JSON 자기교정' num_turns=2
            # 샘플 공존). 1이면 후자가 최종텍스트 없이 끝나 빈 result → 턴 실패 부류의 근본원인.
            proc = subprocess.Popen(
                [_CLAUDE_BIN, "-p", "--model", model, "--system-prompt-file", sys_file,
                 "--max-turns", "3", "--allowedTools", "", "--output-format", "json"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=env)
            try:
                out, errout = proc.communicate(input=prompt, timeout=_TIMEOUT)
            except subprocess.TimeoutExpired:
                # Windows: proc.kill()은 claude.CMD만 죽이고 손자(node)가 남아 communicate가
                # 무한블록(6h hang 근본원인) → taskkill /T로 프로세스 트리 전체 강제 종료.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True)
                else:
                    proc.kill()
                proc.wait()
                raise RuntimeError(f"claude -p 타임아웃({_TIMEOUT}s) — 프로세스 트리 종료함")
        finally:
            os.unlink(sys_file)
        try:
            env_out = json.loads(out)
        except (ValueError, TypeError):
            raise _RetryableClaudeError(
                f"출력 파싱 실패(rc={proc.returncode}): "
                f"stdout={out[:400]!r} / stderr={errout[:300]!r}")
        if env_out.get("is_error") or not str(env_out.get("result", "")).strip():
            # subtype·is_error·num_turns까지 남겨야 다음 발생 시 원인(도구시도 max_turns vs
            # CLI 오류 vs 사용량)을 로그만으로 판별할 수 있다.
            raise _RetryableClaudeError(
                f"빈/오류 응답(status={env_out.get('api_error_status')} "
                f"subtype={env_out.get('subtype')} is_error={env_out.get('is_error')} "
                f"num_turns={env_out.get('num_turns')}): "
                f"{env_out.get('result')} / stderr={errout[:200]!r}")
        u = env_out.get("usage") or {}
        usage = _Usage(
            input_tokens=u.get("input_tokens", 0) or 0,
            output_tokens=u.get("output_tokens", 0) or 0,
            cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=u.get("cache_read_input_tokens", 0) or 0)
        USAGE_LOG.append({"model": model, "in": usage.input_tokens, "out": usage.output_tokens,
                          "cache_create": usage.cache_creation_input_tokens,
                          "cache_read": usage.cache_read_input_tokens,
                          "cost_est": env_out.get("total_cost_usd", 0)})
        return str(env_out.get("result", "")), usage
