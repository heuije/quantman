"""dataset 번들이 Windows 예약장치명 파일을 안전화해 배포하는지 — 회귀 테스트.

2026-07-13 실유저 무발주 인시던트 재발 방지: 실재 티커 CON(Concentra)·PRN이 공유 번들에
실려 한 유저의 Windows 로컬앱 추출이 [WinError]로 전체 중단 → dataset 거의 빔 → 발주 0.
번들 arcname은 sanitize_fs_name으로 통과돼 예약명이 클라이언트에 도달하지 않아야 한다.

    cd platform && PYTHONPATH="core;server" pytest server/tests/test_dataset_bundle_reserved.py -v
"""
import tarfile
from pathlib import PurePosixPath

import pandas as pd
import zstandard

from quant_core import data_fetcher
from app.routers import dataset as ds

_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
             | {f"COM{i}" for i in range(1, 10)}
             | {f"LPT{i}" for i in range(1, 10)})


def _members(bundle_path):
    dctx = zstandard.ZstdDecompressor()
    with open(bundle_path, "rb") as f, dctx.stream_reader(f) as zr, \
            tarfile.open(fileobj=zr, mode="r|") as tar:
        return sorted(m.name for m in tar if m.isfile())


def test_bundle_sanitizes_windows_reserved_names(tmp_path, monkeypatch):
    monkeypatch.setattr(data_fetcher, "DATA_DIR", tmp_path)
    df = pd.DataFrame({"Close": [1.0, 2.0]})
    # 인시던트 재현: 루트에 예약명 티커 + 정상, fundamentals/ 에도 예약명
    for name in ("CON", "PRN", "AAPL", "코스피200선물"):
        df.to_parquet(tmp_path / f"{name}.parquet")
    (tmp_path / "fundamentals").mkdir()
    df.to_parquet(tmp_path / "fundamentals" / "CON.parquet")

    assert ds.build_bundle("trading")["ok"]
    names = _members(ds._bundle_path("trading"))

    # ① 예약명 멤버가 번들에 하나도 남지 않아야(클라이언트 추출 크래시 원천 차단)
    leftover = [n for n in names if PurePosixPath(n).stem.upper() in _RESERVED]
    assert not leftover, f"예약명 멤버 잔존: {leftover}"

    # ② 예약명은 안전화돼 배포(CON→CON_, PRN→PRN_)
    assert "CON_.parquet" in names
    assert "PRN_.parquet" in names
    assert "fundamentals/CON_.parquet" in names

    # ③ 정상 심볼은 byte-identical(기존 유저 하위호환 — 파일명 불변)
    assert "AAPL.parquet" in names
    assert "코스피200선물.parquet" in names
