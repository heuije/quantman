"""N1 회귀 — 같은 전략이 하루에 두 번 같은 방향으로 청산할 수 있는가.

문제(2026-07-20 실증, floo.japan1 #30 코스닥150선물):
  08:35 매도13이 **체결**됐는데도 intent 저널엔 성공 종결(phase)이 없어 종일
  `submitted`(=활성)로 남았다. 15:40 당일매매 청산 매도438이 `is_active`에 걸려
  `_plan_exit_intents`에서 **심볼 통째 hold** → 438계약이 오버나이트로 넘어갔다.

근본 원인: 저널이 intent의 *끝*을 기록하지 않는다. `submitted`는 "브로커가 접수했다"
이지 "아직 살아있다"가 아닌데 게이트가 후자로 읽었다. 해법은 주문이 죽은 시점
(체결·취소·소멸 = pending에서 사라지는 지점)에 `resolved`를 append하는 것.

여기서 잠그는 계약:
  - 1회차 청산이 **체결**되면 2회차 청산이 실제로 발주된다(N1 해소).
  - 1회차 청산이 **미체결로 살아있으면** 2회차는 여전히 차단된다(L-01 유지).
  - 취소로 끝난 주문도 게이트를 푼다(주문이 죽었으므로).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import intents
from sim import scenario

_POLICY = {"buy_tolerance_pct": 1.0, "sell_tolerance_pct": 1.0}
_SYM = "005930"
_SID = "s1"
_TODAY = "2026-06-01"


def _sells(broker) -> list:
    return [s for s in broker.submitted if s["side"] == "sell"]


def test_second_exit_after_first_filled_is_submitted(isolated_trader):
    """japan1 재현 — 1회차 청산 체결 후 2회차 청산이 발주된다.

    수정 전에는 `is_active`가 체결된 intent를 활성으로 봐 2회차가 차단됐다.
    """
    t, broker = isolated_trader
    broker._prices[_SYM] = 70000.0

    # 1회차 — 보유 → 청산 발주 → WS 전량 체결(주문이 죽음)
    scenario.buy_and_fill(t, broker, _SID, _SYM, 13, 70000.0)
    t._submit_sell(_SID, "T", _SYM, 13, 70000.0, _POLICY, "1회차 청산", [])
    o1 = _sells(broker)[-1]["order_no"]
    scenario.inject_ws_fill(t, broker, o1, 13, 70000.0)
    assert o1 not in t.pending, "전량 체결이면 pending에서 회수돼야 한다(전제)"

    # 체결로 죽은 주문은 더 이상 '활성 intent'가 아니다
    assert intents.is_active(_TODAY, _SID, _SYM, "sell") is False, \
        "체결로 종결된 intent가 여전히 활성 — N1 미해소"

    # 2회차 — 같은 전략·심볼·방향
    scenario.buy_and_fill(t, broker, _SID, _SYM, 438, 70000.0)
    n_before = len(_sells(broker))
    t._submit_sell(_SID, "T", _SYM, 438, 70000.0, _POLICY, "2회차 청산", [])

    assert len(_sells(broker)) == n_before + 1, \
        "1회차가 체결됐는데도 2회차 청산이 차단됨 — 438 오버나이트 재발"
    assert _sells(broker)[-1]["qty"] == 438


def test_second_exit_blocked_while_first_still_live(isolated_trader):
    """L-01 유지 — 1회차가 **미체결로 살아있으면** 2회차는 차단된다.

    이게 게이트의 존재 이유다. 체결 종결이 이 보호를 풀어서는 안 된다.
    """
    t, broker = isolated_trader
    broker._prices[_SYM] = 70000.0
    scenario.buy_and_fill(t, broker, _SID, _SYM, 13, 70000.0)

    t._submit_sell(_SID, "T", _SYM, 13, 70000.0, _POLICY, "1회차 청산", [])
    n_after_first = len(_sells(broker))
    assert intents.is_active(_TODAY, _SID, _SYM, "sell") is True

    # 체결 주입 없음 — 주문이 브로커에 살아있는 상태
    dec: list = []
    t._submit_sell(_SID, "T", _SYM, 13, 70000.0, _POLICY, "2회차 청산", dec)

    assert len(_sells(broker)) == n_after_first, "미체결 중인데 이중 매도 발주"
    assert [d for d in dec if d["action"] == "skip_idempotent"]


def test_cancelled_order_releases_gate(isolated_trader):
    """취소로 끝난 주문도 게이트를 푼다 — 주문이 죽었으므로 재발주가 정당하다."""
    t, broker = isolated_trader
    broker._prices[_SYM] = 70000.0
    scenario.buy_and_fill(t, broker, _SID, _SYM, 13, 70000.0)
    t._submit_sell(_SID, "T", _SYM, 13, 70000.0, _POLICY, "1회차 청산", [])
    o1 = _sells(broker)[-1]["order_no"]

    broker._statuses[o1].update(status="cancelled", filled_qty=0, remain_qty=0)
    t._resolve_pending([])
    assert o1 not in t.pending

    assert intents.is_active(_TODAY, _SID, _SYM, "sell") is False, \
        "취소 종결된 intent가 여전히 활성 — 정당한 재발주가 종일 차단됨"


def test_partial_fill_then_cancel_books_the_fill_before_terminating(isolated_trader):
    """🔴 취소·거부는 체결과 배타가 아니다 — 종결 전 미기장 체결분을 기장한다.

    부분체결 후 잔량이 장마감 자동취소되면 `order_status`가 status=cancelled와
    filled_qty>0을 함께 준다. 종전 분기는 filled를 통째로 무시하고 pending만 지워
    그 체결이 **영구 미기장**됐다 — 다음 사이클 목표수렴이 "안 팔린 줄 알고"
    되팔아 실손이 나는 부류(2026-07-14 drift 되팔기).

    N1이 게이트까지 풀면서 더 위험해졌다: 종전엔 pending 잔존이 우연히 그 오류를
    덮어줬는데(멱등 게이트 점유 = 재발주 차단), 이제는 덮이지 않는다.
    """
    t, broker = isolated_trader
    broker._prices[_SYM] = 70000.0
    scenario.buy_and_fill(t, broker, _SID, _SYM, 10, 70000.0)
    qty_before = t.ledger[_SID]["qty"]
    assert qty_before == 10

    t._submit_sell(_SID, "T", _SYM, 10, 70000.0, _POLICY, "청산", [])
    o1 = _sells(broker)[-1]["order_no"]
    # 4주만 체결되고 잔량 6은 장마감 자동취소 — 브로커는 둘을 함께 보고한다.
    broker._statuses[o1].update(status="cancelled", filled_qty=4,
                                remain_qty=0, fill_price=69000.0)
    t._resolve_pending([])

    assert o1 not in t.pending
    assert t.ledger[_SID]["qty"] == 6, (
        f"체결 4주가 기장되지 않았다(원장 {t.ledger[_SID]['qty']}) — 다음 사이클이 "
        "되팔아 실손")


def test_drift_gate_stays_locked_after_fill(isolated_trader):
    """🔴 drift 교정 게이트는 체결로 풀리면 안 된다 — 창 단위 1회가 불변식.

    drift 주문은 체결돼도 **원장을 바꾸지 않는다**(`_apply_fill` 초입 조기 return).
    그래서 다른 멱등 경로와 달리 "원장이 갱신돼 다음 계산에서 자연히 사라진다"는
    자기제한 피드백이 없다 — 재발주를 막는 건 오직 브로커 스냅샷이 diff 0을
    보여주는 것뿐이다. 그런데 scheduler.py의 개장후 수렴(선물 08:46) 주석이
    "개장 단일가 체결의 잔고 API 반영 지연이 크면 drift 오판 가능 — **멱등(저널
    게이트)이라 발주 안전은 유지**"라고 그 전제를 명문화하고 있다.

    N1이 체결 시 게이트를 풀면서 이 방어가 사라졌다: 08:35 drift buy 3이 체결 →
    게이트 해제 → 08:46 수렴이 아직 반영 안 된 잔고로 diff를 +3으로 재계산 →
    buy 3 재발주 → 초과 노출 3(다음 사이클이 되팔며 왕복 비용).
    키(`DRIFT:{window}`)가 이미 창을 담고 있어, N1이 풀려던 "하루 두 번 같은 방향
    청산"과는 애초에 다른 문제다.
    """
    t, broker = isolated_trader
    broker._prices[_SYM] = 70000.0
    dec: list = []
    t._submit_drift(_SYM, 3, "open", dec)

    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert len(buys) == 1, f"전제: drift 매수가 나가야 한다 — {broker.submitted}"
    o1 = buys[-1]["order_no"]
    scenario.inject_ws_fill(t, broker, o1, 3, 70000.0)
    assert o1 not in t.pending, "전제: 전량 체결로 pending에서 회수"

    assert intents.is_active(_TODAY, "DRIFT:open", _SYM, "buy") is True, \
        "drift 게이트가 체결로 풀렸다 — 반영 지연 잔고로 같은 교정이 재발주된다"

    # 같은 창 재실행에서 실제로 재발주가 차단되는지도 확인.
    n_before = len(broker.submitted)
    t._submit_drift(_SYM, 3, "open", dec)
    assert len(broker.submitted) == n_before, "같은 창 drift 교정이 이중 발주됨"


def test_drift_gate_is_still_per_window(isolated_trader):
    """단, 창이 다르면(open→close) 여전히 교정할 수 있어야 한다 — 키가 창을 담는다."""
    t, broker = isolated_trader
    broker._prices[_SYM] = 70000.0
    dec: list = []
    t._submit_drift(_SYM, 3, "open", dec)
    n_after_open = len(broker.submitted)
    t._submit_drift(_SYM, 3, "close", dec)
    assert len(broker.submitted) == n_after_open + 1, \
        "종가창 drift 교정이 아침창 게이트에 막혔다"
