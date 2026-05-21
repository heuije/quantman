"""사이클 후처리 분석 — Monitor에 노출할 집계 데이터.

- 전략별 P&L attribution (오늘/7일/30일/누적)
- 시간대별 슬리피지 평균
- 거부 사유 카운트
- 자산 곡선 기반 drawdown 깊이/지속일수
- 로컬앱 헬스 (마지막 사이클·KIS 토큰 만료·KIS 마스터 sync)
- CSV export 보조 함수 (서버에서 사용)

읽기 전용 — 파일을 변경하지 않는다. 사이클 끝에서 호출돼 snapshot payload에 합쳐진다.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .config import (APP_DIR, CYCLES_PATH, EQUITY_PATH, ORDERS_PATH,
                     SLIPPAGE_PATH)
from . import order_log

log = logging.getLogger("localapp.analytics")

_KIS_TOKEN_CACHE = APP_DIR / ".kis_token.json"
_MASTER_STAMP = APP_DIR / ".kis_master_pushed.txt"


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 전략별 P&L attribution ────────────────────────────────────────────────────

def strategy_pnl_summary(window_days: int = 30) -> dict:
    """orders.jsonl을 종목·전략별로 buy/sell 매칭해 P&L을 산정.

    매수와 매도를 strategy_name·symbol·order_no로 묶고, FIFO 매칭으로 실현 P&L 계산.
    Returns:
        {
          "by_strategy": [{
              "strategy": "삼성전자 모멘텀",
              "trades": 12, "win_rate": 58.3, "pnl": 124300,
              "today_pnl": 0, "week_pnl": 35000, "month_pnl": 124300,
          }, ...],
          "total": { "today": ..., "week": ..., "month": ..., "all": ... }
        }
    """
    if not ORDERS_PATH.exists():
        return {"by_strategy": [], "total": _zero_totals()}

    try:
        lines = ORDERS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {"by_strategy": [], "total": _zero_totals()}

    # 전략·종목별 매수 큐 (FIFO)
    buys: dict[tuple[str, str], list[dict]] = defaultdict(list)
    realized: list[dict] = []      # [{strategy, symbol, sell_ts, pnl}]

    for raw in lines:
        if not raw.strip():
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if o.get("event") not in ("filled", "partial"):
            continue
        strat = o.get("strategy") or "(미지정)"
        symbol = o.get("symbol", "")
        qty = int(o.get("qty", 0) or 0)
        px = float(o.get("fill_price", 0) or 0)
        if qty <= 0 or px <= 0:
            continue
        key = (strat, symbol)
        if o.get("side") == "buy":
            buys[key].append({"qty": qty, "price": px, "ts": o.get("ts", "")})
        else:
            # 매도 — FIFO로 매수와 매칭
            remain = qty
            pnl = 0.0
            while remain > 0 and buys[key]:
                lot = buys[key][0]
                take = min(remain, lot["qty"])
                pnl += (px - lot["price"]) * take
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    buys[key].pop(0)
            realized.append({
                "strategy": strat, "symbol": symbol,
                "sell_ts": o.get("ts", ""), "pnl": pnl,
            })

    # 시간 윈도우별 합계
    now = _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=window_days)

    by_strategy: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "pnl": 0.0,
                  "today_pnl": 0.0, "week_pnl": 0.0, "month_pnl": 0.0})

    total = {"today": 0.0, "week": 0.0, "month": 0.0, "all": 0.0}

    for r in realized:
        s = by_strategy[r["strategy"]]
        s["trades"] += 1
        if r["pnl"] > 0:
            s["wins"] += 1
        s["pnl"] += r["pnl"]
        total["all"] += r["pnl"]
        ts = _parse_ts(r["sell_ts"])
        if ts is None:
            continue
        if ts >= today_start:
            s["today_pnl"] += r["pnl"]; total["today"] += r["pnl"]
        if ts >= week_ago:
            s["week_pnl"] += r["pnl"]; total["week"] += r["pnl"]
        if ts >= month_ago:
            s["month_pnl"] += r["pnl"]; total["month"] += r["pnl"]

    out_rows = []
    for strat, v in sorted(by_strategy.items(), key=lambda kv: -kv[1]["pnl"]):
        out_rows.append({
            "strategy": strat,
            "trades": v["trades"],
            "win_rate": round(v["wins"] / v["trades"] * 100, 2) if v["trades"] else 0.0,
            "pnl": round(v["pnl"], 0),
            "today_pnl": round(v["today_pnl"], 0),
            "week_pnl": round(v["week_pnl"], 0),
            "month_pnl": round(v["month_pnl"], 0),
        })
    return {
        "by_strategy": out_rows,
        "total": {k: round(v, 0) for k, v in total.items()},
    }


def _zero_totals() -> dict:
    return {"today": 0.0, "week": 0.0, "month": 0.0, "all": 0.0}


# ── 시간대별 슬리피지 ─────────────────────────────────────────────────────────

def slippage_by_hour() -> dict:
    """슬리피지 샘플을 KST 시간대 버킷별로 평균/표본수 집계.

    버킷:
      - 08: 동시호가 (08:00~09:00)
      - 09: 장초 (09:00~10:00)
      - 10-14: 장중 (10:00~14:30)
      - 15: 장마감 (14:30~15:30)
      - other: 그 외 시간
    """
    if not SLIPPAGE_PATH.exists():
        return {"buckets": []}
    try:
        d = json.loads(SLIPPAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"buckets": []}
    samples = d.get("samples", [])
    buckets: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        ts = _parse_ts(s.get("ts", ""))
        if ts is None:
            continue
        # KST = UTC+9
        kst = ts.astimezone(timezone(timedelta(hours=9)))
        h = kst.hour; m = kst.minute
        if h == 8:
            key = "동시호가"
        elif h == 9:
            key = "장초"
        elif 10 <= h < 14 or (h == 14 and m < 30):
            key = "장중"
        elif (h == 14 and m >= 30) or h == 15:
            key = "장마감"
        else:
            key = "기타"
        buckets[key].append(float(s.get("bps", 0)))
    out = []
    for label in ("동시호가", "장초", "장중", "장마감", "기타"):
        vals = buckets.get(label, [])
        if not vals:
            continue
        out.append({
            "bucket": label,
            "n": len(vals),
            "avg_bps": round(sum(vals) / len(vals), 2),
            "max_bps": round(max(vals), 2),
        })
    return {"buckets": out}


# ── 거부 사유 카운트 ──────────────────────────────────────────────────────────

def rejection_reasons(limit: int = 200) -> dict:
    """orders.jsonl에서 rejected/timeout/cancelled 사유 카운트."""
    if not ORDERS_PATH.exists():
        return {"reasons": []}
    try:
        lines = ORDERS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return {"reasons": []}
    c: Counter = Counter()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        ev = o.get("event", "")
        if ev not in ("rejected", "timeout", "cancelled"):
            continue
        reason = o.get("msg") or o.get("reason") or ev
        c[f"{ev}: {reason}"[:80]] += 1
    return {"reasons": [{"label": k, "n": v}
                         for k, v in c.most_common(15)]}


# ── 현재 drawdown ─────────────────────────────────────────────────────────────

def drawdown_state() -> dict:
    """자산곡선 기반 현재 drawdown 깊이/지속일수.

    Returns:
      {
        "high": <고점>, "current": <현재>, "depth_pct": <%>,
        "days_since_high": <int>, "high_date": "YYYY-MM-DD",
      }
    """
    if not EQUITY_PATH.exists():
        return {"high": None, "current": None, "depth_pct": 0.0,
                "days_since_high": 0, "high_date": None}
    try:
        equity = json.loads(EQUITY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"high": None, "current": None, "depth_pct": 0.0,
                "days_since_high": 0, "high_date": None}
    if not equity:
        return {"high": None, "current": None, "depth_pct": 0.0,
                "days_since_high": 0, "high_date": None}
    high = -1.0; high_date = None
    for row in equity:
        v = float(row.get("value", 0) or 0)
        if v > high:
            high = v; high_date = row.get("date")
    cur_row = equity[-1]
    cur = float(cur_row.get("value", 0) or 0)
    depth_pct = 0.0
    if high > 0:
        depth_pct = (cur - high) / high * 100
    days = 0
    if high_date and cur_row.get("date"):
        try:
            d1 = datetime.fromisoformat(high_date)
            d2 = datetime.fromisoformat(cur_row["date"])
            days = (d2 - d1).days
        except Exception:
            days = 0
    return {
        "high": round(high, 0), "current": round(cur, 0),
        "depth_pct": round(depth_pct, 2),
        "days_since_high": days, "high_date": high_date,
    }


# ── 로컬앱 헬스 ───────────────────────────────────────────────────────────────

def local_health() -> dict:
    """KIS 토큰 만료, 마지막 KIS 마스터 sync, 마지막 사이클 ts."""
    health = {
        "last_cycle_ts": None,
        "kis_token_expires_at": None,
        "kis_master_pushed_date": None,
        "warnings": [],
    }
    # 마지막 사이클
    if CYCLES_PATH.exists():
        try:
            with open(CYCLES_PATH, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                seek = max(0, size - 4096)
                f.seek(seek)
                tail = f.read().decode("utf-8", errors="ignore")
            last_line = next((ln for ln in reversed(tail.splitlines())
                              if ln.strip()), "")
            if last_line:
                row = json.loads(last_line)
                health["last_cycle_ts"] = row.get("ts")
        except Exception as e:
            log.warning("마지막 사이클 읽기 실패: %s", e)

    # KIS 토큰 만료 (.kis_token.json)
    if _KIS_TOKEN_CACHE.exists():
        try:
            tk = json.loads(_KIS_TOKEN_CACHE.read_text(encoding="utf-8"))
            exp = tk.get("expires_at")
            health["kis_token_expires_at"] = exp
            if exp:
                exp_dt = _parse_ts(exp)
                if exp_dt:
                    # naive datetime이면 utc로 가정
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if exp_dt < _now():
                        health["warnings"].append("KIS 토큰 만료 — 재발급 필요")
                    elif exp_dt < _now() + timedelta(hours=2):
                        health["warnings"].append("KIS 토큰이 2시간 이내 만료 예정")
        except Exception:
            pass

    # 마지막 KIS 마스터 push
    if _MASTER_STAMP.exists():
        try:
            health["kis_master_pushed_date"] = _MASTER_STAMP.read_text(
                encoding="utf-8").strip()
        except Exception:
            pass

    return health


# ── 보유 포지션 풍부화 ────────────────────────────────────────────────────────

def enrich_positions(positions: list[dict], ledger: dict,
                      today_iso: Optional[str] = None) -> list[dict]:
    """KIS 응답의 positions에 ledger의 strategy/exit_rules를 합쳐 더 풍부한 카드 데이터 생성.

    추가 필드:
      - strategy_name, entry_date, peak_price
      - held_days
      - cur_return_pct (현재 수익률 %)
      - distances: {tp: ..., sl: ..., trail: ..., hold_days_left: ...} (해당되는 것만)
    """
    if not positions:
        return []
    today = today_iso or _now().date().isoformat()
    # ledger의 핵심 정보를 symbol → 항목으로 인덱싱 (한 종목당 하나의 활성 전략을 가정)
    by_symbol = {}
    for sid, lg in ledger.items():
        sym = lg.get("symbol")
        if sym:
            by_symbol[sym] = lg

    out = []
    for p in positions:
        sym = p.get("symbol", "")
        cur = float(p.get("eval_price", 0) or 0)
        lg = by_symbol.get(sym, {})
        entry = float(lg.get("entry_price", 0) or 0)
        peak = float(lg.get("peak_price", entry) or entry or cur)
        defn = lg.get("definition", {}) or {}
        ex = defn.get("exit_rules", {}) or {}

        cur_ret = ((cur - entry) / entry * 100) if entry > 0 else 0.0

        # 보유일수
        held = 0
        if lg.get("entry_date"):
            try:
                d1 = datetime.fromisoformat(lg["entry_date"]).date()
                d2 = datetime.fromisoformat(today).date()
                held = (d2 - d1).days
            except Exception:
                pass

        distances = {}
        if ex.get("take_profit") is not None and entry > 0:
            distances["tp_gap_pct"] = round(ex["take_profit"] - cur_ret, 2)
        if ex.get("stop_loss") is not None and entry > 0:
            distances["sl_gap_pct"] = round(cur_ret - ex["stop_loss"], 2)
        if ex.get("trail_pct") is not None and peak > 0:
            from_peak = (cur - peak) / peak * 100
            distances["trail_gap_pct"] = round(ex["trail_pct"] + from_peak, 2)
        if ex.get("hold_days") is not None:
            distances["hold_days_left"] = max(0, int(ex["hold_days"]) - held)

        out.append({
            **p,
            "strategy_name": lg.get("strategy_name", ""),
            "entry_date": lg.get("entry_date"),
            "entry_price": entry, "peak_price": peak,
            "cur_return_pct": round(cur_ret, 2),
            "held_days": held,
            "distances": distances,
        })
    return out


# ── Phase 40 — KIS 잔고 ↔ ledger 정합성 ──────────────────────────────────────
#
# HTS/MTS 수동 매매로 KIS 실 잔고와 자동매매 ledger가 어긋날 수 있다.
# 정책 (사용자 승인 — "자동 정정 + 알림"):
#   - ledger 일부 또는 전체 외부 매도 발견 → ledger 자동 차감/제거 + external_close 거래 기록
#   - 외부 매수 (KIS 초과분 또는 신규 종목) → ledger 손대지 않음 (정보만 표시)
# 자동 처리는 15:35 settlement 사이클에서만 — 매매 직전 08:55에서 손대면 위험.

def reconcile_ledger(kis_positions: list[dict], ledger: dict) -> dict:
    """KIS 실 잔고와 로컬 ledger를 비교해 drift를 카테고리별로 분류.

    Args:
        kis_positions: KIS account_snapshot()["positions"] 리스트.
            각 항목: {symbol, qty, avg_price, eval_price, name}
        ledger: trader.ledger (sid → {symbol, qty, entry_price, ...})

    Returns:
        {
          "ledger_orphans": [  # ledger엔 있는데 KIS엔 부족 (외부 매도 의심)
            {symbol, ledger_sids: [{sid, qty}, ...], kis_qty, shortfall,
             ledger_total_qty}
          ],
          "external_extras": [  # KIS엔 있는데 ledger엔 부족 (외부 매수)
            {symbol, kis_qty, ledger_total_qty, excess, in_ledger: bool}
          ],
          "in_sync": [symbol, ...],  # 양쪽 일치
          "checked_at": ISO timestamp,
          "ledger_symbol_count": int, "kis_symbol_count": int,
        }
    """
    from datetime import datetime

    kis_by_symbol = {p["symbol"]: int(p.get("qty", 0)) for p in kis_positions
                      if p.get("symbol") and int(p.get("qty", 0)) > 0}

    # ledger를 symbol → [{sid, qty}, ...]로 그룹핑 (한 종목을 여러 전략이 보유 가능)
    ledger_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for sid, lg in ledger.items():
        sym = lg.get("symbol")
        qty = int(lg.get("qty", 0))
        if sym and qty > 0:
            ledger_by_symbol[sym].append({"sid": sid, "qty": qty})

    orphans = []
    extras = []
    in_sync = []

    all_symbols = set(kis_by_symbol) | set(ledger_by_symbol)
    for sym in sorted(all_symbols):
        kis_qty = kis_by_symbol.get(sym, 0)
        ledger_sids = ledger_by_symbol.get(sym, [])
        ledger_total = sum(s["qty"] for s in ledger_sids)

        if kis_qty == ledger_total and ledger_total > 0:
            in_sync.append(sym)
        elif kis_qty < ledger_total:
            # 외부 매도 — ledger 차감 필요
            orphans.append({
                "symbol": sym,
                "ledger_sids": ledger_sids,
                "ledger_total_qty": ledger_total,
                "kis_qty": kis_qty,
                "shortfall": ledger_total - kis_qty,
            })
        else:
            # kis_qty > ledger_total — 외부 매수 (신규 또는 추가)
            extras.append({
                "symbol": sym,
                "kis_qty": kis_qty,
                "ledger_total_qty": ledger_total,
                "excess": kis_qty - ledger_total,
                "in_ledger": ledger_total > 0,
            })

    return {
        "ledger_orphans": orphans,
        "external_extras": extras,
        "in_sync": in_sync,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ledger_symbol_count": len(ledger_by_symbol),
        "kis_symbol_count": len(kis_by_symbol),
    }


def plan_orphan_adjustments(orphans: list[dict]) -> list[dict]:
    """ledger_orphans에 대한 차감 계획. 비례 분배 (가장 적은 qty 가진 sid부터 모두
    제거 → 남은 차감량을 다음 sid에서 차감, 음수 방지).

    Returns: [{sid, symbol, old_qty, new_qty, removed_qty, fully_closed}]
    """
    plans = []
    for o in orphans:
        # qty 적은 순으로 정렬해 작은 entry부터 제거
        sids = sorted(o["ledger_sids"], key=lambda s: s["qty"])
        kis_qty = o["kis_qty"]
        target_total = kis_qty
        # 큰 entry부터 남기고, 작은 entry부터 차감
        # 단순 비례 차감 → 라운딩 오차로 합이 안 맞을 수 있어 큰 entry로 흡수
        ledger_total = o["ledger_total_qty"]
        if ledger_total <= 0:
            continue
        remaining_target = target_total
        # 큰 entry부터 비례 배정 (큰 entry에 비례 몫 + 잔여)
        sids_desc = sorted(o["ledger_sids"], key=lambda s: -s["qty"])
        new_qtys: dict[str, int] = {}
        allocated = 0
        for i, s in enumerate(sids_desc):
            if i == len(sids_desc) - 1:
                # 마지막 entry: 남은 만큼 모두 할당 (라운딩 보정)
                nq = max(0, target_total - allocated)
            else:
                nq = int(s["qty"] * target_total / ledger_total)
                allocated += nq
            new_qtys[s["sid"]] = nq

        for s in o["ledger_sids"]:
            new_q = new_qtys[s["sid"]]
            plans.append({
                "sid": s["sid"],
                "symbol": o["symbol"],
                "old_qty": s["qty"],
                "new_qty": new_q,
                "removed_qty": s["qty"] - new_q,
                "fully_closed": new_q == 0,
            })
    return plans
