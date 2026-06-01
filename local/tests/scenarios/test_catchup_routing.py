"""REV-②(a) 회귀 — catch-up 손절의 시장 분류가 한 종목 실패로 전체 abort되지 않는다.

기존 catch-up은 raw market_group_of를 list comprehension에서 호출해, 마스터 미로드 등으로
한 종목이 RoutingError를 던지면 보유 전 종목의 손절 평가가 통째로 abort됐다. EOD cycle과
동일하게 _market_group_safe(미해결 시 KRX 기본)로 일관화한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def test_catchup_routing_failure_does_not_abort(isolated_trader, monkeypatch):
    from localapp import catchup, market_index

    t, broker = isolated_trader
    orig = market_index.market_group_of

    def boom(sym):
        if sym == "BADX":
            raise RuntimeError("RoutingError: master not loaded")
        return orig(sym)
    monkeypatch.setattr(market_index, "market_group_of", boom)

    broker.set_positions([{"symbol": "BADX", "qty": 1},
                          {"symbol": "005930", "qty": 10}])
    broker._prices["005930"] = 70000

    # 한 종목 라우팅 실패가 catch-up 전체를 abort(예외 전파)시키면 안 된다.
    res = catchup._catchup_stop_loss("KRX", broker, t)
    assert res["error"] is None, f"catch-up이 라우팅 실패로 abort됨: {res}"
    assert res["checked"] >= 1, "정상 종목이 평가 대상에서 누락됨"
