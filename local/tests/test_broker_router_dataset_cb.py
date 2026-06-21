"""BrokerRouter dataset_for_code 콜백 — 기본=quant_core(KIS 무변경), 주입=LS-aware."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp.broker_router import BrokerRouter


class _Stock:
    def account_snapshot(self, overseas=True):
        return {"balance": {"cash": 100}, "positions": []}


class _Fut:
    domestic_configured = True
    overseas_configured = False
    def account_snapshot(self):
        return {"account": {"equity": 5000000}, "positions": [{"symbol": "101V6000", "side": "long", "qty": 1}]}


def test_default_dataset_for_code_is_kis():
    # 기본 콜백(quant_core) — LS코드 101V6000은 KIS 매핑서 None → 심볼 미정규화(현 동작 보존)
    r = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "101V6000"


def test_injected_dataset_for_code_normalizes():
    r = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s,
                     dataset_for_code=lambda c: "코스피200선물" if c.startswith("101") else None)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "코스피200선물" and pos["contract_code"] == "101V6000"
