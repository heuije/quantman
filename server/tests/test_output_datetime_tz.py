"""구조적 회귀 — API 출력 datetime은 항상 UTC offset을 달고 직렬화돼야 한다.

근본 버그: 서버 DB(Postgres `timestamp without time zone`)는 모든 시각을 UTC로
저장하되 round-trip 시 tzinfo를 잃어 naive로 읽힌다. 이 naive datetime을 그대로
JSON으로 내보내면(`isoformat()`에 offset 없음) 브라우저 `new Date("...15:45:01")`가
naive ISO를 **현지시간(KST=UTC+9)으로 오해** → 정확히 9시간 과거로 밀린다.

증상: `/sync/snapshot`의 `last_heartbeat_at`가 9시간 전으로 표시돼 로컬앱이
살아있는데도 "끊김 — 자동매매 중단됨"이 뜸. (반면 `/trading/timeline`은 한 곳만
수동으로 `tzinfo=utc`를 붙여 정상 — 이 불일치가 모순을 만듦.)

구조적 수정: 공유 `UtcDateTime` 타입(naive를 UTC offset 붙여 직렬화)을 모든 출력
스키마 datetime 필드에 일괄 적용 → 부류 전체를 한 진입점에서 닫는다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.schemas import (  # noqa: E402
    CommandOut, DeviceOut, StrategyOut, StrategyStatsOut, StrategyVersionOut,
    SyncSnapshotOut, UserOut,
)

# DB가 round-trip으로 돌려주는 모습 — naive UTC.
_NAIVE = datetime(2026, 6, 9, 15, 45, 1, 492091)
_AWARE = datetime(2026, 6, 9, 15, 45, 1, 492091, tzinfo=timezone.utc)


def _assert_utc_iso(s: str) -> None:
    """직렬화 결과가 UTC offset을 단 ISO 문자열인지 — 브라우저가 UTC로 파싱 가능."""
    assert isinstance(s, str), s
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is not None, f"offset 없음(naive): {s!r}"
    assert parsed.utcoffset() == timezone.utc.utcoffset(None), f"UTC 아님: {s!r}"
    # 시각 자체는 보존(타임존만 부여, 값 이동 금지).
    assert parsed.replace(tzinfo=None) == _NAIVE


def test_snapshot_naive_datetimes_serialized_as_utc():
    out = SyncSnapshotOut(payload={}, received_at=_NAIVE,
                          last_heartbeat_at=_NAIVE, device_id=6)
    j = out.model_dump(mode="json")
    _assert_utc_iso(j["received_at"])
    _assert_utc_iso(j["last_heartbeat_at"])


def test_snapshot_aware_datetime_preserved():
    out = SyncSnapshotOut(payload={}, received_at=_AWARE, last_heartbeat_at=_AWARE)
    j = out.model_dump(mode="json")
    _assert_utc_iso(j["received_at"])
    _assert_utc_iso(j["last_heartbeat_at"])


def test_device_out_datetimes_utc():
    j = DeviceOut(id=1, name="d", created_at=_NAIVE, last_seen_at=_NAIVE).model_dump(mode="json")
    _assert_utc_iso(j["created_at"])
    _assert_utc_iso(j["last_seen_at"])


def test_command_out_datetimes_utc():
    j = CommandOut(id=1, device_id=6, type="LIQUIDATE_ALL", params={},
                   status="pending", created_at=_NAIVE, delivered_at=_NAIVE,
                   completed_at=_NAIVE, result={}).model_dump(mode="json")
    _assert_utc_iso(j["created_at"])
    _assert_utc_iso(j["delivered_at"])
    _assert_utc_iso(j["completed_at"])


def test_user_and_strategy_datetimes_utc():
    _assert_utc_iso(UserOut(id=1, email="t@e.com", created_at=_NAIVE)
                    .model_dump(mode="json")["created_at"])
    sj = StrategyOut(id=1, name="s", run_mode="paper", definition={},
                     created_at=_NAIVE, updated_at=_NAIVE,
                     paper_started_at=_NAIVE).model_dump(mode="json")
    _assert_utc_iso(sj["created_at"])
    _assert_utc_iso(sj["updated_at"])
    _assert_utc_iso(sj["paper_started_at"])
    _assert_utc_iso(StrategyVersionOut(version_no=1, name="s", created_at=_NAIVE,
                                       created_reason="initial")
                    .model_dump(mode="json")["created_at"])
    _assert_utc_iso(StrategyStatsOut(last_snapshot_at=_NAIVE)
                    .model_dump(mode="json")["last_snapshot_at"])


def test_optional_datetime_none_stays_none():
    """None은 그대로 None(직렬화 오류 없이 통과)."""
    j = SyncSnapshotOut(payload={}, received_at=_NAIVE, last_heartbeat_at=None).model_dump(mode="json")
    assert j["last_heartbeat_at"] is None


# ── 엔드포인트 통합 — 실제 FastAPI 직렬화 경로(response_model)로 확인 ───────────

def test_sync_snapshot_endpoint_emits_utc_offset():
    """`/sync/snapshot` HTTP 응답의 received_at·last_heartbeat_at이 UTC offset을 단다.

    DB round-trip naive를 재현: SyncSnapshot.received_at·UserSettings.last_heartbeat_at을
    naive로 직접 세팅 → 엔드포인트 응답 JSON이 offset 포함인지(브라우저가 -9h로
    오해하지 않는지) 검증. 이 통합 테스트가 라이브에서 본 "끊김 9시간 전" 회귀를 막는다.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.db import get_session
    from app.models import Device, SyncSnapshot, User, UserSettings
    from app.routers import sync as sync_router
    from app.security import create_access_token

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com", created_at=_NAIVE)
        s.add(user); s.commit(); s.refresh(user)
        s.add(Device(user_id=user.id, name="dev", token_hash="h"))
        # naive UTC — Postgres round-trip 재현
        s.add(SyncSnapshot(user_id=user.id, device_id=1, payload={"balance": {}},
                           received_at=_NAIVE))
        s.add(UserSettings(user_id=user.id, last_heartbeat_at=_NAIVE))
        s.commit()
        uid = user.id

    app = FastAPI()
    app.include_router(sync_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    client = TestClient(app)

    r = client.get("/sync/snapshot",
                   headers={"Authorization": f"Bearer {create_access_token(uid)}"})
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_utc_iso(body["received_at"])
    _assert_utc_iso(body["last_heartbeat_at"])
