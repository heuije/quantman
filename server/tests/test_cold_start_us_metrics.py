"""P1 회귀 — 콜드스타트 us_metrics 빈 값 방지 (호출 순서 단언, 네트워크 없음).

근본 원인: S&P500 시드가 _refresh_kr_dataset(18:15)에만 있어, 첫 부팅 때 글로벌
초기 갱신이 kr보다 먼저 돌면 managed_overseas가 비어 US OHLCV를 못 받고
us_metrics가 0이 됐다(다음 07:30 cron까지). 또 마스터 미로드 시 build_metrics가
거래소 메타 없는 종목을 전부 skip해 0.

수정: _refresh_global_dataset이 ① fetch_managed_overseas 전에 S&P500 시드,
② us_metrics.refresh 전에 마스터 미로드면 로드. 둘 다 멱등.

이 테스트는 외부 fetch를 모두 stub해 _refresh_global_dataset의 호출 순서만 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path


_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from quant_core import data_fetcher
from app import main as appmain
from app import us_metrics_cache


def _patch(monkeypatch, master_loaded: bool) -> list[str]:
    calls: list[str] = []

    def _fetch_overseas(*a, **k):
        calls.append("fetch_overseas")
        return 0

    monkeypatch.setattr(data_fetcher, "fetch_all",
                        lambda **k: calls.append("fetch_all"))
    monkeypatch.setattr(data_fetcher, "fetch_managed_overseas", _fetch_overseas)
    monkeypatch.setattr(appmain, "_rebuild_overseas_universe",
                        lambda: (calls.append("rebuild"), 0)[1])
    monkeypatch.setattr(appmain.data_cache, "invalidate",
                        lambda: calls.append("invalidate"))
    monkeypatch.setattr(appmain, "_trigger_preview",
                        lambda src: calls.append(f"preview:{src}"))
    # _package_bundle는 실제로 DATA_DIR(수만 개 parquet)를 tar+zstd로 압축한다.
    # 이 테스트는 _refresh_global_dataset의 '호출 순서'만 검증하므로 스텁한다 —
    # 스텁하지 않으면 공유·라이브 볼륨을 실제 압축하느라 (a) 테스트가 수십 초로 느려지고
    # (b) 데이터엔진 cron·병렬 세션이 압축 중 parquet을 추가/삭제하면 build_bundle의
    # tar.add가 FileNotFoundError로 터져(_refresh가 단언 전에 raise) 전체 스위트·동시
    # 실행에서만 깨지는 격리 결함이 된다. _trigger_preview와 동일 이유의 스텁.
    monkeypatch.setattr(appmain, "_package_bundle",
                        lambda: calls.append("package_bundle"))
    monkeypatch.setattr(appmain.kis_master_cache, "get_master_set",
                        lambda: ({"AAPL"} if master_loaded else set()))
    monkeypatch.setattr(appmain.kis_master_cache, "refresh",
                        lambda: (calls.append("master_refresh"), {})[1])
    monkeypatch.setattr(us_metrics_cache, "refresh",
                        lambda: (calls.append("us_refresh"), {})[1])
    return calls


def test_rebuild_before_overseas_fetch(monkeypatch):
    """해외 유니버스 rebuild가 해외 OHLCV fetch보다 먼저 호출된다 (콜드스타트 순서)."""
    calls = _patch(monkeypatch, master_loaded=True)
    appmain._refresh_global_dataset()
    assert "rebuild" in calls and "fetch_overseas" in calls
    assert calls.index("rebuild") < calls.index("fetch_overseas")


def test_master_loaded_before_us_metrics_when_cold(monkeypatch):
    """마스터 미로드 시: us_metrics.refresh 전에 마스터를 로드한다."""
    calls = _patch(monkeypatch, master_loaded=False)
    appmain._refresh_global_dataset()
    assert "master_refresh" in calls
    assert calls.index("master_refresh") < calls.index("us_refresh")


def test_master_not_reloaded_when_already_loaded(monkeypatch):
    """마스터가 이미 로드돼 있으면 재로드하지 않는다 (멱등 가드)."""
    calls = _patch(monkeypatch, master_loaded=True)
    appmain._refresh_global_dataset()
    assert "master_refresh" not in calls
    assert "us_refresh" in calls          # 그래도 metrics는 재빌드
