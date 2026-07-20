"""N8 — catch-up 손절 배너가 사용자에게 **거짓 신호**를 주던 결함.

발단은 "문구 오독"(유저가 `KRX stop-loss`를 '손절 발생'으로 읽음)이었지만, 조사해보니
숫자 자체가 틀렸다. 문구만 고치면 거짓 신호에 더 그럴듯한 라벨을 붙이는 셈이다.

  ① `fired`가 '발주된 손절 건수'가 아니라 '매니저 decisions 증가분'이다.
     양방향으로 틀린다 — 접수됐지만 미체결인 매도는 decision을 남기지 않아 **0으로
     세고**(→ "✓ 손절선 안전"이라는 허위 안전신호), 멱등 차단·발주 예외·거부는
     decision을 남겨 **발주로 센다**(→ "🔴 N건 손절 발주"인데 실제 주문 0건).
  ② `checked`가 '평가한 건수'가 아니라 '평가하려던 건수'다. 현재가 조회가 전건
     실패해도 보유수 그대로라, 실제 손절 평가 0건인데 "보유 5건 → ✓ 손절선 안전"이
     표시된다(판정 불가를 정상으로 접는 패턴).
  ③ 계획 요약 `__str__`이 `krx_close_classes`를 통째로 빠뜨려, 종가창 catch-up만
     있는 계획이 "(none)"으로 출력된다 — 로그·배너가 "할 일 없음"이라 말하는 동안
     실제로는 종가 청산 주문을 낸다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

_SYM = "005930"


def _stop_loss_def():
    return {"name": "손절전략", "engine": "ir",
            "universe": {"kind": "single", "symbols": [_SYM]},
            "signal": {"op": "compare", "params": {"op": ">"},
                       "inputs": {"left": {"op": "data",
                                           "params": {"ref": "__SELF__.Close"}},
                                  "right": {"op": "const", "params": {"value": 0}}}},
            "position": {"direction": "long",
                         "sizing": {"mode": "pct_cash", "amount_pct": 10},
                         "entry": {"mode": "on_signal"},
                         "exit": {"stop_loss": -5.0}, "overlays": {}},
            "simulation": {}}


def _held(t, broker, qty=10, entry=70000.0):
    t.ledger["s1"] = {"symbol": _SYM, "qty": qty, "side": "long",
                      "entry_price": entry, "peak_price": entry,
                      "entry_date": "2026-05-29", "strategy_name": "손절전략",
                      "definition": _stop_loss_def()}
    broker.set_positions([{"symbol": _SYM, "qty": qty}])


def test_fired_counts_actual_orders_not_decisions(isolated_trader):
    """손절이 실제 발주되면 fired=1 — 미체결이라 decision이 안 남아도.

    종전엔 `len(manager.decisions)` 증가분을 셌는데, 접수만 되고 체결 전이면
    decision이 없어 fired=0 → 배너가 "✓ 손절선 안전"을 띄웠다(허위 안전신호).
    """
    from localapp import catchup
    t, broker = isolated_trader
    _held(t, broker)
    broker._prices[_SYM] = 66000        # −5.7% → 손절선 아래

    res = catchup._catchup_stop_loss("KRX", broker, t)

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1, f"전제: 손절 매도가 나가야 한다 — {broker.submitted}"
    assert res["fired"] == 1, (
        f"실제 발주 1건인데 fired={res['fired']} — 배너가 '손절선 안전'을 띄운다")


def test_fired_does_not_count_idempotent_block_as_order(isolated_trader):
    """멱등 게이트에 막혀 **주문이 안 나간** 건은 fired로 세지 않는다.

    종전엔 skip_idempotent decision이 추가돼 fired=1이 됐다 — 주문 0건인데
    "🔴 1건 손절 발주".
    """
    from localapp import catchup, intents
    from localapp.trader import kst_today
    t, broker = isolated_trader
    _held(t, broker)
    broker._prices[_SYM] = 66000
    # 이미 오늘 같은 (sid, symbol, sell) intent가 활성 — 게이트가 막는다.
    iid = intents.new_intent_id()
    today = kst_today().isoformat()
    intents.begin(today, iid, "s1", "손절전략", _SYM, "sell", 10, 70000.0)
    intents.mark_submitted(today, iid, "PRE-1")

    res = catchup._catchup_stop_loss("KRX", broker, t)

    assert [s for s in broker.submitted if s["side"] == "sell"] == []
    assert res["fired"] == 0, (
        f"주문이 0건인데 fired={res['fired']} — 배너가 손절 발주를 허위 보고")


def test_checked_counts_evaluated_not_intended(isolated_trader):
    """현재가를 못 얻어 평가 자체를 못 한 건은 checked에서 빠지고 skipped로 보인다.

    종전엔 checked=len(positions)라, 전건 조회 실패해도 "보유 N건 → ✓ 손절선 안전"이
    떴다. '모른다'를 '괜찮다'로 접는 부류.
    """
    from localapp import catchup
    t, broker = isolated_trader
    _held(t, broker)
    broker._prices.pop(_SYM, None)

    def _boom(sym):
        raise RuntimeError("현재가 조회 실패")
    broker.price = _boom

    res = catchup._catchup_stop_loss("KRX", broker, t)

    assert res["checked"] == 0, (
        f"평가 0건인데 checked={res['checked']} — 안전신호를 허위로 만든다")
    assert res["skipped"] == 1, "판정 불가 건수가 보여야 한다"


def test_plan_str_includes_close_window_classes():
    """계획 요약이 종가창 catch-up을 빠뜨리면 '할 일 없음'으로 오독된다."""
    from localapp.catchup import CatchupPlan

    plan = CatchupPlan()
    plan.krx_close_classes = ["futures"]
    assert plan.has_any() is True
    assert str(plan) != "(none)", "종가창 catch-up이 계획 요약에서 통째로 누락"
    assert "close" in str(plan).lower() or "종가" in str(plan)


# ── 배너 문구 — 유저에게 실제로 닿는 표면 ─────────────────────────────────
def _summary(results: dict) -> str:
    """GUI 인스턴스 없이 _format_catchup_summary만 구동(Tk 의존 회피)."""
    from localapp.gui import SettingsApp
    return SettingsApp._format_catchup_summary(object(), {"results": results})


def test_banner_separates_check_from_fire():
    """'손절 점검 N건 → 발동 M건' — 점검과 발동이 분리돼야 오독이 없다.

    실측 2026-07-20: 종전 `KRX stop-loss` 표기를 유저가 '손절 발생'으로 읽었다.
    """
    out = _summary({"krx_stop_loss": {"checked": 5, "skipped": 0, "fired": 0}})
    assert "점검 5건" in out and "발동 없음" in out
    out2 = _summary({"krx_stop_loss": {"checked": 5, "skipped": 0, "fired": 2}})
    assert "발동 2건" in out2


def test_banner_surfaces_unevaluable_positions():
    """판정 불가(현재가 조회 실패)를 '안전'으로 접지 않고 드러낸다."""
    out = _summary({"krx_stop_loss": {"checked": 0, "skipped": 3, "fired": 0}})
    assert "판정불가 3건" in out, f"판정 불가가 숨겨짐: {out!r}"


def test_banner_shows_close_window_catchup():
    """종가창 catch-up 결과가 배너에서 통째로 비가시이던 것 — 종가 청산을 냈는데
    유저는 아무것도 못 봤다."""
    out = _summary({"krx_close_futures": {"n_bought": 0, "n_sold": 1, "n_netted": 0}})
    assert "krx_close_futures" in out and "매도 1건" in out


def test_intraday_hook_preserves_submit_result(isolated_trader, monkeypatch):
    """🔴 장중 loop의 매도 hook이 `_submit_sell` 반환값을 삼키면 N8이 통째로 무효다.

    intraday_loop은 push 훅을 걸려고 `trader._submit_sell`을 래퍼로 교체한다.
    그 래퍼가 원 함수 반환값을 버리면 항상 None → `placed`가 falsy →
    `submitted_count`가 영영 0. 그리고 영향은 장중 loop에 그치지 않는다 —
    catch-up이 `trader._submit_sell`을 그대로 넘겨받으므로(catchup.py:529),
    loop가 도는 08:30~15:45 내내 **catch-up 배너의 발주 수까지** 무효가 된다.

    프로덕션 래퍼(`make_sell_hook`)를 직접 구동한다 — 재현물이 아니라.
    """
    from localapp import intraday_loop
    t, broker = isolated_trader
    _held(t, broker)
    broker._prices[_SYM] = 66000
    monkeypatch.setattr(intraday_loop, "_push_after_sell",
                        lambda b, d: None, raising=False)

    class _Mgr:
        decisions: list = []

    hook = intraday_loop.make_sell_hook(t, broker, _Mgr(), t._submit_sell)
    placed = hook("s1", "손절전략", _SYM, 10, 70000.0,
                  {"sell_tolerance_pct": 1.0}, "손절", [])
    assert placed is True, "래퍼가 발주 성공 여부를 삼켰다 — N8 집계가 무효화된다"


def test_intraday_hook_reports_blocked_submit_as_not_placed(isolated_trader, monkeypatch):
    """멱등 차단으로 주문이 안 나갔으면 래퍼도 False를 전달한다."""
    from localapp import intents, intraday_loop
    from localapp.trader import kst_today
    t, broker = isolated_trader
    _held(t, broker)
    broker._prices[_SYM] = 66000
    monkeypatch.setattr(intraday_loop, "_push_after_sell",
                        lambda b, d: None, raising=False)
    iid = intents.new_intent_id()
    today = kst_today().isoformat()
    intents.begin(today, iid, "s1", "손절전략", _SYM, "sell", 10, 70000.0)
    intents.mark_submitted(today, iid, "PRE-1")

    class _Mgr:
        decisions: list = []

    hook = intraday_loop.make_sell_hook(t, broker, _Mgr(), t._submit_sell)
    assert hook("s1", "손절전략", _SYM, 10, 70000.0,
                {"sell_tolerance_pct": 1.0}, "손절", []) is False
