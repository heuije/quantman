"""자동매매 건강 CLI(health_cli) — 출력·정렬·필터 단위 테스트.

compute_user_health(이미 test_health_monitor에서 검증)를 monkeypatch로 대체해 CLI의
포매팅·필터 로직만 hermetic 검증(DB 불요).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import health_cli

_ROWS = [
    {"user_id": 73, "email": "mwmw@x.com", "live_strategies": 2,
     "snapshot_at": "2026-07-13T06:50:00", "heartbeat_at": None, "overall": "red",
     "conditions": {"runtime": {"status": "green", "detail": "ok"},
                    "data_freshness": {"status": "red", "detail": "데이터 로드율 1/129"}},
     "alert_reasons": [{"code": "no_orders_data_starved", "message": "데이터 결손"}]},
    {"user_id": 1, "email": "ok@x.com", "live_strategies": 0, "snapshot_at": None,
     "heartbeat_at": None, "overall": "green", "conditions": {}, "alert_reasons": []},
]


def test_cli_prints_red_and_alert_and_detail(monkeypatch, capsys):
    monkeypatch.setattr(health_cli, "compute_user_health", lambda s: _ROWS)
    health_cli.main_cli([])
    out = capsys.readouterr().out
    assert "mwmw@x.com" in out and "RED" in out
    assert "no_orders_data_starved" in out          # alert 코드 표시
    assert "데이터 로드율" in out                     # RED 조건 상세 자동 노출


def test_cli_red_only_filters_green(monkeypatch, capsys):
    monkeypatch.setattr(health_cli, "compute_user_health", lambda s: _ROWS)
    health_cli.main_cli(["--red-only"])
    out = capsys.readouterr().out
    assert "mwmw@x.com" in out and "ok@x.com" not in out


def test_cli_user_filter(monkeypatch, capsys):
    monkeypatch.setattr(health_cli, "compute_user_health", lambda s: _ROWS)
    health_cli.main_cli(["--user", "1"])
    out = capsys.readouterr().out
    assert "ok@x.com" in out and "mwmw@x.com" not in out


def test_cli_json_is_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(health_cli, "compute_user_health", lambda s: _ROWS)
    health_cli.main_cli(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2 and data[0]["email"] == "mwmw@x.com"


def test_cli_surfaces_recent_errors_and_content_age(monkeypatch, capsys):
    """F1: 원시 recent_errors(예: CON.parquet)와 content_at(직전 cycle 기준)을 CLI가 노출한다.
    이번 CON 오진의 직접 근본수정 — 증거가 있어도 CLI가 안 보여주던 문제."""
    rows = [{
        "user_id": 73, "email": "mwmw@x.com", "live_strategies": 2,
        "snapshot_at": "2026-07-13T13:35:00", "content_at": "2026-07-13T13:12:00",
        "heartbeat_at": None, "overall": "amber",
        "conditions": {"data_freshness": {"status": "green", "detail": "데이터 로드 136/136"}},
        "alert_reasons": [],
        # CON 에러가 목록의 '가장 오래된' 위치 — truncate하면 놓친다(이번 오진 회귀 방지).
        "recent_errors": (
            ["2026-07-13 08:56 WARNING [WinError 6] ...: 'CON.parquet'"]
            + [f"2026-07-13 1{h}:00 WARNING preview 502" for h in range(9)]),
        "bundle": {"result": "ok", "n_files": 24047, "n_failed": 0},
        "dataset": {"needed": 136, "loaded": 136},
    }]
    monkeypatch.setattr(health_cli, "compute_user_health", lambda s: rows)
    health_cli.main_cli(["--user", "73"])
    out = capsys.readouterr().out
    assert "CON.parquet" in out            # 가장 오래된 에러도 노출(truncate 안 함) — 핵심
    assert "최근 에러 10건" in out
    assert "content=2026-07-13T13:12" in out   # 직전 cycle 기준 투명화
    assert "24047" in out                   # 번들 요약
