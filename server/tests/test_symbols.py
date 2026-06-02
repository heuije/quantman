"""tradable_symbols() — /symbols payload와 동일 tradable 기준 검증(특히 dedup 분기).

인덱스에 OHLC 없이 있으면서 마스터에도 있는 종목(매크로·지수류)을 tradable로
오판정하던 회귀를 고정한다 — payload는 인덱스 전체(seen) 기준으로 Loop 2를
dedup하므로 helper도 동일해야 UI(/symbols)와 서버 게이트가 어긋나지 않는다.

네트워크/캐시 로드 없이 인덱스·마스터를 인메모리로 주입해 순수 로직만 검증.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import data_cache, kis_master_cache
from app.symbols import tradable_symbols


def test_tradable_symbols_matches_payload_dedup(monkeypatch):
    # AAA: OHLC + 마스터        → tradable
    # BBB: 인덱스에 OHLC 없음 + 마스터(KOSPI) → 비매매 (payload tradable=in_master AND has_ohlc=False)
    # CCC: OHLC 있으나 마스터 없음 → 비매매
    # DDD: 마스터에만(KOSPI, 데이터 없음) → tradable (라이브 매매)
    # EEE: 마스터에만(미국 NAS)   → 제외
    monkeypatch.setattr(data_cache, "get_symbol_index", lambda: {
        "AAA": {"has_ohlc": True, "rows": 10},
        "BBB": {"has_ohlc": False, "rows": 0},
        "CCC": {"has_ohlc": True, "rows": 10},
    })
    monkeypatch.setattr(kis_master_cache, "get_master_list", lambda: [
        {"symbol": "AAA", "market": "KOSPI"},
        {"symbol": "BBB", "market": "KOSPI"},
        {"symbol": "DDD", "market": "KOSPI"},
        {"symbol": "EEE", "market": "NAS"},
    ])
    assert tradable_symbols() == {"AAA", "DDD"}
    # 회귀 핵심: OHLC 없는 인덱스 종목(BBB)이 tradable로 새지 않는다(seen 기준 dedup).
    assert "BBB" not in tradable_symbols()
