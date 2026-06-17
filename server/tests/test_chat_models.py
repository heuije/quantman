from app.config import settings
from sqlmodel import Session, SQLModel, create_engine
from app.models import Conversation, Message


def test_chat_model_default():
    # env 미설정 시 Sonnet 기본. agentic 추론·논의 부담이 NL 컴파일러보다 커 상향.
    assert settings.CHAT_MODEL  # 비어있지 않음
    assert "claude" in settings.CHAT_MODEL


def _mem_session() -> Session:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def test_conversation_and_message_roundtrip():
    s = _mem_session()
    conv = Conversation(user_id=1, title="테스트 대화")
    s.add(conv)
    s.commit()
    s.refresh(conv)
    assert conv.id is not None

    parts = [{"type": "text", "text": "안녕"},
             {"type": "tool_result", "tool_use_id": "t1", "name": "screen",
              "result": {"success": True, "results": [{"symbol": "005930", "score": 0.8}]}}]
    msg = Message(conversation_id=conv.id, role="assistant", parts=parts)
    s.add(msg)
    s.commit()
    s.refresh(msg)

    got = s.get(Message, msg.id)
    assert got.role == "assistant"
    assert got.parts[1]["result"]["results"][0]["symbol"] == "005930"  # JSON 왕복
