"""데이터셋 캐시 일관성 — 세대 마커로 out-of-process/수동 변경 자가 감지.

cron은 in-process invalidate로 즉시 무효화되지만, manage·백필(별도 프로세스)이나
수동 변경은 공유 세대 토큰(data_fetcher.data_generation)으로 라이브 서버가 자가 리로드.
"""

import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import data_cache
from quant_core import data_fetcher


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """세대 마커를 tmp로 격리 + data_cache 모듈 상태 저장/복원."""
    monkeypatch.setattr(data_fetcher, "_GENERATION_PATH", tmp_path / "_generation")
    saved = (data_cache._dataset, data_cache._manifest, data_cache._version,
             data_cache._built_generation, data_cache._last_check)
    data_cache._dataset = None
    data_cache._manifest = None
    data_cache._built_generation = None
    data_cache._last_check = 0.0
    yield
    (data_cache._dataset, data_cache._manifest, data_cache._version,
     data_cache._built_generation, data_cache._last_check) = saved


def test_mark_dirty_changes_generation(isolated_cache):
    assert data_fetcher.data_generation() == 0          # 파일 없음
    g1 = data_fetcher.mark_data_dirty()
    assert g1 != 0 and data_fetcher.data_generation() == g1
    g2 = data_fetcher.mark_data_dirty()
    assert g2 != g1                                      # ns 토큰 갱신


def test_get_dataset_reloads_on_generation_change(isolated_cache, monkeypatch):
    calls = {"n": 0}

    def fake_load(**_):
        calls["n"] += 1
        return {"GEN": calls["n"]}                       # 매번 다른 객체

    monkeypatch.setattr(data_cache.qc, "load_dataset", fake_load)

    d1 = data_cache.get_dataset()
    assert calls["n"] == 1

    # 같은 세대 — throttle 우회해도 재로드 없음
    data_cache._last_check = 0.0
    assert data_cache.get_dataset() is d1
    assert calls["n"] == 1

    # 다른 프로세스가 데이터 변경(세대 bump) → 다음 읽기에 리로드
    data_fetcher.mark_data_dirty()
    data_cache._last_check = 0.0                         # throttle 우회
    d2 = data_cache.get_dataset()
    assert calls["n"] == 2 and d2 is not d1


def test_throttle_skips_check_within_interval(isolated_cache, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(data_cache.qc, "load_dataset",
                        lambda **_: (calls.__setitem__("n", calls["n"] + 1), {"x": 1})[1])
    data_cache.get_dataset()
    assert calls["n"] == 1

    data_fetcher.mark_data_dirty()
    data_cache._last_check = time.monotonic()            # 방금 확인 → throttle 내
    data_cache.get_dataset()
    assert calls["n"] == 1                               # 미감지(쓰로틀)


def test_invalidate_bumps_marker_for_cross_process(isolated_cache, monkeypatch):
    monkeypatch.setattr(data_cache.qc, "load_dataset", lambda **_: {"x": 1})
    data_cache.get_dataset()                             # 캐시 빌드
    before = data_fetcher.data_generation()
    data_cache.invalidate()
    assert data_fetcher.data_generation() != before     # 마커 bump → 타 프로세스도 무효화
    assert data_cache._dataset is None                  # 로컬 캐시도 폐기


def test_aux_all_caches_until_generation_change(isolated_cache):
    """보조 패널(펀더/컨센서스/수급)은 한 세대 동안 1회만 로드 — get_projected가 호출마다
    디스크 전독하던 aux(~18s 병목)를 캐시. 데이터 세대 변경 시 함께 폐기(일관성)."""
    calls = {"n": 0}

    def fake_loader():
        calls["n"] += 1
        return {"AUX": calls["n"]}

    data_cache._aux_cache.clear()
    try:
        a = data_cache._aux_all("fund", fake_loader)
        b = data_cache._aux_all("fund", fake_loader)     # 캐시 히트 — 재로드 없음
        assert a is b and calls["n"] == 1

        data_fetcher.mark_data_dirty()                    # 별도 프로세스 데이터 변경
        data_cache._last_check = 0.0                      # throttle 우회
        c = data_cache._aux_all("fund", fake_loader)
        assert calls["n"] == 2 and c is not a             # 세대 변경 → 재로드
    finally:
        data_cache._aux_cache.clear()


def test_get_projected_narrows_to_fund_bearing(monkeypatch):
    """펀더-only 컬럼·전 유니버스 요청 → 펀더 보유 종목만 계산(나머지는 어차피 NaN으로 랭킹
    제외 = 의미 불변). 크립토·FRED 등 수천 종목 헛계산 제거(SELECT 3분 병목 근본·뿌리④)."""
    import pandas as pd
    raw = {"A": pd.DataFrame({"Close": [1.0, 2.0]}),       # 펀더 보유
           "B": pd.DataFrame({"Close": [1.0, 2.0]}),       # 펀더 없음
           "FRED:X": pd.DataFrame({"Close": [1.0, 2.0]})}  # 보조 없음
    monkeypatch.setattr(data_cache, "get_raw_dataset", lambda: raw)
    monkeypatch.setattr(data_cache.data_fetcher, "load_fund_all",
                        lambda: {"A": pd.DataFrame({"pb_ratio": [1.0, 1.1]})})
    monkeypatch.setattr(data_cache, "compute_columns",
                        lambda df, c, *aux, **kw: df.assign(pb_ratio=1.0))
    data_cache._aux_cache.clear()
    try:
        out = data_cache.get_projected(["pb_ratio"], symbols=None)
        assert set(out.keys()) == {"A"}                   # 펀더 보유 A만 — B·FRED는 narrow 제외
    finally:
        data_cache._aux_cache.clear()
