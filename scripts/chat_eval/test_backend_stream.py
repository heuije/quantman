"""shim 라이브 스트리밍(stream-json) — 델타 방출·JSON 결정 억제·재시도 경계 회귀 가드.

배경: 배치 claude -p는 완성까지 대기라 TTFT가 라운드당 수 분(침묵 UX의 물리 뿌리).
stream-json(+include-partial-messages·CLI 2.1.161 실측)으로 평문 최종답을 토큰 단위로
흘리고, 프롬프트형 프로토콜의 도구 결정 JSON은 화면에 새지 않게 메시지별 게이트로 억제한다.

subprocess를 가짜 NDJSON으로 대체 — claude 미호출·$0·결정적.
"""
import io
import json
import os
import sys
import time as _time_mod
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend  # noqa: E402


# ── 가짜 stream-json 프로세스 ────────────────────────────────────────────────

def _se_start():
    return json.dumps({"type": "stream_event", "event": {"type": "message_start"}})


def _se_delta(text):
    return json.dumps({"type": "stream_event",
                       "event": {"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": text}}})


def _result(result="OK", is_error=False):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                       "result": result, "num_turns": 1,
                       "usage": {"input_tokens": 3, "output_tokens": 7}})


class _FakeStdout:
    def __init__(self, lines):
        self._it = iter(lines)

    def __iter__(self):
        return self._it

    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = _FakeStdout([ln + "\n" for ln in lines])
        self.stderr = io.StringIO("")
        self.pid = 4321
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _fake_popen_factory(scripted):
    """scripted[i] = i번째 호출의 NDJSON 라인 리스트."""
    state = {"n": 0}

    def _popen(*_a, **_k):
        i = state["n"]
        state["n"] += 1
        return _FakeProc(scripted[min(i, len(scripted) - 1)])

    return _popen, state


@pytest.fixture(autouse=True)
def _authed_and_fast(monkeypatch):
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.setattr(_time_mod, "sleep", lambda *_a, **_k: None)


def _stream(tools=None, tool_choice=None):
    return backend.ClaudeCodeBackend().messages.stream(
        model="claude-sonnet-5", system="sys", messages=[{"role": "user", "content": "q"}],
        tools=tools, tool_choice=tool_choice)


TOOLS = [{"name": "simulate", "description": "d", "input_schema": {}}]


def test_prose_final_answer_streams_deltas(monkeypatch):
    """평문 최종답(신 프로토콜)은 토큰 델타가 실시간으로 흐르고 최종 메시지는 text 블록."""
    popen, state = _fake_popen_factory([
        [_se_start(), _se_delta("안"), _se_delta("녕하세요"), _result("안녕하세요")]])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    s = _stream(tools=TOOLS)
    assert list(s.text_stream) == ["안", "녕하세요"]
    m = s.get_final_message()
    assert m.content[0].type == "text" and m.content[0].text == "안녕하세요"
    assert state["n"] == 1


def test_tool_decision_json_is_suppressed_but_parsed(monkeypatch):
    """도구 결정 JSON은 화면에 한 글자도 안 새고, 최종 메시지는 tool_use 블록으로 파싱된다."""
    dec = '{"action":"tool","name":"simulate","input":{"nl":"코스닥 백테스트"}}'
    popen, _ = _fake_popen_factory([
        [_se_start(), _se_delta(dec[:10]), _se_delta(dec[10:]), _result(dec)]])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    s = _stream(tools=TOOLS)
    assert list(s.text_stream) == []
    m = s.get_final_message()
    assert m.content[0].type == "tool_use"
    assert m.content[0].name == "simulate"
    assert m.content[0].input == {"nl": "코스닥 백테스트"}


def test_gate_resets_per_message_no_json_leak(monkeypatch):
    """max-turns 자기교정: 앞 메시지가 평문이어도 뒤 메시지의 JSON은 화면에 새지 않는다."""
    dec = '{"action":"tool","name":"simulate","input":{}}'
    popen, _ = _fake_popen_factory([
        [_se_start(), _se_delta("분석하겠습니다"),
         _se_start(), _se_delta(dec), _result(dec)]])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    s = _stream(tools=TOOLS)
    assert list(s.text_stream) == ["분석하겠습니다"]
    assert s.get_final_message().content[0].type == "tool_use"


def test_retry_before_any_emission(monkeypatch):
    """빈/오류 봉투는 방출 전이면 재시도 — 두 번째 시도의 델타가 정상 스트림된다."""
    popen, state = _fake_popen_factory([
        [_se_start(), _result("", is_error=True)],
        [_se_start(), _se_delta("응답"), _result("응답")]])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    s = _stream(tools=TOOLS)
    assert list(s.text_stream) == ["응답"]
    assert state["n"] == 2


def test_no_retry_after_emission(monkeypatch):
    """델타가 이미 화면에 흘렀으면 재시도(중복 표출) 대신 정직하게 실패를 표면화한다."""
    popen, state = _fake_popen_factory([
        [_se_start(), _se_delta("이미 보임"), _result("", is_error=True)]])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    s = _stream(tools=TOOLS)
    with pytest.raises(RuntimeError, match="emitted=True"):
        list(s.text_stream)
    assert state["n"] == 1


def test_forced_tool_choice_parses_input(monkeypatch):
    """NL→IR형 강제 tool_choice: JSON 억제 + 강제 도구 input으로 파싱."""
    ir = '{"name":"전략","universe":{"kind":"all"}}'
    popen, _ = _fake_popen_factory([[_se_start(), _se_delta(ir), _result(ir)]])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    s = _stream(tools=TOOLS, tool_choice={"type": "tool", "name": "emit_strategy"})
    assert list(s.text_stream) == []
    m = s.get_final_message()
    assert m.content[0].type == "tool_use" and m.content[0].name == "emit_strategy"
    assert m.content[0].input["universe"] == {"kind": "all"}
