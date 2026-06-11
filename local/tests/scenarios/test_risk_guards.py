"""Workbench 시나리오 — 리스크·안전 가드(A7).

발주 직전 가격 클램프(INV-PRICE-1)를 실제 _submit_buy 경로로 검증한다. full cycle
없이 발주 helper만 구동(intents·pending 모두 tmp 격리).
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def test_buy_limit_clamped_to_daily_price_limit(isolated_trader, monkeypatch):
    """INV-PRICE-1: 과도한 tolerance여도 매수 지정가는 상한가로 사전 클램프.

    가격 평면 정책(2026-06-11): 클램프 기준은 자체 ±30% 재계산이 아니라 **브로커
    시세 응답의 거래소 상한가**(quote_band.upper) — 자산군별 밴드(주식 ±30%·선물
    ±8%·신규상장 ±60%)가 자동으로 정확. tolerance 100%면 limit이 현재가의 2배로
    가려 하지만 거래소 상한가로 잘려야 한다.
    """
    t, broker = isolated_trader
    ref = 70000.0
    upper = 91000.0                                            # 거래소 상한가(=+30%)
    monkeypatch.setattr(broker, "quote_band",
                        lambda s: {"price": ref, "upper": upper, "lower": None})
    policy = {"use_limit": True, "buy_tolerance_pct": 100.0,
              "sell_tolerance_pct": 1.0}                       # 의도적 과도 tolerance

    t._submit_buy("s1", "T", {}, "005930", 10, ref, policy, [])

    limit = broker.submitted[-1]["limit"]
    assert limit == upper, \
        f"INV-PRICE-1: 거래소 상한가 클램프 실패 limit={limit} (기대 {upper})"


def test_killswitch_blocks_new_buys(isolated_trader):
    """INV-KS-1: kill switch active면 cycle 진입 패스가 차단돼 신규 매수가 0이다.

    진입 패스는 _enter_from_preview 직전 ks_active 게이트로 막힌다(보유 청산은 별개).
    ledger·dataset 비어도 게이트가 buy_candidates 처리 전에 차단하므로 현실적 시세
    셋업 없이 검증 가능.
    """
    from localapp import killswitch
    t, broker = isolated_trader
    killswitch.activate("테스트 — 일일손실 한도 도달")
    assert killswitch.is_active()

    candidates = [{"strategy_id": "s1",
                   "candidates": [{"symbol": "005930"}],
                   "screener_members": ["005930"]}]
    payload = t.cycle(strategies=[], dataset={}, buy_candidates=candidates,
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")

    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert buys == [], f"INV-KS-1 위반: killswitch active인데 매수 발주됨 {buys}"
    assert any(d["action"] == "skip_killswitch" for d in payload["decisions"]), \
        "skip_killswitch 결정이 기록되지 않음"


def test_killswitch_liquidates_parse_failed_orphan(isolated_trader):
    """REV-③#4: kill switch 발동 시 정의 파싱 불가한 고아 포지션도 강제 청산된다.

    이전엔 _exit_reason_for 파싱 예외에서 곧장 continue해 ks 강제청산 분기를 건너뛰어,
    삭제/손상 전략의 고아 포지션이 kill switch 중에도 청산 안 되고 남았다."""
    from localapp import killswitch
    t, broker = isolated_trader
    # 정의 파싱 불가(빈/손상 IR)한 고아 포지션을 ledger에 직접 주입
    t.ledger["orphan"] = {
        "symbol": "005930", "qty": 7, "entry_date": "2026-06-01",
        "entry_price": 70000, "peak_price": 70000,
        "strategy_name": "삭제된전략", "definition": {"engine": "ir"},  # 필수필드 결여 → 파싱 실패
    }
    broker.set_positions([{"symbol": "005930", "qty": 7}])
    broker._prices["005930"] = 70000
    killswitch.activate("테스트 — 전량 청산")

    t.cycle(strategies=[], dataset={}, buy_candidates=[],
            risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == 7, \
        f"kill switch가 파싱실패 고아를 청산 안 함: {sells}"
