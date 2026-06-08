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


def test_refresh_kr_fundamentals_recent_years_and_budget(monkeypatch):
    """적재 가속 — 현재값 우선: fetch를 최근 2년·예산 18000으로 호출하고 캐시 invalidate.

    이전 8년치×9000은 종목당 ~50초·전체 ~수주라 초기 적재 병목이었음. 최근 2년(현재 TTM)·예산
    상향으로 전 종목 현재 pb/pe/ev를 ~2-3일에 채운다(스크리닝·360 전종목 즉시 가용)."""
    from datetime import datetime

    import quant_core.data.feeds.fundamental_kr as fk
    import quant_core.data_fetcher as df

    monkeypatch.setattr(df, "load_managed_kr_codes", lambda: ["005930", "000660"])
    cap: dict = {}
    monkeypatch.setattr(fk, "fetch", lambda codes, years, budget_calls=9000, fresh_days=80:
                        (cap.update(years=years, budget=budget_calls), {"ok": 2, "fail": 0})[1])
    inval: list = []
    monkeypatch.setattr(main.data_cache, "invalidate", lambda: inval.append(True))

    main._refresh_kr_fundamentals()

    yr = datetime.now().year
    assert cap["years"] == [yr - 1, yr]            # 최근 2년(현재 TTM)
    assert cap["budget"] == 18000                  # 예산 상향(OpenDART 20k 한도 내)
    assert inval == [True]                          # 적재 후 캐시 무효화(전 경로 일관)
