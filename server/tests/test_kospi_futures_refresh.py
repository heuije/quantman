"""KOSPI200 선물 데이터 갱신 — KRX 만기물 패널 경로 배선 검증(S4).

_refresh_kospi_futures가 KRX_API_KEY 미설정이면 no-op, 설정이면 fetch_futures_panel로
최근 윈도우를 수집·캐시 무효화하는지. 패널→연속물 stitch 로직 자체는 core
test_krx_openapi(fetch_futures_panel_saves_and_rebuilds)·test_futures_roll이 커버.

    cd platform/server && python -m pytest tests/test_kospi_futures_refresh.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
# 이 워크트리의 core를 최우선 — 다른 워크트리(editable install)의 quant_core가 잡히는 것 방지.
_CORE_DIR = _SERVER_DIR.parent / "core"
for _p in (str(_CORE_DIR), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_refresh_noop_without_key(monkeypatch):
    from app import main as m
    from quant_core.data.feeds import krx_openapi

    monkeypatch.setattr(krx_openapi, "is_active", lambda: False)
    called = []
    monkeypatch.setattr(krx_openapi, "fetch_futures_panel", lambda *a, **k: called.append(a))

    m._refresh_kospi_futures()                          # 키 미설정 → no-op(선물분석 CSV 무관)
    assert not called


def test_refresh_fetches_panel_when_active(monkeypatch):
    from app import main as m
    from quant_core.data.feeds import krx_openapi

    monkeypatch.setattr(krx_openapi, "is_active", lambda: True)
    calls = []
    monkeypatch.setattr(krx_openapi, "fetch_futures_panel",
                        lambda s, e: calls.append((s, e)) or {"ok": True, "saved": {"패널행": 7, "연속물": 3}})
    invalidated = []
    monkeypatch.setattr(m.data_cache, "invalidate", lambda: invalidated.append(True))

    m._refresh_kospi_futures()

    assert len(calls) == 1                              # fetch_futures_panel(최근 윈도우) 1회
    s, e = calls[0]
    assert len(s) == 8 and len(e) == 8 and s < e        # YYYYMMDD 최근 윈도우
    assert invalidated                                 # 캐시 무효화
