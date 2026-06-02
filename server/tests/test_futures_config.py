"""futures_config.INSTRUMENTS 레지스트리 무결성 — 6종 모두 유효한 설정."""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.futures_config import INSTRUMENTS


def test_six_instruments_registered():
    assert set(INSTRUMENTS) == {"oil", "nasdaq", "natgas", "gold", "silver", "bitcoin"}


def test_every_config_is_valid():
    for sym, cfg in INSTRUMENTS.items():
        assert cfg.symbol == sym
        assert cfg.data_key and cfg.name and cfg.unit and cfg.eyebrow and cfg.source
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
