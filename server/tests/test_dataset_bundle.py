"""dataset 번들 스코프 분리 — trading(자동매매 로컬앱) vs full(dev 챗봇).

trading은 서버 전용 피드(flow·시총·공매도·13F)를 담지 않아야(로컬앱 하위호환·불필요
다운로드 방지), full은 담아야(dev 테스트환경이 프로덕션 볼륨과 동일 데이터).

    cd platform && pytest server/tests/test_dataset_bundle.py -v
"""
import tarfile

import pandas as pd
import pytest
import zstandard

from quant_core import data_fetcher
from app.routers import dataset as ds

_SERVER_FEEDS = ("flow", "marketcap", "short_volume", "institutional")


def _seed(base):
    df = pd.DataFrame({"Close": [1.0, 2.0]})
    df.to_parquet(base / "005930.parquet")
    (base / "_classification.json").write_text("{}", encoding="utf-8")
    for sub in ("fundamentals",) + _SERVER_FEEDS:
        (base / sub).mkdir(parents=True, exist_ok=True)
        df.to_parquet(base / sub / "005930.parquet")


def _members(bundle_path):
    dctx = zstandard.ZstdDecompressor()
    with open(bundle_path, "rb") as f, dctx.stream_reader(f) as zr, \
            tarfile.open(fileobj=zr, mode="r|") as tar:
        return sorted(m.name for m in tar if m.isfile())


def test_trading_excludes_server_feeds(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, "DATA_DIR", tmp_path)
    _seed(tmp_path)
    assert ds.build_bundle("trading")["ok"]
    names = _members(ds._bundle_path("trading"))
    assert "005930.parquet" in names
    assert "fundamentals/005930.parquet" in names
    assert "_classification.json" in names
    for sub in _SERVER_FEEDS:
        assert not any(n.startswith(f"{sub}/") for n in names), sub
    # 파일명 하위호환 — 배포된 로컬앱이 요청하는 경로
    assert ds._bundle_path("trading").name == "dataset-bundle.tar.zst"


def test_full_includes_server_feeds(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, "DATA_DIR", tmp_path)
    _seed(tmp_path)
    assert ds.build_bundle("full")["ok"]
    names = _members(ds._bundle_path("full"))
    assert "005930.parquet" in names
    for sub in ("fundamentals",) + _SERVER_FEEDS:
        assert f"{sub}/005930.parquet" in names, sub
    assert ds._bundle_path("full").name == "dataset-bundle-full.tar.zst"


def test_scopes_independent_etag_and_full_superset(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, "DATA_DIR", tmp_path)
    _seed(tmp_path)
    ds.build_bundle("trading")
    ds.build_bundle("full")
    assert ds._current_bundle_etag("trading") != ds._current_bundle_etag("full")
    assert len(_members(ds._bundle_path("full"))) > len(_members(ds._bundle_path("trading")))


def test_unknown_scope_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, "DATA_DIR", tmp_path)
    with pytest.raises(ValueError):
        ds.build_bundle("bogus")
