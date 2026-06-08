"""Phase 1 — KR 펀더멘털 startup 초기 fetch 배선 회귀 차단.

OpenDART 펀더멘털은 17:30 cron만 있고 startup 초기 fetch가 없어, 재배포·신규 볼륨 후 다음
17:30까지 pb_ratio·PER·EV가 전 종목 NaN이었다(스크리닝·360 밸류·진단 동시 빈값의 근본원인).
startup 스레드가 _refresh_kr_fundamentals를 부르고, 그 예외(예: 키 미설정)는 부팅을 막지 않음을 고정.

    cd platform/server && pytest tests/test_initial_kr_fundamentals.py -v
"""
import time

import pytest

main = pytest.importorskip("app.main")


def test_initial_kr_fundamentals_calls_refresh(monkeypatch):
    """startup 초기 fetch 래퍼가 지연 후 _refresh_kr_fundamentals를 호출한다(키 유무 무관)."""
    calls = []
    monkeypatch.setattr(time, "sleep", lambda *_a: None)               # 420s 지연 제거
    monkeypatch.setattr(main, "_refresh_kr_fundamentals", lambda: calls.append(True))
    main._initial_kr_fundamentals_refresh()
    assert calls == [True]


def test_initial_kr_fundamentals_swallows_exception(monkeypatch):
    """fetch 예외(키 미설정 등)는 부팅을 막지 않고 삼켜야 한다 — 17:30 cron이 재시도."""
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    def _boom():
        raise RuntimeError("OPENDART_API_KEY 환경변수 필요")

    monkeypatch.setattr(main, "_refresh_kr_fundamentals", _boom)
    main._initial_kr_fundamentals_refresh()        # 예외 전파 없이 리턴해야 통과
