"""_prune_orphan_parquets_once — 1회성 orphan 정리 startup 잡의 안전 가드 검증.

자동 삭제라 mass-deletion 방지가 핵심: marker 게이트(1회)·유니버스 sanity floor(미로드 시
삭제 금지·재시도). 실 삭제 대상은 유니버스 밖 orphan만이고 매크로·실티커는 보존한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _SERVER_DIR.parent / "core"
for _p in (str(_CORE_DIR), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _setup(monkeypatch, tmp_path):
    from app import main as m
    from quant_core import data_fetcher as df
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)       # 540s 대기 스킵
    monkeypatch.setattr(df, "DATA_DIR", tmp_path)
    monkeypatch.setattr(df, "mark_data_dirty", lambda: 0)
    return m, df


def _sane_keys(*extra):
    return {f"K{i}" for i in range(2000)} | set(extra)


def test_skips_when_marker_exists(monkeypatch, tmp_path):
    m, df = _setup(monkeypatch, tmp_path)
    (tmp_path / m._ORPHAN_PRUNE_MARKER).write_text("done", encoding="utf-8")
    (tmp_path / "AACT_U.parquet").write_text("x", encoding="utf-8")
    monkeypatch.setattr(df, "iter_universe_keys", lambda: _sane_keys())
    m._prune_orphan_parquets_once()
    assert (tmp_path / "AACT_U.parquet").exists(), "marker 있으면 삭제 안 함"


def test_skips_and_no_marker_when_universe_too_small(monkeypatch, tmp_path):
    """유니버스 미로드(키 < floor) — 전 파일 orphan 오판 위험 → 삭제 금지·marker 미기록(재시도)."""
    m, df = _setup(monkeypatch, tmp_path)
    (tmp_path / "AACT_U.parquet").write_text("x", encoding="utf-8")
    (tmp_path / "AAPL.parquet").write_text("x", encoding="utf-8")
    monkeypatch.setattr(df, "iter_universe_keys", lambda: {"AAPL"})   # 1개 < 1000
    m._prune_orphan_parquets_once()
    assert (tmp_path / "AACT_U.parquet").exists()                     # 아무것도 안 지움
    assert (tmp_path / "AAPL.parquet").exists()
    assert not (tmp_path / m._ORPHAN_PRUNE_MARKER).exists(), "재시도 위해 marker 미기록"


def test_deletes_orphans_preserves_universe_and_marks(monkeypatch, tmp_path):
    m, df = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(df, "iter_universe_keys", lambda: _sane_keys("AAPL", "금선물"))
    for n in ("AAPL.parquet", "금선물.parquet", "AACT_U.parquet", "DELISTED.parquet"):
        (tmp_path / n).write_text("x", encoding="utf-8")
    m._prune_orphan_parquets_once()
    assert (tmp_path / "AAPL.parquet").exists()          # 실티커 보존
    assert (tmp_path / "금선물.parquet").exists()          # 매크로 보존
    assert not (tmp_path / "AACT_U.parquet").exists()    # orphan 삭제
    assert not (tmp_path / "DELISTED.parquet").exists()
    assert (tmp_path / m._ORPHAN_PRUNE_MARKER).exists()  # 1회 완료 marker


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
