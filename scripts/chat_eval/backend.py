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
import threading
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
    "위에서 도구 호출(emit_strategy·screen·simulate 등)을 지시하더라도 도구 호출 메커니즘을 쓰지 마라. "
    "도구가 필요한 결정이면 **요청된 JSON 객체 하나만 평문 텍스트로**(코드펜스·설명·여는 말 금지) 출력하고, "
    "도구가 필요 없는 **최종 사용자 답변이면 JSON으로 감싸지 말고 답변 본문만 평문으로** 출력하라. "
    "(평문 최종답은 실시간 스트리밍으로 사용자에게 그대로 표시된다.)")

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
    """`with client.messages.stream(...) as s:` — **라이브 스트리밍**(stream-json).

    text_stream이 claude -p 서브프로세스를 소진하며 **평문(최종답) 델타를 실시간으로**
    흘린다 — 프로덕션 실 API 스트리밍과 같은 체감(TTFT 수 초). 도구 결정은 프롬프트형
    프로토콜상 JSON 텍스트라 화면에 흘리면 안 되므로, 첫 비공백 문자가 '{'인 메시지는
    억제하고 파싱만 한다(진행 표시는 agent의 progress 이벤트가 담당). 소진이 끝나면
    get_final_message()가 배치 경로와 동일한 파싱으로 _Message를 만든다."""

    def __init__(self, backend: "ClaudeCodeBackend", sys_text: str, prompt: str,
                 model: str, tools, tool_choice):
        self._b = backend
        self._args = (sys_text, prompt, model, tools, tool_choice)
        self._final: _Message | None = None
        self._gen = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        if self._gen is None:                 # 단일 제너레이터 캐시 — 반복 접근에도 1회 실행
            self._gen = self._b._stream_deltas(self)
        return self._gen

    def get_final_message(self) -> _Message:
        for _ in self.text_stream:            # 미소진분 드레인(소비자가 안 읽어도 완결 보장)
            pass
        return self._final


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
            f"위 system 지침에 따라 다음 중 하나로만 응답하라. 도구를 실제로 호출하지 말 것:\n"
            f'- 도구 사용 결정: {{"action":"tool","name":"<도구명>","input":{{...}}}} '
            f"— 이 JSON 객체 하나만(설명·코드펜스 금지)\n"
            f"- 최종 답변: JSON으로 감싸지 말고 사용자에게 보일 답변 본문만 평문으로 "
            f"(실시간 스트리밍으로 그대로 표시된다)")


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
        # 라이브 스트리밍 경로(오케스트레이터 루프) — create()(NL→IR 컴파일·다이제스트)는
        # 검증된 배치 경로 유지. 델타는 _Stream.text_stream 소비 시점에 흐른다.
        sys_text = _join_system(system)
        if tools:
            sys_text += _NOTOOL_ADDENDUM
        prompt = _build_prompt(messages or [], tools, tool_choice)
        return _Stream(self._b, sys_text, prompt, _model_alias(model), tools, tool_choice)


def _finalize_message(result_text: str, usage: _Usage, tools, tool_choice) -> _Message:
    """claude -p 최종 텍스트 → SDK _Message. 배치·스트리밍 공용 파싱(단일 출처)."""
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
    # 평문 최종답(신 프로토콜·스트리밍용) 또는 {"action":"text"} JSON(구 프로토콜) 모두 수용
    text = decision.get("text") if decision.get("action") == "text" else result_text
    return _Message([_TextBlock(str(text))], "end_turn", usage)


def _claude_env(token: str) -> dict:
    # ANTHROPIC_API_KEY가 env에 있으면 claude -p가 OAuth보다 그걸 우선해 API모드로 간다
    # (하니스는 compile_nl 존재가드 통과용 더미 키를 쓴다) → 서브프로세스 env에선 제거.
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _kill_tree(proc) -> None:
    # Windows: proc.kill()은 claude.CMD만 죽이고 손자(node)가 남아 대기가 무한블록
    # (6h hang 근본원인) → taskkill /T로 프로세스 트리 전체 강제 종료.
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.kill()
    proc.wait()


