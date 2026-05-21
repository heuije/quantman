"""내일 매매 미리보기 (Next-day preview) — 서버 측 evaluate-only 사이클.

각 데이터 갱신 cron 종료 후 호출되어, 모든 사용자의 paper/live 전략에 대해
"내일 사이클이 결정할 매수/매도 후보"를 평가하고 sync snapshot에 저장한다.

실제 발주는 여전히 로컬앱 08:55 사이클에서 — preview는 사용자 투명성용.
KIS API 호출 0회, 서버에 이미 있는 데이터만 사용:
  • dataset (백테스트와 동일 — 매수 신호 평가)
  • 마지막 sync snapshot (잔고·보유 종목)
  • 전략 정의 (DB)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import quant_core as qc
from sqlmodel import Session, select

from .data_cache import get_dataset
from .db import engine
from . import kis_master_cache
from .models import Strategy, SyncSnapshot, User, UserSettings

_log = logging.getLogger("app.preview")


def _latest_snapshot(session: Session, user_id: int) -> SyncSnapshot | None:
    return session.exec(
        select(SyncSnapshot).where(SyncSnapshot.user_id == user_id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()


def _prev_close(dataset: dict, symbol: str) -> float | None:
    df = dataset.get(symbol)
    if df is None or len(df) == 0 or "Close" not in df.columns:
        return None
    try:
        v = float(df["Close"].iloc[-1])
        return v if v > 0 else None
    except Exception:
        return None


def _last_date(dataset: dict, symbol: str) -> str | None:
    df = dataset.get(symbol)
    if df is None or len(df) == 0:
        return None
    try:
        return str(df.index[-1])[:10]
    except Exception:
        return None


def _atr14(dataset: dict, symbol: str) -> float | None:
    df = dataset.get(symbol)
    if df is None or "atr_14" not in df.columns:
        return None
    try:
        v = float(df["atr_14"].iloc[-1] or 0.0)
        return v if v > 0 else None
    except Exception:
        return None


def _size_and_append_candidate(symbol: str, prev_close: float, dataset: dict,
                                cash: float, exec_pol: dict, amount_pct: float,
                                master_by_code: dict, buy_tolerance_pct: float,
                                source: str, out: dict) -> bool:
    """수량 사이징 후 out["candidates"]에 append. 성공 시 True.

    수동/자동 선택 공통 — 매수 후보 종목 1개에 대한 사이징·예상 발주가 계산.
    source는 후보 origin 표시용 ('manual' | 'screener').
    """
    sizing_mode = exec_pol.get("sizing_mode", "atr_risk")
    if sizing_mode == "atr_risk":
        atr_val = _atr14(dataset, symbol)
        if atr_val is None:
            out["skipped"].append({
                "symbol": symbol,
                "reason": "ATR 데이터 없음 — atr_risk 모드 진입 불가"})
            return False
        atr_risk_pct = float(exec_pol.get("atr_risk_pct") or 1.0)
        atr_mult = float(exec_pol.get("atr_mult") or 2.0)
        max_position_pct = float(exec_pol.get("max_position_pct") or 10.0)
        risk = cash * atr_risk_pct / 100.0
        risk_per_share = atr_val * atr_mult
        qty = int(risk // risk_per_share) if risk_per_share > 0 else 0
        cap_qty = int((cash * max_position_pct / 100.0) // prev_close)
        qty = min(qty, cap_qty, int(cash // prev_close))
    else:
        qty = int(cash * amount_pct / 100.0 // prev_close)

    if qty <= 0:
        out["skipped"].append({
            "symbol": symbol,
            "reason": f"수량 부족 (현금 {cash:,.0f} / 전일종가 {prev_close:,.0f})"})
        return False

    est_price = int(prev_close * (1 + buy_tolerance_pct / 100.0))
    meta = master_by_code.get(symbol, {})
    out["candidates"].append({
        "symbol": symbol,
        "name": meta.get("name", ""),
        "qty": qty,
        "prev_close": round(prev_close, 2),
        "est_limit_price": est_price,
        "est_total": est_price * qty,
        "sizing_mode": sizing_mode,
        "data_as_of": _last_date(dataset, symbol),
        "source": source,
    })
    return True


def _evaluate_strategy(strat_def: dict, dataset: dict, cash: float,
                        held_keys: set[str], master_by_code: dict) -> dict:
    """전략 하나에 대한 preview 결과 — 매수 후보 빌드.

    Phase 41 — 자동 선택 / 수동 다중 모두 동일한 평가 흐름:
      1) 평가 대상 종목 리스트 결정 (수동 = trade_symbol 토큰 / 자동 = preset 매칭)
      2) 공통 조건은 한 번 평가, 종목별 조건([이 종목] placeholder)은 각 종목에서 평가
      3) AND/OR 결합 후 통과한 종목만 사이징해서 후보로 등록 (screener_limit 한도)

    KIS 호출 없음. dataset(OHLC/지표) + krx_cache(스크리너 메트릭) + 마스터만 사용.
    """
    out = {
        "strategy_name": strat_def.get("name", ""),
        "trade_symbol": strat_def.get("trade_symbol", ""),
        "signal_passed": False,
        "candidates": [],
        "skipped": [],
        "signal_details": [],
        "signal_summary": "",
        "per_symbol_details": {},
    }

    mode, targets = qc.parse_trade_symbols(strat_def.get("trade_symbol", ""))
    screener_limit = int(strat_def.get("screener_limit", 1) or 1)
    amount_pct = float(strat_def.get("amount_pct", 100) or 100)
    exec_pol = strat_def.get("execution") or {}
    buy_tolerance_pct = float(exec_pol.get("buy_tolerance_pct") or 1.0)

    # ── 1. 평가 대상 종목 결정 ──────────────────────────────────────────────────
    match_meta_by_symbol: dict[str, dict] = {}
    if mode == "screener":
        preset_key = targets[0] if targets else ""
        if not preset_key:
            out["skipped"].append({"reason": "자동 선택 preset key 없음"})
            return out
        try:
            from . import screener as screener_engine
            matches = screener_engine.run_preset(preset_key)
        except Exception as e:
            out["skipped"].append({
                "reason": f"자동 선택 실행 실패 (preset={preset_key}): {e}"})
            return out
        out["screener_preset"] = preset_key
        if not matches:
            out["skipped"].append({
                "reason": f"자동 선택 매칭 종목 없음 (preset={preset_key})"})
            return out
        eval_symbols = [m["symbol"] for m in matches if m.get("symbol")]
        match_meta_by_symbol = {m["symbol"]: m for m in matches}
        source = "screener"
        slots_left = screener_limit
    else:
        if not targets:
            out["skipped"].append({"reason": "매수 대상 종목 없음"})
            return out
        eval_symbols = list(targets)
        source = "manual"
        slots_left = screener_limit if len(targets) > 1 else 1

    # ── 2. 매수 조건 평가 ───────────────────────────────────────────────────────
    try:
        strat = qc.Strategy(**strat_def)
    except Exception as e:
        out["skipped"].append({"reason": f"전략 파싱 실패: {e}"})
        return out

    conditions = [c.model_dump() for c in strat.buy.conditions]
    logic = strat.buy.logic

    if conditions:
        try:
            expl = qc.explain_buy_signal_per_symbol(
                dataset, conditions, logic, eval_symbols)
        except Exception as e:
            out["skipped"].append({"reason": f"신호 평가 오류: {e}"})
            return out

        # 공통 조건 결과는 별도 노출 (UI 패널에서 한 번만 표시)
        common_ex = expl.get("common")
        if common_ex is not None:
            out["signal_details"] = common_ex["details"]
            out["signal_summary"] = common_ex["summary"]
            # AND 결합 + 공통 미통과면 전체 보류 (게이트 의미)
            if logic == "AND" and not common_ex["passed"]:
                out["signal_passed"] = False
                out["skipped"].append({
                    "reason": f"공통 매수 조건 미통과 — {common_ex['summary']}"
                })
                out["per_symbol_details"] = expl.get("per_symbol", {})
                return out

        out["per_symbol_details"] = expl.get("per_symbol", {})
        passed_symbols = list(expl.get("passed_symbols") or [])
        out["signal_passed"] = bool(passed_symbols)

        if not passed_symbols:
            # 어느 종목도 통과하지 않음 — 사유 한 줄로 요약
            sample = next(iter(expl["per_symbol"].values()), None)
            reason_tail = f" (예: {sample['summary']})" if sample else ""
            out["skipped"].append({
                "reason": "매수 조건 충족 종목 없음" + reason_tail
            })
            return out
    else:
        # 매수 조건 비어있음 — 안전한 기본값으로 매수 보류 (Phase 38.11과 동일)
        out["signal_passed"] = False
        out["skipped"].append({"reason": "매수 조건 없음 — 매수 보류"})
        return out

    # ── 3. 통과 종목 사이징 (screener_limit 한도) ──────────────────────────────
    bought = 0
    for symbol in eval_symbols:
        if bought >= slots_left:
            break
        if symbol not in passed_symbols:
            continue
        prev_close = _prev_close(dataset, symbol)
        if prev_close is None and symbol in match_meta_by_symbol:
            # 자동 선택 — KRX 캐시 fallback (해외 ETF 등 dataset 누락 종목)
            prev_close = float(match_meta_by_symbol[symbol].get("close") or 0)
            if prev_close <= 0:
                prev_close = None
        if prev_close is None:
            out["skipped"].append({
                "symbol": symbol,
                "reason": "전일 종가 없음 (dataset · KRX 캐시 모두 누락)"})
            continue
        if _size_and_append_candidate(
                symbol, prev_close, dataset, cash, exec_pol, amount_pct,
                master_by_code, buy_tolerance_pct, source, out):
            bought += 1

    return out


def _evaluate_exits(positions: list[dict], dataset: dict,
                      master_by_code: dict) -> list[dict]:
    """보유 종목에 대한 청산 미리보기 — 마지막 종가 기반 추정.

    실제 청산 평가는 다음날 사이클의 KIS 현재가로 다시 — 여기선 사용자에게
    "현 추세대로면 청산될 종목" 힌트만 제공.
    """
    candidates = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        entry_price = float(pos.get("entry_price") or pos.get("avg_price") or 0)
        peak_price = float(pos.get("peak_price") or entry_price)
        if entry_price <= 0:
            continue

        prev_close = _prev_close(dataset, symbol)
        if prev_close is None:
            continue   # 데이터 없으면 추정 불가

        ret_pct = (prev_close - entry_price) / entry_price * 100
        # 전략 정의에서 exit_rules 가져오기는 복잡 — 우선 가격만 노출하고 사용자 판단
        # (다음 단계에서 strat_def.exit_rules 매칭 추가 가능)
        candidates.append({
            "symbol": symbol,
            "name": master_by_code.get(symbol, {}).get("name", ""),
            "qty": int(pos.get("qty", 0)),
            "entry_price": entry_price,
            "prev_close": round(prev_close, 2),
            "return_pct": round(ret_pct, 2),
            "peak_price": round(peak_price, 2),
        })
    return candidates


def build_user_preview(session: Session, user_id: int,
                        data_source: str) -> dict:
    """사용자 1명에 대한 next-day preview 생성.

    Args:
        session: SQL 세션
        user_id: 사용자 ID
        data_source: cron 식별자 ('dataset_global', 'krx_2nd', 등)
    """
    dataset = get_dataset()
    snapshot = _latest_snapshot(session, user_id)
    if snapshot is None or not snapshot.payload:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": data_source,
            "available": False,
            "reason": "로컬앱 페어링·sync 필요",
        }

    payload = snapshot.payload
    balance = payload.get("balance") or {}
    cash = float(balance.get("cash") or 0)
    positions = payload.get("positions") or []
    held_symbols = {p.get("symbol", "") for p in positions}

    # KIS 마스터 lookup (종목명 표시용)
    master_list = kis_master_cache.get_master_list()
    master_by_code = {m["symbol"]: m for m in master_list}

    # 전략별 매수 평가
    strats = session.exec(
        select(Strategy).where(
            Strategy.user_id == user_id,
            Strategy.run_mode.in_(("paper", "live"))
        )
    ).all()

    by_strategy = []
    total_buy_amount = 0
    n_buy_candidates = 0

    for s in strats:
        strat_def = dict(s.definition or {})
        strat_def["_id"] = s.id
        result = _evaluate_strategy(strat_def, dataset, cash, held_symbols, master_by_code)
        result["strategy_id"] = s.id
        result["run_mode"] = s.run_mode
        by_strategy.append(result)
        for c in result.get("candidates", []):
            total_buy_amount += c["est_total"]
            n_buy_candidates += 1

    # 청산 미리보기
    exit_candidates = _evaluate_exits(positions, dataset, master_by_code)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": data_source,
        "available": True,
        "summary": {
            "n_buy_candidates": n_buy_candidates,
            "est_total_buy_amount": total_buy_amount,
            "n_holding": len(positions),
            "cash": cash,
        },
        "by_strategy": by_strategy,
        "exit_candidates": exit_candidates,
    }


def _post_preview_webhook(url: str, preview: dict) -> bool:
    """Discord/Slack 호환 webhook으로 preview 요약 발송."""
    if not url:
        return False
    import requests
    s = preview.get("summary") or {}
    n_buy = s.get("n_buy_candidates", 0)
    est_amt = s.get("est_total_buy_amount", 0)
    n_exit = len(preview.get("exit_candidates") or [])

    # 상위 5개 매수 후보 요약
    lines = []
    for bs in (preview.get("by_strategy") or [])[:3]:
        for c in (bs.get("candidates") or [])[:3]:
            lines.append(f"  • [{bs['strategy_name']}] {c['symbol']} {c['name']} "
                         f"— {c['qty']}주 × {c['est_limit_price']:,}원")

    text = (f"📋 [Quant] 내일 매매 미리보기 (확정)\n"
            f"매수 {n_buy}건 · 예상 총액 {est_amt:,}원 · 청산 후보 {n_exit}건\n"
            + ("\n".join(lines) if lines else ""))
    try:
        r = requests.post(url, json={"content": text, "text": text}, timeout=8)
        return 200 <= r.status_code < 300
    except Exception as e:
        _log.warning("preview webhook 전송 실패: %s", e)
        return False


def refresh_all_users_preview(data_source: str) -> dict:
    """모든 사용자의 preview를 갱신해 sync_snapshots의 payload에 next_day_preview 추가.

    cron 종료 시 호출됨. KIS 호출 0회, 가벼움 (사용자당 수십ms 예상).
    data_source == 'dataset_kr' (마지막 cron, 18:15) 일 때만 webhook 발송 — 스팸 방지.
    """
    n_ok = n_skipped = n_failed = n_alerted = 0
    is_final_cron = data_source == "dataset_kr"
    with Session(engine) as session:
        users = session.exec(select(User)).all()
        for u in users:
            try:
                preview = build_user_preview(session, u.id, data_source)
                if not preview.get("available"):
                    n_skipped += 1
                    continue
                snap = _latest_snapshot(session, u.id)
                if snap is None:
                    n_skipped += 1
                    continue
                new_payload = dict(snap.payload or {})
                new_payload["next_day_preview"] = preview
                snap.payload = new_payload
                session.add(snap)
                n_ok += 1

                # 최종 cron 후 webhook 발송 (사용자가 webhook URL 설정한 경우)
                if is_final_cron:
                    s = session.get(UserSettings, u.id)
                    if s and s.alert_webhook_url:
                        if _post_preview_webhook(s.alert_webhook_url, preview):
                            n_alerted += 1
            except Exception as e:
                _log.exception("user %d preview 실패: %s", u.id, e)
                n_failed += 1
        session.commit()

    _log.info("preview 갱신 [%s]: 성공 %d · skip %d · 실패 %d · 알림 %d",
              data_source, n_ok, n_skipped, n_failed, n_alerted)
    return {"ok": n_ok, "skipped": n_skipped, "failed": n_failed,
            "alerted": n_alerted}
