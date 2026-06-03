"""futures_config.INSTRUMENTS 레지스트리 무결성 — 7종(라이브 6 + 정적 KOSPI) 설정 검증."""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from quant_core.oil_futures import prepare_wti

from app.futures_config import INSTRUMENTS
from app.routers.futures import _load_static_csv


def test_seven_instruments_registered():
    assert set(INSTRUMENTS) == {
        "oil", "nasdaq", "natgas", "gold", "silver", "bitcoin", "kospi",
    }


def test_every_config_is_valid():
    for sym, cfg in INSTRUMENTS.items():
        assert cfg.symbol == sym
        # 라이브 종목은 data_key, 정적 스냅샷 종목은 static_path 중 하나는 반드시 있어야 한다.
        assert cfg.data_key or cfg.static_path
        assert cfg.name and cfg.unit and cfg.eyebrow and cfg.source
        assert cfg.currency in {"USD", "KRW"}
        assert cfg.spec.tick > 0 and cfg.spec.multiplier > 0
        shorts = cfg.shorts.values()
        longs = cfg.longs.values()
        assert len(shorts) >= 2 and len(longs) >= 2
        assert shorts == sorted(set(shorts))
        assert longs == sorted(set(longs))


def test_oil_matches_legacy_defaults():
    oil = INSTRUMENTS["oil"]
    assert oil.shorts.values()[0] == 80 and oil.shorts.values()[-1] == 150
    assert oil.longs.values()[0] == 10 and oil.longs.values()[-1] == 60
    assert len(oil.shorts.values()) == 71 and len(oil.longs.values()) == 51
    assert oil.spec.tick == 0.01 and oil.spec.multiplier == 1000
    assert oil.currency == "USD" and oil.static_path is None


def test_kospi_is_static_krw_snapshot():
    k = INSTRUMENTS["kospi"]
    assert k.static_path and not k.data_key      # 정적 스냅샷 — 라이브 캐시 미사용
    assert k.currency == "KRW"                   # KRX 계약사양상 손익은 원화
    assert k.spec.tick == 0.05 and k.spec.multiplier == 250000
    # 전구간 임계: shorts(250,1450,25)=49, longs(200,1450,25)=51
    assert len(k.shorts.values()) == 49 and k.shorts.values()[0] == 250
    assert len(k.longs.values()) == 51 and k.longs.values()[0] == 200


def test_kospi_static_csv_loads_and_prepares():
    """config의 static_path가 실제 번들 CSV로 해석되고 엔진 입력으로 정제되는지 — end-to-end."""
    k = INSTRUMENTS["kospi"]
    df = prepare_wti(_load_static_csv(k.static_path))
    assert len(df) > 4000                                   # 2010~ 16년치
    assert {"date", "open", "high", "low", "close"}.issubset(df.columns)
    assert df["close"].min() > 0
    assert df["date"].is_monotonic_increasing
