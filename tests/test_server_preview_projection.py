"""P2 — preview가 컬럼 프로젝션을 쓰는지(전 유니버스 9.4GB 빌드 회피) 검증.

preview cron이 build_user_preview→get_dataset(full)을 호출하던 것이 OOM 크래시 루프의
직접 원인이었다. _preview_dataset이 전략별 참조 컬럼·종목만 프로젝션하고, 결정 불가
(strat:)일 때만 전체로 폴백하는지 고정한다.

    cd platform && pytest tests/test_server_preview_projection.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "server"))

pe = pytest.importorskip("app.preview_engine")


def _strat(definition: dict):
    # _preview_dataset는 전략 *definition dict* 리스트를 받는다 — build_user_preview가
    # [d["definition"] for d in ir_defs]로 호출(C1 리팩터 후 ORM/객체 아닌 plain dict).
    # 테스트도 동일 계약으로 definition dict를 그대로 전달한다.
    return definition


_ALL_MOM = _strat({
    "signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
    "universe": {"kind": "all"},
    "position": {"entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 3}},
    "simulation": {"initial_capital": 1e7},
})
_SINGLE = _strat({
    "signal": {"op": "compare", "params": {"op": "<"},
               "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}},
                          "right": {"op": "const", "params": {"value": 35.0}}}},
    "universe": {"kind": "single", "symbols": ["AAA"]},
    "position": {"entry": {"mode": "on_signal"}, "exit": {"hold_days": 5}},
})
_COMPOSE = _strat({
    "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
    "universe": {"kind": "list", "symbols": ["strat:7"]},
    "position": {"entry": {"mode": "always"}},
})


@pytest.fixture
def _spy(monkeypatch):
    calls = {"projected": [], "full": 0}

    def fake_projected(columns, symbols=None):
        calls["projected"].append((set(columns), symbols))
        return {"_proj": True}

    def fake_full():
        calls["full"] += 1
        return {"_full": True}

    monkeypatch.setattr(pe, "get_projected", fake_projected)
    monkeypatch.setattr(pe, "get_dataset", fake_full)
    return calls


def test_all_strategy_projects_full_universe_not_full_build(_spy):
    """all/momentum 전략 → 전 종목 × 참조컬럼 프로젝션. get_dataset(9.4GB) 호출 금지."""
    pe._preview_dataset([_ALL_MOM], set())
    assert _spy["full"] == 0, "preview가 전 유니버스(get_dataset)를 빌드하면 안 됨 — OOM 원인"
    assert len(_spy["projected"]) == 1
    cols, symbols = _spy["projected"][0]
    assert "momentum_12_1m" in cols
    assert symbols is None, "all 유니버스는 전 종목(symbols=None) 프로젝션"


def test_single_strategy_projects_only_needed_symbols(_spy):
    """single 전략 → 그 종목 ∪ 보유종목만 프로젝션."""
    pe._preview_dataset([_SINGLE], {"ZZZ"})
    assert _spy["full"] == 0
    cols, symbols = _spy["projected"][0]
    assert "rsi_14" in cols
    assert symbols is not None and {"AAA", "ZZZ"} <= set(symbols)


def test_composition_falls_back_to_full(_spy):
    """strat: 조합은 자식이 임의 컬럼을 쓸 수 있어 전체(get_dataset) 안전 폴백."""
    pe._preview_dataset([_COMPOSE], set())
    assert _spy["full"] == 1, "strat: 조합은 전체 폴백이어야"
    assert len(_spy["projected"]) == 0


def test_no_strategies_projects_held_only(_spy):
    """전략 없고 보유만 있으면 보유종목(Close)만 프로젝션 — 청산 미리보기용."""
    pe._preview_dataset([], {"AAA", "BBB"})
    assert _spy["full"] == 0
    cols, symbols = _spy["projected"][0]
    assert set(symbols) == {"AAA", "BBB"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
