"""HTTP 서빙 캐시 영속 경로 — 볼륨(prod)/앱폴더(dev) 해석 + HOME 캐시 3종 배선 회귀 가드."""
import os

from app.serving_cache import serving_cache_path


def test_uses_volume_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("QP_CORE_DATA_DIR", str(tmp_path))
    p = serving_cache_path("financials", "005930.json")
    assert p == os.path.join(str(tmp_path), "serving_cache", "financials", "005930.json")


def test_falls_back_to_app_data_when_unset(monkeypatch):
    monkeypatch.delenv("QP_CORE_DATA_DIR", raising=False)
    p = serving_cache_path("earnings")
    assert p.endswith(os.path.join("data", "serving_cache", "earnings"))
    assert "app" in p          # server/app/data 로컬 폴백


def test_home_caches_point_under_serving_cache():
    """재배포 warmup 근본수정 회귀 가드 — 3개 HTTP 서빙 캐시가 영속 네임스페이스를 가리킨다."""
    from app import company_profiles, financials, krdata
    assert os.path.join("serving_cache", "financials") in financials._DIR
    assert os.path.join("serving_cache", "earnings") in krdata._EARN_DIR
    assert os.path.join("serving_cache", "company_profiles.json") in company_profiles._PATH
