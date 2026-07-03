"""flow.us_short_volume 피드(FINRA Reg SHO) 단위 테스트 — 네트워크 없음(fake fetch).

검증: 순수 파서(헤더/트레일러 skip·비수치 drop)·404=비거래일 vs None=실패 구분·
부분 실패 시 전진 금지·종목별 parquet 멱등 merge.
"""

from __future__ import annotations

import pandas as pd

from quant_core import data_fetcher as df_mod
from quant_core.data.feeds import short_volume_us as sv


_FILE = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
         "20240102|AAPL|438584.5|673|733402.7|B,Q,N\n"
         "20240102|MSFT|100|0|300|B,Q,N\n"
         "20240102||9|9|9|B\n"                       # 빈 심볼 → skip
         "trailer garbage line\n")                   # 비정형 → skip


def test_parse_file_pure():
    out = sv.parse_file(_FILE)
    assert set(out) == {"AAPL", "MSFT"}
    assert out["AAPL"] == (438584.5, 673.0, 733402.7)


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sv, "SHORTVOL_DIR", tmp_path / "short_volume")
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)


def test_holiday_404_ok_but_network_fail_blocks(monkeypatch, tmp_path):
    """''(404 비거래일)=정상 진행 / None(네트워크·5xx)=실패(커서 유지) 구분."""
    _setup(monkeypatch, tmp_path)
    res = sv.fetch_range("20240106", "20240107", fetch=lambda bd: "")   # 주말/휴장
    assert res["ok"] is True and res["stocks"] == 0

    res2 = sv.fetch_range("20240102", "20240103", fetch=lambda bd: None)
    assert res2["ok"] is False and res2["fail"] >= 1


def test_partial_failure_blocks_advance(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def fetch(bd):
        return None if bd == "20240103" else _FILE
    res = sv.fetch_range("20240102", "20240104", fetch=fetch)
    assert res["ok"] is False
    assert sv.load_short_volume("AAPL").empty            # 부분 저장 없음


def test_fetch_range_saves_and_idempotent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    res = sv.fetch_range("20240102", "20240103", fetch=lambda bd: _FILE)
    assert res["ok"] is True and res["stocks"] == 2

    d = sv.load_short_volume("AAPL")
    assert list(d.columns) == ["short_volume", "short_exempt_volume", "total_volume"]
    assert len(d) == 2                                   # 평일 2일(파일 Date는 무시·bd 기준)

    sv.fetch_range("20240102", "20240103", fetch=lambda bd: _FILE)   # 멱등
    assert len(sv.load_short_volume("AAPL")) == 2
