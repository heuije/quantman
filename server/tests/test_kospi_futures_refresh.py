"""KRX 선물(K200·KQ150) 아침 공급 경로 배선 검증(S4 + 파이프라인 Phase 3).

_refresh_krx_futures: 키 미설정 no-op / KR 휴장 skip(문제5) / fetch 후 **신선도 판정**
("직전 거래일 봉 수신" — saved 총행수는 신호가 아님·문제3·4) — stale이면 raise(백오프
재시도 유도) / fresh면 trading 번들 재포장(문제1·2)+preview(실패 전파·문제8).
패널→연속물 stitch 자체는 core test_krx_openapi·test_futures_roll이 커버.

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

    m._refresh_krx_futures()                            # 키 미설정 → no-op(선물분석 CSV 무관)
    assert not called


def _patch_morning(monkeypatch, m, *, last_bar="2026-07-14",
                   prev_session="2026-07-14", session=True):
    """아침 공급 경로 더블 — 캘린더·저장 패널·번들·preview를 기록 치환."""
    import datetime as dt

    import pandas as pd
    from quant_core import data_fetcher as df_mod
    from quant_core import market_calendar as mc

    monkeypatch.setattr(mc, "is_session_day", lambda mkt, d: session)
    monkeypatch.setattr(mc, "prev_session_day",
                        lambda mkt, d: dt.date.fromisoformat(prev_session))
    monkeypatch.setattr(df_mod, "load_futures_panel", lambda sym: pd.DataFrame(
        {"Close": [1.0]}, index=pd.to_datetime([last_bar])))
    packaged: list = []
    previewed: list = []
    monkeypatch.setattr(m, "_package_bundle",
                        lambda include_full=True: packaged.append(include_full))
    monkeypatch.setattr(m, "_trigger_preview", lambda src: previewed.append(src))
    return packaged, previewed


def test_refresh_fetches_panel_when_active(monkeypatch):
    from app import main as m
    from quant_core.data.feeds import krx_openapi

    monkeypatch.setattr(krx_openapi, "is_active", lambda: True)
    calls = []
    monkeypatch.setattr(krx_openapi, "fetch_futures_panel",
                        lambda s, e: calls.append((s, e)) or
                        {"ok": True, "saved": {"코스피200선물": {"패널행": 7, "연속물": 3}}})
    invalidated = []
    monkeypatch.setattr(m.data_cache, "invalidate", lambda: invalidated.append(True))
    packaged, previewed = _patch_morning(monkeypatch, m)

    m._refresh_krx_futures()

    assert len(calls) == 1                              # fetch_futures_panel(최근 윈도우) 1회
    s, e = calls[0]
    assert len(s) == 8 and len(e) == 8 and s < e        # YYYYMMDD 최근 윈도우
    assert invalidated                                 # 캐시 무효화
    # 문제 1·2 근본수정 — fresh 확인 후 trading 번들 재포장(+preview 같은 상태 갱신).
    assert packaged == [False], "아침 경로는 trading만(include_full=False)"
    assert previewed == ["kospi_futures"]


def test_refresh_skips_on_kr_holiday(monkeypatch):
    """문제 5 — KR 휴장일엔 fetch 자체를 skip(지연 오판·재시도 낭비 방지)."""
    from app import main as m
    from quant_core.data.feeds import krx_openapi

    monkeypatch.setattr(krx_openapi, "is_active", lambda: True)
    calls = []
    monkeypatch.setattr(krx_openapi, "fetch_futures_panel",
                        lambda *a, **k: calls.append(a))
    packaged, previewed = _patch_morning(monkeypatch, m, session=False)

    m._refresh_krx_futures()
    assert not calls and not packaged and not previewed


def test_refresh_raises_when_prev_session_bar_missing(monkeypatch):
    """문제 3·4 근본 — 지연일(직전 거래일 봉 미수신)은 raise → _run_with_retry 백오프.

    saved는 병합 후 총행수라 '새 봉 없음' 신호가 아니다(실측) — 저장 패널의 마지막 봉
    날짜를 직접 검사한다. stale이면 번들·preview를 갱신하지 않는다(stale 포장 금지)."""
    import pytest
    from app import main as m
    from quant_core.data.feeds import krx_openapi

    monkeypatch.setattr(krx_openapi, "is_active", lambda: True)
    monkeypatch.setattr(krx_openapi, "fetch_futures_panel",
                        lambda s, e: {"ok": True,
                                      "saved": {"코스피200선물": {"패널행": 7}}})
    monkeypatch.setattr(m.data_cache, "invalidate", lambda: None)
    # 07-14 사고 형상: 직전 거래일 07-14인데 저장 패널 마지막 봉이 07-10.
    packaged, previewed = _patch_morning(
        monkeypatch, m, last_bar="2026-07-10", prev_session="2026-07-14")

    with pytest.raises(RuntimeError, match="stale"):
        m._refresh_krx_futures()
    assert not packaged and not previewed, "stale 상태로 번들/preview 갱신 금지"
