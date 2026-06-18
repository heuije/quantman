"""주문 내역 손익 표시 — orders.jsonl 체결행 ↔ trades.jsonl realized_pnl 조인.

realized_pnl의 진실원천은 trades.jsonl(청산 정산)이고 orders.jsonl엔 없다. 화면이
체결행에 손익을 보이려면 둘을 조인하는데, 두 파일은 같은 _apply_fill 호출에서 같은
(날짜·side·종목·수량·체결가)로 기록되므로 키가 일치한다. 청산만 realized_pnl을 가지므로
진입·주식매도·external_close는 매핑에서 빠져 화면에서 '—'로 귀결된다.
"""
import json

from localapp import order_log


def _write_trades(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")


def test_index_maps_only_settled_closes(tmp_path, monkeypatch):
    trades = tmp_path / "trades.jsonl"
    _write_trades(trades, [
        # 신규 진입(롱) — realized_pnl 없음 → 제외
        {"ts": "2026-06-18", "action": "buy", "symbol": "코스피200선물",
         "qty": 2, "price": 1425.725, "reason": "매수신호"},
        # 선물 당일청산 — realized_pnl 있음 → 매핑
        {"ts": "2026-06-18", "action": "sell", "symbol": "코스피200선물",
         "qty": 2, "price": 1470.1, "reason": "당일청산", "realized_pnl": 22187500.0},
        # realized_pnl 없는 거래(진입·소급 안 된 과거 기록 등) → 제외
        {"ts": "2026-06-17", "action": "sell", "symbol": "GOOG",
         "qty": 263, "price": 355.4, "reason": "당일청산"},
        # external_close(reconcile 추정) — side가 buy/sell 아님 → 제외(설사 pnl 있어도)
        {"ts": "2026-06-12", "action": "external_close", "symbol": "GOOG",
         "qty": 263, "price": 353.2, "realized_pnl": 12345.0},
    ])
    monkeypatch.setattr(order_log, "TRADES_PATH", trades)

    idx = order_log.realized_pnl_index()

    assert idx == {("2026-06-18", "sell", "코스피200선물", 2, 1470.1): 22187500.0}


def test_order_filled_event_joins_to_pnl(tmp_path, monkeypatch):
    trades = tmp_path / "trades.jsonl"
    _write_trades(trades, [
        {"ts": "2026-06-17", "action": "buy", "symbol": "코스피200선물",
         "qty": 2, "price": 1428.85, "reason": "숏청산", "realized_pnl": -25350000.0},
    ])
    monkeypatch.setattr(order_log, "TRADES_PATH", trades)
    idx = order_log.realized_pnl_index()

    # orders.jsonl 체결행은 전체 ISO ts·fill_price 키를 쓴다 — 같은 키로 조인돼야 한다.
    key = order_log._trade_key(
        "2026-06-17T15:50:04.006016+09:00", "buy", "코스피200선물", 2, 1428.85)
    assert idx.get(key) == -25350000.0


def test_missing_or_unparseable_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(order_log, "TRADES_PATH", tmp_path / "nope.jsonl")
    assert order_log.realized_pnl_index() == {}

    bad = tmp_path / "trades.jsonl"
    bad.write_text("{not json}\n\n", encoding="utf-8")  # 깨진 줄·공백 — skip, 안 죽음
    monkeypatch.setattr(order_log, "TRADES_PATH", bad)
    assert order_log.realized_pnl_index() == {}


def test_trade_key_tolerates_missing_numbers():
    # 가격·수량 결측/비수치도 예외 없이 일관된 키(조인 실패 → 화면 '—'로 안전 귀결).
    assert order_log._trade_key(
        "2026-06-18T00:00:00+09:00", "buy", "X", None, None
    ) == ("2026-06-18", "buy", "X", None, None)
