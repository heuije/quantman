"""shim `_call` 재시도 로직 테스트.

claude -p(구독 shim)는 공유 구독·CLI 히컵으로 간헐 빈응답(is_error·빈 result·출력
파싱 실패)을 낸다. 실 anthropic SDK는 일시 오류를 기본 재시도하므로, shim도 동일
신뢰성을 갖도록 bounded 재시도해야 한다(dev 하니스 전용·프로덕션 무관).

subprocess를 가짜로 대체 → claude -p 미호출·API $0·결정적.
"""
import json
import os
import sys
import time as _time_mod
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend  # noqa: E402


def _envelope(*, is_error=False, result="OK", status=None):
    d = {"is_error": is_error, "result": result,
         "usage": {"input_tokens": 1, "output_tokens": 2}}
    if status is not None:
        d["api_error_status"] = status
    return json.dumps(d)


class _FakeProc:
    def __init__(self, stdout):
        self._stdout = stdout
        self.returncode = 0
        self.pid = 4321

    def communicate(self, input=None, timeout=None):
        return self._stdout, ""


def _fake_popen_factory(scripted):
    """scripted[i] = i번째 claude -p 호출이 낼 stdout(JSON). 이후는 마지막 반복."""
    state = {"n": 0}

    def _popen(*_a, **_k):
        i = state["n"]
        state["n"] += 1
        return _FakeProc(scripted[min(i, len(scripted) - 1)])

    return _popen, state


@pytest.fixture(autouse=True)
def _authed_and_fast(monkeypatch):
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.setattr(_time_mod, "sleep", lambda *_a, **_k: None)   # 백오프 즉시


def test_retries_then_succeeds(monkeypatch):
    """1회 빈응답(is_error) 후 성공하면 성공 결과를 반환한다."""
    popen, state = _fake_popen_factory(
        [_envelope(is_error=True, result=None), _envelope(result="OK")])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    text, _usage = backend.ClaudeCodeBackend()._call("sys", "prompt", "claude-sonnet-5")
    assert text == "OK"
    assert state["n"] == 2   # 1 실패 + 1 성공


def test_gives_up_after_three_attempts(monkeypatch):
    """계속 실패하면 3회(초기 1 + 재시도 2) 시도 후 포기(raise)한다."""
    popen, state = _fake_popen_factory([_envelope(is_error=True, result=None)])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError):
        backend.ClaudeCodeBackend()._call("sys", "prompt", "claude-sonnet-5")
    assert state["n"] == 3


def test_empty_result_is_retried(monkeypatch):
    """is_error=false여도 result가 비면 일시 실패로 보고 재시도한다."""
    popen, state = _fake_popen_factory([_envelope(result=""), _envelope(result="OK")])
    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    text, _usage = backend.ClaudeCodeBackend()._call("sys", "prompt", "claude-sonnet-5")
    assert text == "OK"
    assert state["n"] == 2


def test_max_turns_allows_tool_attempt_self_correction(monkeypatch):
    """모델이 addendum에도 native tool_use를 시도하는 샘플(실측 num_turns=2)이 거부 피드백 후
    자기교정할 수 있도록 --max-turns는 2 이상이어야 한다(1이면 빈 result로 턴 실패)."""
    captured = {}

    def _popen(cmd, **_k):
        captured["cmd"] = cmd
        return _FakeProc(_envelope(result="OK"))

    monkeypatch.setattr(backend.subprocess, "Popen", _popen)
    backend.ClaudeCodeBackend()._call("sys", "prompt", "claude-sonnet-5")
    i = captured["cmd"].index("--max-turns")
    assert int(captured["cmd"][i + 1]) >= 2


def test_effort_flag_passed(monkeypatch):
    """claude -p에 --effort가 명시 전달돼야 한다(추론예산 레버·상수 _EFFORT 주석 참조). 누락되면
    CLI 기본 추론예산으로 새어나가 $0에서 무거운 쿼리가 안 보이는 thinking으로 수분 침묵한다."""
    captured = {}

    def _popen(cmd, **_k):
        captured["cmd"] = cmd
        return _FakeProc(_envelope(result="OK"))

    monkeypatch.setattr(backend.subprocess, "Popen", _popen)
    backend.ClaudeCodeBackend()._call("sys", "prompt", "claude-sonnet-5")
    assert "--effort" in captured["cmd"]
    val = captured["cmd"][captured["cmd"].index("--effort") + 1]
    assert val == backend._EFFORT
    assert val in {"low", "medium", "high", "xhigh", "max"}
