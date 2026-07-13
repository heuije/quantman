"""find_orphan_parquets — orphan parquet 안전 판정(오삭제 방지).

유니버스 밖 top-level parquet만 orphan으로 잡고, 매크로·실티커·가격별칭·서브디렉터리는
보존하는지 고정한다. (2026-07-13 orphan 정리 — 옛 오종목 AACT_U.parquet 볼륨 잔존 대응.)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core import data_fetcher as df  # noqa: E402


@pytest.fixture
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(df, "DATA_DIR", tmp_path)
    monkeypatch.setattr(df, "ALL_SYMBOLS", ["금선물", "S&P500", "코스피200선물"])
    monkeypatch.setattr(df, "PRICE_ALIAS", {"미니코스피200선물": "코스피200선물"})
    monkeypatch.setattr(df, "load_managed_kr_codes", lambda: ["005930"])
    monkeypatch.setattr(df, "load_managed_overseas", lambda: [{"code": "AAPL"}, {"code": "CON"}])
    monkeypatch.setattr(df, "load_user_stocks", lambda: [{"name": "MyStock"}])
    return tmp_path


def _touch(d: Path, *names):
    for n in names:
        (d / n).write_text("x", encoding="utf-8")


def test_orphans_detected_universe_preserved(_env):
    # 보존돼야: 매크로·실티커·KR·별칭 대상·사용자
    _touch(_env, "금선물.parquet", "S&P500.parquet", "코스피200선물.parquet",
           "005930.parquet", "AAPL.parquet", "CON.parquet", "MyStock.parquet")
    # orphan이어야: 옛 오종목/상장폐지 잔재
    _touch(_env, "AACT_U.parquet", "AAC_WS.parquet", "DELISTED.parquet")
    orphans = {p.name for p in df.find_orphan_parquets()}
    assert orphans == {"AACT_U.parquet", "AAC_WS.parquet", "DELISTED.parquet"}


def test_subdirs_excluded(_env):
    """fundamentals/·flow/ 서브디렉터리 parquet은 스코프 밖(별도 피드 소유) — 미포함."""
    _touch(_env, "AAPL.parquet")
    for sub in ("fundamentals", "flow"):
        (_env / sub).mkdir()
        _touch(_env / sub, "ORPHANLIKE.parquet")     # 서브디렉터리 — 건드리지 않음
    orphans = df.find_orphan_parquets()
    assert orphans == []                              # top-level에 orphan 없음


def test_price_alias_target_preserved(_env):
    """가격별칭(미니→정규)은 정규 파일을 공유 — 정규 파일이 orphan으로 안 잡힌다."""
    _touch(_env, "코스피200선물.parquet")             # 미니의 실파일(별칭 대상)
    assert df.find_orphan_parquets() == []


def test_malformed_overseas_entry_does_not_protect_its_file(_env, monkeypatch):
    """명단 self-clean 전에 malformed 엔트리(AACT_U)가 남아 있어도, keep-set은 predicate로
    필터하므로 그 orphan 파일이 '보호'되지 않는다(orphan 정리가 명단 타이밍에 비의존)."""
    monkeypatch.setattr(df, "load_managed_overseas",
                        lambda: [{"code": "AAPL"}, {"code": "AACT_U"}])   # AACT_U=아직 남은 malformed
    _touch(_env, "AAPL.parquet", "AACT_U.parquet")
    orphans = {p.name for p in df.find_orphan_parquets()}
    assert orphans == {"AACT_U.parquet"}, f"malformed의 파일은 orphan이어야: {orphans}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
