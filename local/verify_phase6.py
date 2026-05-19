"""Phase 6 검증 - 로컬앱 모의투자 완성도.

청산 규칙(익절·손절·트레일링·보유기간·매도신호), 고아 포지션 청산,
단일 인스턴스 가드를 MockBroker로 자동 검증한다. 백엔드 불필요.
"""

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_TMP = Path(tempfile.mkdtemp(prefix="qp_verify6_"))
os.environ["QP_LOCAL_DIR"] = str(_TMP)
os.environ["QP_CORE_DATA_DIR"] = str(Path(__file__).resolve().parent.parent / "core" / "data")

import localapp  # noqa: E402, F401
import quant_core as qc  # noqa: E402
from localapp import single_instance  # noqa: E402
from localapp.broker import MockBroker  # noqa: E402
from localapp.config import LEDGER_PATH, EQUITY_PATH  # noqa: E402
from localapp.trader import Trader  # noqa: E402

D0 = date(2026, 1, 5)
SYM = "S&P500"
# price_level > 0 : 항상 참 → 결정적 매수 신호
ALWAYS = {"symbol": SYM, "indicator": "price_level", "op": ">", "value": 0}


def _strategy(exit_rules: dict, sell=None) -> dict:
    d = {"name": "검증전략", "trade_symbol": SYM,
         "buy": {"conditions": [ALWAYS], "logic": "AND"},
         "exit_rules": exit_rules, "amount_pct": 100.0}
    if sell:
        d["sell"] = sell
    return d


def _fresh_trader(box: dict) -> Trader:
    LEDGER_PATH.unlink(missing_ok=True)
    EQUITY_PATH.unlink(missing_ok=True)
    broker = MockBroker(10_000_000, lambda s: box["px"])
    return Trader(broker)


def scenario(label, exit_rules, price_path, expect, days, dataset, sell=None,
             drop_strategy_at_exit=False):
    """매수 후 가격을 움직여 기대한 청산 사유가 나오는지 검증."""
    box = {"px": price_path[0]}
    trader = _fresh_trader(box)
    sdef = _strategy(exit_rules, sell)
    item = {"id": 1, "definition": sdef}

    p = trader.cycle([item], dataset, today=D0)
    assert any(t["action"] == "buy" for t in p["trades"]), f"{label}: 매수 실패"

    last = None
    for i, px in enumerate(price_path[1:], start=1):
        box["px"] = px
        strategies = [] if drop_strategy_at_exit else [item]
        last = trader.cycle(strategies, dataset, today=D0 + timedelta(days=days[i]))

    sells = [t for t in last["trades"] if t["action"] == "sell"]
    assert sells, f"{label}: 청산 안 됨 (trades={last['trades']})"
    assert sells[0]["reason"] == expect, \
        f"{label}: 청산사유 '{sells[0]['reason']}' != 기대 '{expect}'"
    print(f"   OK  {label} → {expect}")


def main():
    print("Phase 6 검증 — 로컬앱 모의투자 완성도\n")
    dataset = qc.load_dataset(with_indicators=True)
    print(f"데이터셋 {len(dataset)}심볼 로드\n")

    print("1) 청산 규칙 (백테스트와 동일 우선순위)")
    scenario("익절(+15%, TP 10%)", {"take_profit": 10.0},
             [100.0, 115.0], "익절", [0, 0], dataset)
    scenario("손절(-12%, SL -8%)", {"stop_loss": -8.0},
             [100.0, 88.0], "손절", [0, 0], dataset)
    scenario("트레일링(고점 130 후 115, 10%)", {"trail_pct": 10.0},
             [100.0, 130.0, 115.0], "트레일링스톱", [0, 0, 0], dataset)
    scenario("보유기간(5일)", {"hold_days": 5},
             [100.0, 100.0], "보유기간", [0, 6], dataset)
    scenario("매도신호(sell 조건 충족)", {},
             [100.0, 100.0], "매도신호", [0, 0], dataset,
             sell={"conditions": [ALWAYS], "logic": "AND"})

    print("\n2) 고아 포지션 — 플랫폼에서 전략이 삭제돼도 저장된 규칙으로 청산")
    scenario("고아 포지션 보유기간 청산", {"hold_days": 3},
             [100.0, 100.0], "보유기간", [0, 5], dataset,
             drop_strategy_at_exit=True)

    print("\n3) 단일 인스턴스 가드")
    assert single_instance.acquire() is True, "최초 잠금 실패"
    # 2번째 프로세스를 흉내 — 같은 잠금 파일을 다른 핸들로 잠그면 거부돼야 함
    import localapp.config as cfg
    blocked = False
    h = open(cfg.APP_DIR / "localapp.lock", "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        blocked = True
    finally:
        h.close()
    assert blocked, "2번째 인스턴스가 차단되지 않음"
    single_instance.release()
    print("   OK  2번째 인스턴스 차단 (이중 주문 방지)")

    print("\n4) 파일 로깅·체결로그")
    assert (cfg.APP_DIR / "trades.jsonl").exists(), "체결 로그 미생성"
    print(f"   OK  체결 로그 기록됨: {cfg.APP_DIR / 'trades.jsonl'}")

    print("\n[OK] Phase 6 검증 통과 - 로컬앱 모의투자 완성도 정상")


if __name__ == "__main__":
    main()
