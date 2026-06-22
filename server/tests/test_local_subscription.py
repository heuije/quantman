"""로컬 구독 모드(Mode B) seam — QP_CHAT_LOCAL_SUBSCRIPTION 플래그가 챗 기본 클라이언트
해석을 claude -p 구독 백엔드로 바꾸고, 미설정 시 프로덕션 경로(Anthropic API 키)가 변하지
않음을 확인한다. (실제 claude -p 호출 없음 — 클라이언트 구성만 검증·$0.)
"""
import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from app.chat import agent


def test_default_client_off_uses_api_key(monkeypatch):
    """플래그 미설정 → anthropic.Anthropic(api_key=...) 경로(프로덕션 동작 무변경)."""
    monkeypatch.delenv("QP_CHAT_LOCAL_SUBSCRIPTION", raising=False)
    import anthropic
    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    client = agent._default_chat_client()
    assert isinstance(client, _FakeAnthropic)      # API 경로 사용(구독 백엔드 아님)
    assert "api_key" in captured                   # 키로 생성


def test_default_client_on_uses_subscription(monkeypatch):
    """플래그=1 → ClaudeCodeBackend 반환 + anthropic.Anthropic 몽키패치(compile_nl·뉴스도 $0)."""
    monkeypatch.setenv("QP_CHAT_LOCAL_SUBSCRIPTION", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-dummy")   # 구성만(호출 없음)
    import anthropic
    orig = anthropic.Anthropic
    try:
        client = agent._default_chat_client()
        from backend import ClaudeCodeBackend          # _load_cc_backend가 sys.path 주입
        assert isinstance(client, ClaudeCodeBackend)     # 오케스트레이터용 구독 백엔드
        assert anthropic.Anthropic is ClaudeCodeBackend  # compile_nl·뉴스도 구독 경유
    finally:
        anthropic.Anthropic = orig                       # 몽키패치 복원(다른 테스트 보호)


def test_chat_llm_guard_allows_subscription_mode(monkeypatch):
    """503 가드: API 키 없어도 로컬 구독 모드면 통과(Mode B), 둘 다 없으면 503."""
    from fastapi import HTTPException

    from app.routers import chat as chat_router

    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.delenv("QP_CHAT_LOCAL_SUBSCRIPTION", raising=False)
    with pytest.raises(HTTPException) as ei:              # 키·플래그 둘 다 없음 → 503
        chat_router._require_chat_llm()
    assert ei.value.status_code == 503
    monkeypatch.setenv("QP_CHAT_LOCAL_SUBSCRIPTION", "1")
    chat_router._require_chat_llm()                       # 구독 모드 → 통과(예외 없음)
