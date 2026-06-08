"""KR 펀더멘털 백필 — 10분 청크 트리거 배선 회귀 차단.

한 방(부팅+420초 / 17:30 단일) 의존은 재배포 폭주에 fetch가 시작도 못 해 정체했다(2026-06-10
실측 24h 0건). 10분마다 짧은 청크(budget 1500)로 받게 바꿔 폭주에도 전진하게 한다. 수집(청크)과
attach(invalidate)를 분리: 청크는 parquet만 기록(invalidate는 144회/일 재빌드라 금지),
17:30 앵커가 일일 invalidate. startup은 부팅 직후 1회 청크.

    cd server && pytest tests/test_initial_kr_fundamentals.py -v
"""
import time

import pytest

main = pytest.importorskip("app.main")


def test_initial_refresh_calls_backfill_chunk(monkeypatch):
    """startup 래퍼가 지연 후 _backfill_kr_fundamentals_chunk를 호출한다(키 유무 무관)."""
    calls = []
    monkeypatch.setattr(time, "sleep", lambda *_a: None)               # 지연 제거
    monkeypatch.setattr(main, "_backfill_kr_fundamentals_chunk", lambda: calls.append(True))
    main._initial_kr_fundamentals_refresh()
    assert calls == [True]


def test_initial_refresh_swallows_exception(monkeypatch):
    """청크 예외(키 미설정 등)는 부팅을 막지 않고 삼켜야 한다 — 10분 cron이 재시도."""
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    def _boom():
        raise RuntimeError("OPENDART_API_KEY 환경변수 필요")

    monkeypatch.setattr(main, "_backfill_kr_fundamentals_chunk", _boom)
    main._initial_kr_fundamentals_refresh()        # 예외 전파 없이 리턴해야 통과


def test_backfill_chunk_budget_and_years_no_invalidate(monkeypatch):
    """청크는 최근 2년·budget 1500으로 fetch하고 **invalidate는 하지 않는다**(재빌드 폭주 방지).

    한 방 18000 대신 작은 청크. 10분마다 144회 invalidate는 비싼 dataset 재빌드 폭주라,
    attach는 일일 앵커(17:30)·dataset_kr(18:15)에 맡긴다."""
    from datetime import datetime

    import quant_core.data.feeds.fundamental_kr as fk
    import quant_core.data_fetcher as df

    monkeypatch.setattr(df, "load_managed_kr_codes", lambda: ["005930", "000660"])
    cap: dict = {}
    monkeypatch.setattr(fk, "fetch", lambda codes, years, budget_calls=9000, fresh_days=80:
                        (cap.update(years=years, budget=budget_calls),
                         {"ok": 2, "fail": 0, "empty": 0, "calls": 20})[1])
    inval: list = []
    monkeypatch.setattr(main.data_cache, "invalidate", lambda: inval.append(True))

    main._backfill_kr_fundamentals_chunk()

    yr = datetime.now().year
    assert cap["years"] == [yr - 1, yr]            # 최근 2년(현재 TTM)
    assert cap["budget"] == 1500                   # 청크 예산
    assert inval == []                              # 청크는 invalidate 안 함


def test_refresh_kr_fundamentals_invalidates_only(monkeypatch):
    """일일 앵커 _refresh_kr_fundamentals는 fetch 없이 invalidate만(누적분 attach)."""
    import quant_core.data.feeds.fundamental_kr as fk

    fetched: list = []
    monkeypatch.setattr(fk, "fetch", lambda *a, **k: fetched.append(True))
    inval: list = []
    monkeypatch.setattr(main.data_cache, "invalidate", lambda: inval.append(True))

    main._refresh_kr_fundamentals()

    assert inval == [True]                          # attach
    assert fetched == []                            # 수집은 청크가 — 여기선 안 함


def test_refresh_naver_materializes_valuation(monkeypatch):
    """NAVER 고유필드 merge 직후 스크리너 밸류 일원화(materialize)를 호출한다 — 360과 같은 출처(OpenDART).

    배선이 이 자리(부팅+120s·17:00)여야 스냅샷 통째 재구축(15:45·부팅) 후에도 NAVER와 동일 cadence로
    pbr/per가 채워진다 — 17:30 앵커에만 두면 부팅~17:30 사이 스크리너 밸류 공백(회귀)."""
    monkeypatch.setattr(main.naver_fundamentals, "refresh", lambda: {"ok": 1})
    called = []
    monkeypatch.setattr(main, "_materialize_kr_valuation", lambda: called.append(True))

    main._refresh_naver()
    assert called == [True]
