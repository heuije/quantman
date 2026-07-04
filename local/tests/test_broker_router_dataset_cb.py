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


def test_default_dataset_for_code_normalizes_krx_form():
    # 기본 콜백(quant_core)이 KRX 상품코드형(LS 잔고 t0441 "101V6000")도 역매핑한다.
    # 종전엔 None → 조용한 정규화 실패 → reconcile이 자기 포지션을 "외부 매도"로 오판·
    # 원장 삭제(2026-07 분기 인시던트). 이제 기본 콜백만으로도 정규화된다.
    r = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "코스피200선물" and pos["contract_code"] == "101V6000"
    assert "symbol_unmapped" not in pos


def test_injected_dataset_for_code_normalizes():
    r = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s,
                     dataset_for_code=lambda c: "코스피200선물" if c.startswith("101") else None)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "코스피200선물" and pos["contract_code"] == "101V6000"


class _FutUnknown:
    domestic_configured = True
    overseas_configured = False
    def account_snapshot(self):
        # 옵션 등 카탈로그 미등록 코드 — 역매핑 불가 케이스
        return {"account": {}, "positions": [{"symbol": "201T9000", "side": "long", "qty": 1}]}


def test_unmapped_code_flagged_not_silent():
    # I1 — 정규화 실패는 조용히 잔류하지 않고 symbol_unmapped 표식을 남긴다(원시 코드는
    # 유지해 웹 표시·external 집계 계속). 이 표식이 reconcile fail-safe(파괴 차단)의 신호.
    r = BrokerRouter(_Stock(), _FutUnknown(), resolve=lambda s: s)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "201T9000" and pos.get("symbol_unmapped") is True