def _check_envelope(env_out: dict, errout: str, model: str) -> tuple[str, _Usage]:
    """result 봉투 공통 검사(배치 json·스트리밍 stream-json 동일 봉투) + usage 회계."""
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
        return _finalize_message(result_text, usage, tools, tool_choice)

    # ── 배치 경로 (create — NL→IR 컴파일·다이제스트) ─────────────────────────────

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
            # --max-turns 3: _NOTOOL_ADDENDUM에도 모델이 native tool_use를 시도하는 샘플이 있다
            # (동일 쿼리 실측: 1턴 순응 샘플과 '1턴째 도구시도(거부)→2턴째 JSON 자기교정' num_turns=2
            # 샘플 공존). 1이면 후자가 최종텍스트 없이 끝나 빈 result → 턴 실패 부류의 근본원인.
            proc = subprocess.Popen(
                [_CLAUDE_BIN, "-p", "--model", model, "--system-prompt-file", sys_file,
                 "--max-turns", "3", "--allowedTools", "", "--output-format", "json"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=_claude_env(token))
            try:
                out, errout = proc.communicate(input=prompt, timeout=_TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(f"claude -p 타임아웃({_TIMEOUT}s) — 프로세스 트리 종료함")
        finally:
            os.unlink(sys_file)
        try:
            env_out = json.loads(out)
        except (ValueError, TypeError):
            raise _RetryableClaudeError(
                f"출력 파싱 실패(rc={proc.returncode}): "
                f"stdout={out[:400]!r} / stderr={errout[:300]!r}")
        return _check_envelope(env_out, errout, model)

    # ── 스트리밍 경로 (stream — 오케스트레이터 루프·TTFT 수 초) ──────────────────

    def _stream_deltas(self, s: _Stream):
        """_Stream.text_stream 본체 — 평문 델타를 yield하고 완료 시 s._final을 세팅.

        재시도는 **아무것도 방출하기 전**에만 — 델타가 이미 화면에 흘렀는데 재시도하면
        같은 답이 중복 표출된다. 방출 후 실패는 정직하게 표면화(드묾)."""
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not token:
            raise RuntimeError(
                "CLAUDE_CODE_OAUTH_TOKEN 미설정 — `claude setup-token`으로 구독 토큰을 발급하고 "
                "환경변수로 설정하세요(설계서 §9).")
        sys_text, prompt, model, tools, tool_choice = s._args
        emitted = False
        for attempt in range(_MAX_RETRIES + 1):
            try:
                for kind, val in self._stream_once(sys_text, prompt, model, token):
                    if kind == "delta":
                        emitted = True
                        yield val
                    else:                       # ("final", (result_text, usage))
                        s._final = _finalize_message(val[0], val[1], tools, tool_choice)
                return
            except _RetryableClaudeError as e:
                if emitted or attempt >= _MAX_RETRIES:
                    raise RuntimeError(
                        f"claude -p 스트리밍 실패({attempt + 1}회차·emitted={emitted}) — {e}") from e
                time.sleep(_RETRY_BACKOFF_SEC * (attempt + 1))

    def _stream_once(self, sys_text: str, prompt: str, model: str, token: str):
        """claude -p stream-json 1회 실행 — ("delta", str)… 후 ("final", (text, usage)).

        프로토콜(CLI 2.1.161 실측·probe): stream_event/content_block_delta(text_delta)가
        토큰 단위로 오고, 마지막 result 이벤트가 배치 --output-format json과 **동일 봉투**
        (is_error·result·usage·subtype·num_turns)를 준다 → 봉투 검사·재시도 분류를 배치와
        공유(_check_envelope). 게이트: assistant 메시지별 첫 비공백 문자가 '{'면 도구 결정
        JSON → 화면 미방출(파싱은 봉투 result로). max-turns 자기교정으로 한 호출에 여러
        메시지가 올 수 있어 message_start마다 게이트를 리셋한다 — 앞 턴이 평문이어도 뒤 턴
        JSON이 화면에 새지 않는다. 도구시도의 input_json_delta·thinking 델타는 text가 없어
        자연 무시된다."""
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(sys_text)
            sys_file = f.name
        killed = {"v": False}
        stderr_tail: list[str] = []
        envelope = None
        try:
            proc = subprocess.Popen(
                [_CLAUDE_BIN, "-p", "--model", model, "--system-prompt-file", sys_file,
                 "--max-turns", "3", "--allowedTools", "",
                 "--output-format", "stream-json", "--include-partial-messages", "--verbose"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=_claude_env(token))

            def _on_timeout():
                killed["v"] = True
                _kill_tree(proc)

            watchdog = threading.Timer(_TIMEOUT, _on_timeout)   # 스트림 hang 보호(배치와 동일 예산)
            watchdog.start()

            def _drain_stderr():
                # stderr 미소비로 파이프 버퍼가 차면 stdout 읽기가 교착한다 — 꼬리만 보존(진단용).
                for ln in proc.stderr:
                    stderr_tail.append(ln)
                    del stderr_tail[:-20]

            threading.Thread(target=_drain_stderr, daemon=True).start()
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
                mode = None          # 메시지별 게이트: None(미분류) → "prose" | "json"
                buf = ""
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    t = ev.get("type")
                    if t == "stream_event":
                        inner = ev.get("event") or {}
                        it = inner.get("type")
                        if it == "message_start":
                            mode, buf = None, ""
                        elif it == "content_block_delta":
                            txt = (inner.get("delta") or {}).get("text") or ""
                            if not txt:
                                continue
                            if mode is None:
                                buf += txt
                                stripped = buf.lstrip()
                                if not stripped:
                                    continue
                                mode = "json" if stripped[0] == "{" else "prose"
                                if mode == "prose":
                                    yield ("delta", buf)
                                    buf = ""
                            elif mode == "prose":
                                yield ("delta", txt)
                            # mode == "json": 도구 결정 — 화면 미방출
                    elif t == "result":
                        envelope = ev
                proc.wait()
            finally:
                watchdog.cancel()
        finally:
            os.unlink(sys_file)
        if killed["v"]:
            raise RuntimeError(f"claude -p 타임아웃({_TIMEOUT}s) — 프로세스 트리 종료함")
        errout = "".join(stderr_tail)
        if envelope is None:
            raise _RetryableClaudeError(
                f"stream 종료·result 봉투 없음(rc={proc.returncode}) / stderr={errout[:300]!r}")
        yield ("final", _check_envelope(envelope, errout, model))
