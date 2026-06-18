"""주문 이벤트 로그 + 사이클 의사결정 로그 + 슬리피지 통계.

- orders.jsonl: 주문 단위 이벤트 (submitted/filled/cancelled/rejected)
- cycles.jsonl: 사이클 단위 요약 (어떤 전략이 진입·스킵됐고 사유는)
- slippage.json: 최근 N건의 (intended, fill, bps) 누적 통계

Phase 48 P2-A — 로그 보존 정책:
모든 jsonl은 append-only이며 자동 rotation·archive를 수행하지 않는다. 자본시장법
관련 행정처분 시효(5년) + FINRA Reg Notice 15-09 권장(최소 5년)을 충족하도록
**사용자가 직접 백업·삭제**한다. 운영자가 자동 archival을 도입하려면 5년 retention을
명시한 별도 정책 필요. orders.jsonl/cycles.jsonl은 텍스트라 1년 운영 누적도
수십 MB 수준 — 자동 rotation 없이도 일반 PC 디스크에 안전 보관 가능하다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import CYCLES_PATH, ORDERS_PATH, SLIPPAGE_PATH, TRADES_PATH
from .state_store import append_jsonl, save_json

log = logging.getLogger("localapp.orderlog")

_SLIPPAGE_KEEP = 100   # 최근 N건만 유지
_KST = ZoneInfo("Asia/Seoul")


def _now() -> str:
    # 로컬 저널 타임존 단일화(KST) — orders/cycles만 UTC라 intents.jsonl(+09:00)과
    # 섞여 사람이 9시간 오독하던 불일치 정리(리뷰 D6-3, 실제 진단 오독 발생).
    # 파서들(catchup._entry_ts 등)은 tz-aware iso만 가정하므로 호환.
    return datetime.now(_KST).isoformat()


def _append(path, obj: dict) -> None:
    """orders/cycles 한 줄 append — state_store 위임 (R5, 최초 생성 시 owner-only ACL).

    원시 주문 이벤트(종목·수량·체결가)는 같은 PC 타 사용자에게 노출되면 안 된다.
    로그 기록은 best-effort — 실패해도 매매 사이클을 중단시키지 않는다(체결 진실은
    intents.jsonl·KIS 주문조회이고 이건 보조 로그). fsync 불필요(L-01 대상 아님).
    """
    try:
        append_jsonl(obj, path)
    except Exception as e:
        log.warning("로그 기록 실패 [%s]: %s", path.name, e)


# ── 주문 이벤트 ────────────────────────────────────────────────────────────────

def log_order(event: str, symbol: str, side: str, qty: int,
              order_no: str = "", intended_price: float | None = None,
              limit_price: float | None = None, fill_price: float | None = None,
              strategy_name: str = "", reason: str = "",
              extra: dict | None = None) -> None:
    """주문 이벤트 한 건을 orders.jsonl에 append.

    event: submitted | filled | partial | cancelled | rejected | timeout
    """
    row = {
        "ts": _now(), "event": event, "side": side, "symbol": symbol,
        "qty": int(qty), "order_no": order_no,
        "intended_price": intended_price, "limit_price": limit_price,
        "fill_price": fill_price, "strategy": strategy_name, "reason": reason,
    }
    if extra:
        row.update(extra)
    _append(ORDERS_PATH, row)
    # 체결 이벤트면 슬리피지 누적
    if event == "filled" and intended_price and fill_price and intended_price > 0:
        record_slippage(side, symbol, intended_price, fill_price)


def read_orders(limit: int = 100) -> list[dict]:
    """최근 N건의 주문 이벤트 읽기. 신규 → 과거 순."""
    if not ORDERS_PATH.exists():
        return []
    try:
        lines = ORDERS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
        if len(rows) >= limit:
            break
    return rows


# ── 실현손익 조인 (주문 내역 화면용) ────────────────────────────────────────────
# realized_pnl의 진실원천은 trades.jsonl(청산 정산)이다. orders.jsonl(주문 이벤트)에는
# 손익이 없으므로, 주문 내역 화면이 체결행에 손익을 보이려면 둘을 조인한다. 두 파일은
# trader._apply_fill의 같은 호출에서 같은 (날짜·side·종목·수량·체결가)로 기록돼 키가
# 일치한다. realized_pnl은 청산(선물 정산·숏 환매)에만 존재 — 진입·주식매도엔 없어
# 그런 행은 손익이 매핑에 없다(화면에서 '—'). 표시 전용이라 트레이딩·락 경로 무변경.

def _trade_key(date: str, side: str, symbol: str, qty, price) -> tuple:
    """orders.jsonl 체결행 ↔ trades.jsonl 거래를 잇는 조인 키.

    date는 ISO(orders) 또는 'YYYY-MM-DD'(trades) 어느 쪽이든 앞 10자만 쓴다."""
    try:
        p = round(float(price), 4)
    except (TypeError, ValueError):
        p = None
    try:
        q = int(qty)
    except (TypeError, ValueError):
        q = qty
    return ((date or "")[:10], side, symbol, q, p)


def realized_pnl_index() -> dict:
    """trades.jsonl을 1회 스캔해 조인키→realized_pnl 매핑을 만든다.

    청산 거래에만 realized_pnl이 있어 그 외 거래는 매핑에 없다(주문 내역 손익 '—').
    파일 부재·파싱 실패는 빈 dict/skip — 화면 갱신을 깨지 않는다(best-effort)."""
    if not TRADES_PATH.exists():
        return {}
    try:
        lines = TRADES_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    idx: dict[tuple, float] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except Exception:
            continue
        pnl = t.get("realized_pnl")
        side = t.get("action")
        if pnl is None or side not in ("buy", "sell"):
            continue
        idx[_trade_key(t.get("ts", ""), side, t.get("symbol", ""),
                       t.get("qty", 0), t.get("price"))] = pnl
    return idx


# ── 사이클 의사결정 ────────────────────────────────────────────────────────────

def log_cycle(decisions: list[dict], summary: dict) -> None:
    """1회 사이클 의사결정·결과 요약."""
    _append(CYCLES_PATH, {"ts": _now(), "decisions": decisions, "summary": summary})


def read_cycles(limit: int = 20) -> list[dict]:
    if not CYCLES_PATH.exists():
        return []
    try:
        lines = CYCLES_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
        if len(rows) >= limit:
            break
    return rows


# ── 슬리피지 통계 ──────────────────────────────────────────────────────────────

def _load_slip() -> dict:
    if not SLIPPAGE_PATH.exists():
        return {"samples": []}
    try:
        return json.loads(SLIPPAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"samples": []}


def _save_slip(d: dict) -> None:
    save_json(SLIPPAGE_PATH, d)


def record_slippage(side: str, symbol: str, intended: float, fill: float) -> None:
    """체결 1건의 슬리피지를 bps 단위로 누적."""
    if intended <= 0:
        return
    # 매수: 체결가 > 의도 → 양의 슬리피지(불리). 매도: 체결가 < 의도 → 양의 슬리피지.
    sign = 1 if side == "buy" else -1
    bps = sign * (fill - intended) / intended * 10_000
    d = _load_slip()
    d["samples"].append({"ts": _now(), "side": side, "symbol": symbol,
                          "intended": intended, "fill": fill, "bps": round(bps, 2)})
    d["samples"] = d["samples"][-_SLIPPAGE_KEEP:]
    _save_slip(d)


def slippage_stats() -> dict:
    """평균·중앙값·p95 슬리피지 bps."""
    d = _load_slip()
    samples = d.get("samples", [])
    n = len(samples)
    if n == 0:
        return {"n": 0, "avg_bps": None, "p50_bps": None, "p95_bps": None,
                "max_bps": None, "recent": []}
    bps_list = sorted(s["bps"] for s in samples)
    avg = sum(bps_list) / n
    p50 = bps_list[n // 2]
    p95 = bps_list[min(n - 1, int(n * 0.95))]
    return {
        "n": n, "avg_bps": round(avg, 2), "p50_bps": round(p50, 2),
        "p95_bps": round(p95, 2), "max_bps": round(bps_list[-1], 2),
        "recent": list(reversed(samples[-10:])),
    }


# ── Decision helper (사이클 빌더용) ────────────────────────────────────────────

def decision(action: str, strategy_id: str | int = "", strategy_name: str = "",
             symbol: str = "", reason: str = "", extra: dict | None = None) -> dict:
    """사이클 의사결정 한 줄을 만든다.

    action: bought | sold | skip_gap | skip_killswitch | skip_signal | skip_funds
            | skip_held | unfilled | rejected | error
    """
    row = {"action": action, "strategy_id": str(strategy_id),
           "strategy_name": strategy_name, "symbol": symbol, "reason": reason}
    if extra:
        row.update(extra)
    return row


def iter_open_orders(orders: Iterable[dict]) -> list[dict]:
    """orders.jsonl 이벤트들로부터 현재 미체결 상태인 주문만 추려낸다."""
    by_no: dict[str, dict] = {}
    for o in orders:
        no = o.get("order_no", "")
        if not no:
            continue
        by_no[no] = o
    return [o for o in by_no.values()
            if o.get("event") in ("submitted", "partial")]
