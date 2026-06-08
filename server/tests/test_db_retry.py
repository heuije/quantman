"""Phase 60 — Neon 서버리스 연결 끊김 재시도(retry-on-disconnect) 단위 검증.

`server conn crashed?`(Neon suspend가 끊은 연결로의 쿼리)가 pre_ping을 통과해도
발생하던 부류를 닫는 헬퍼: 끊김 감지 시 pool 폐기 후 fresh 연결로 1회 재시도.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import db


def test_is_disconnect_matches_neon_message():
    assert db.is_disconnect(Exception("server conn crashed?")) is True
    assert db.is_disconnect(Exception("ProtocolViolation: server conn crashed?")) is True
    assert db.is_disconnect(ValueError("그냥 다른 에러")) is False


def test_is_disconnect_matches_invalidated_flag():
    class FakeDBAPIError(db.DBAPIError):
        def __init__(self):
            self.connection_invalidated = True
    # DBAPIError 서브클래스 + connection_invalidated=True → 끊김으로 판별
    e = FakeDBAPIError.__new__(FakeDBAPIError)
    e.connection_invalidated = True
    assert db.is_disconnect(e) is True


def test_retry_recovers_after_one_disconnect(monkeypatch):
    """첫 호출이 끊김 → pool 폐기 후 재시도 → 두 번째 성공."""
    disposed = {"n": 0}
    monkeypatch.setattr(db.engine, "dispose", lambda: disposed.__setitem__("n", disposed["n"] + 1))
    calls = {"n": 0}

    def fn(x):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("server conn crashed?")
        return x * 2

    assert db.call_with_disconnect_retry(fn, 21) == 42
    assert calls["n"] == 2          # 1회 재시도됨
    assert disposed["n"] == 1       # 재시도 전 pool 폐기


def test_retry_propagates_non_disconnect(monkeypatch):
    """끊김이 아닌 예외는 재시도 없이 즉시 전파."""
    monkeypatch.setattr(db.engine, "dispose", lambda: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("로직 에러")

    with pytest.raises(ValueError):
        db.call_with_disconnect_retry(fn)
    assert calls["n"] == 1          # 재시도 안 함


def test_retry_gives_up_after_one(monkeypatch):
    """끊김이 연속(재시도도 끊김)이면 1회만 재시도 후 전파(무한루프 방지)."""
    monkeypatch.setattr(db.engine, "dispose", lambda: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise Exception("server conn crashed?")

    with pytest.raises(Exception, match="server conn crashed"):
        db.call_with_disconnect_retry(fn)
    assert calls["n"] == 2          # 최초 1 + 재시도 1
