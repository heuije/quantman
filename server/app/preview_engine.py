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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quant_core import market_calendar as _mc
from quant_core.ir_engine import StrategyIR, needed_columns, needed_symbols
from sqlmodel import Session, select

from .data_cache import get_dataset, get_projected
from .db import engine
from . import kis_master_cache
from .models import Strategy, SyncSnapshot, User, UserSettings

_log = logging.getLogger("app.preview")
_KST = ZoneInfo("Asia/Seoul")


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


def _is_kr_symbol(symbol: str) -> bool:
    """6자리 숫자면 한국 종목. 그 외(NVDA·AAPL 등)는 미국으로 분류."""
    return symbol.isdigit() and len(symbol) == 6


def _last_session_on_or_before(market: str, today: date) -> date | None:
    """today 이하의 가장 가까운 시장 거래일. 캘린더 만료·예외 시 None.

    is_session_day는 휴장 여부만 답하므로 역행 루프로 검색. 30일 이상
    역행해야 한다면 캘린더가 비정상 → None 반환 (호출자는 fail-open).
    """
    d = today
    for _ in range(31):
        try:
            if _mc.is_session_day(market, d):
                return d
        except Exception:
            return None
        d = d - timedelta(days=1)
    return None


def _data_freshness_ok(dataset: dict, symbol: str,
                        today: date | None = None,
                        now_kst: datetime | None = None) -> tuple[bool, str]:
    """S-05 — 종목 dataset의 마지막 데이터가 시장 직전 거래일과 일치하는가.

    실패 케이스 — 사용자에게 노출하지 말아야 할 매수 후보:
      · 거래정지 종목 (마지막 거래일이 N일 전)
      · 상폐 직전 종목 (데이터 갱신 멈춤)
      · 데이터 cron 실패로 dataset이 stale

    True면 정상, False면 (False, 사유 메시지). 캘린더 자체가 비정상이면
    fail-open으로 True 반환 — over-engineering 자제(캘린더 만료는 별도
    log/health 경로에서 잡힘).

    now_kst — 미국 KST 05:00 cutoff 판정용 명시 시각. None이면 datetime.now(KST).
    today만 명시되고 now_kst가 None이면 today 한낮으로 가정(보수적 = "이미 마감").
    """
    last_str = _last_date(dataset, symbol)
    if last_str is None:
        return False, "데이터 없음 (dataset 누락)"
    try:
        last_d = date.fromisoformat(last_str)
    except ValueError:
        return False, f"날짜 파싱 실패: {last_str}"

    if today is None:
        if now_kst is None:
            now_kst = datetime.now(_KST)
        today = now_kst.date()
    elif now_kst is None:
        # today만 명시 호출(테스트 등) — 시간 정보 없음. 보수적으로 한낮 가정 → US 어제 마감 끝났다고 셈.
        now_kst = datetime(today.year, today.month, today.day, 12, 0, tzinfo=_KST)

    market = "KR" if _is_kr_symbol(symbol) else "US"
    # US 정규장은 EDT 16:00 = KST 익일 05:00 마감.
    # 따라서 "마지막 마감된 US 거래일"은 시각에 따라 달라진다:
    #   - KST 한낮(5/27 12:00): 어제 KST(5/26)의 US 거래 = 5/26 종가 확정. ref = 5/26.
    #   - KST 자정~05시(5/28 00:08): 어제 KST(5/27)의 US 거래가 아직 진행 중
    #     (EDT 11시) → 5/27 종가 미확정. ref = 5/26 (그제 기준).
    #   - KST 05시 이후(5/28 06:00): 어제 KST(5/27)의 US 거래 마감 끝 → ref = 5/27.
    # 이전 코드(`today - 1`만)는 KST 0:00~5:00 구간에 5/27을 "마감"으로 오인 →
    # dataset(5/26)을 1일 stale로 잘못 판단해 매수 후보에서 제외되는 회귀.
    if market == "US":
        days_back = 2 if now_kst.hour < 5 else 1
        ref_anchor = today - timedelta(days=days_back)
    else:
        ref_anchor = today
    ref = _last_session_on_or_before(market, ref_anchor)
    if ref is None:
        # 캘린더 미작동 — stale 판정 자체가 불가하므로 통과 (다른 신호가 잡음)
        return True, ""
    if last_d < ref:
        days_behind = (ref - last_d).days
        return False, (
            f"데이터 stale (last={last_str}, 직전 {market} 거래일 "
            f"{ref.isoformat()}, {days_behind}일 지연) — "
            f"거래정지·상폐·데이터 누락 가능")
    return True, ""



def _evaluate_ir_strategy(strat_def: dict, dataset: dict, cash: float,
                          held_keys: set[str], master_by_code: dict,
                          live_basket: list[str] | None = None) -> dict:
    """engine='ir' 전략의 다음날 매수 후보 — ir_engine 마지막 바 신호·선택 재사용.

    수렴의 본질: run_unified/_run_scheduled와 **동일한** _select·_target_weights를 써
    backtest와 live의 신호 선택을 일치시킨다. 사이징은 preview 추정치 — 실제 사이징·발주는
    로컬앱(Stage 3)이 소유한다. 미국 종목은 USD 사이징 불가로 qty=None(표시만, 보안 원칙).

    live_basket(Task 12b) — 정적 세부조건(once_at_start) 라이브 바스켓이 이미 형성됐다면
    그 고정 종목집합만 후보로 쓰고 세부조건을 재평가하지 않는다(전환 시점에 고정). None이면
    동적 세부조건을 매 평가마다 적용하고, 형성용 후보(당일 자격집합)를 screener_members에 담는다.

    out 형태는 _evaluate_strategy와 동일(프론트 PreviewByStrategy 공유).
    """
    import numpy as np
    import pandas as pd

    out = {
        "strategy_name": strat_def.get("name", ""),
        "trade_symbol": "",
        "signal_passed": False,
        "candidates": [], "skipped": [],
        "signal_details": [], "signal_summary": "",
        "per_symbol_details": {}, "screener_members": [],
    }
    from quant_core.ir_engine import StrategyIR
    from quant_core.ir_engine import live as ir_live
    from quant_core.ir_engine.engine import (
        _scoped, _screener_mask, _select, _target_weights, _universe_symbols)
    from quant_core.blocks import EvalContext, Node, evaluate
    from quant_core.blocks.catalog import get, has

    try:
        s = StrategyIR.model_validate(strat_def)
    except Exception as e:  # noqa: BLE001 — 사용자 정의 파싱 실패는 skip 사유로
        out["skipped"].append({"reason": f"IR 전략 파싱 실패: {e}"})
        return out

    pos, u = s.position, s.universe
    out["trade_symbol"] = ("IR:전체" if u.kind == "all"
                           else "IR:" + ",".join(u.symbols))
    st = get(s.signal.op).out_type.value if has(s.signal.op) else None
    if st not in ("condition", "score"):
        out["skipped"].append({"reason": f"최상위 신호가 condition/score가 아닙니다: {st}"})
        return out
    is_condition = (st == "condition")
    if pos.direction in ("short", "long_short") and is_condition:
        out["skipped"].append({"reason": "숏·롱숏 방향은 score(점수) 신호가 필요합니다."})
        return out

    syms = _universe_symbols(s, dataset)
    if not syms:
        out["skipped"].append({"reason": "유니버스에 종목이 없습니다."})
        return out
    screener = u.screener or {}
    filt = (Node.model_validate(screener["condition"])
            if screener.get("condition") else None)
    ds = _scoped(dataset, syms, s.signal, filt, pos.overlays.group_label)
    ctx = EvalContext.from_dataset(ds)
    try:
        alpha = evaluate(s.signal, ctx)
    except Exception as e:  # noqa: BLE001
        out["skipped"].append({"reason": f"신호 평가 오류: {e}"})
        return out
    if not isinstance(alpha, pd.DataFrame) or alpha.empty:
        out["skipped"].append({"reason": "신호가 패널(종목×날짜)을 산출하지 않습니다."})
        return out
    if is_condition:
        b = alpha.astype(bool)
        alpha = b.astype(float).where(b, np.nan)
    cols = [c for c in syms if c in alpha.columns]
    if not cols:
        out["skipped"].append({"reason": "신호가 유니버스 종목을 포함하지 않습니다."})
        return out
    if live_basket is not None:
        # 정적 세부조건 라이브 바스켓 — 전환 시점에 고정된 종목집합만 후보로(세부조건 재평가 안 함).
        # cols 순서를 보존(결정적)하면서 바스켓과 교집합한다.
        basket_set = set(live_basket)
        cols = [c for c in cols if c in basket_set]
        if not cols:
            out["skipped"].append({"reason": "고정 바스켓에 거래 가능 종목이 없습니다."})
            return out
        alpha = alpha[cols]
    elif screener.get("condition"):
        alpha = alpha[cols]
        try:
            elig = _screener_mask(screener, ctx, cols)
            elig = elig.reindex(index=alpha.index, columns=cols).fillna(False)
            alpha = alpha.where(elig)
            # 형성용 후보 바스켓 — 마지막 행(당일)에서 자격 True인 종목들. 정적 전략이 고정할 집합.
            last_elig = elig.iloc[-1]
            out["screener_members"] = [c for c in cols if bool(last_elig.get(c, False))]
        except Exception as e:  # noqa: BLE001
            out["skipped"].append({"reason": f"스크리너 평가 오류: {e}"})
            return out
    else:
        alpha = alpha[cols]

    d = alpha.index[-1]
    out["signal_summary"] = f"마지막 신호일 {str(d)[:10]} 기준"
    longs, _shorts = _select(alpha.loc[d], pos, is_condition)
    if not longs:
        out["skipped"].append({"reason": "다음날 진입 신호(롱) 종목 없음"})
        return out
    out["signal_passed"] = True

    # ── 사이징 (preview 추정 — 로컬앱이 실제 발주 사이징 소유) ──
    # 이벤트(on_signal) 진입은 엔진 _budget과 동일하게 항상 per-name 예산:
    #   amount_krw(fixed_amount) 또는 cash×amount_pct% (단일 종목 유니버스는 100% 전액).
    #   횡단 사이저(equal/vol/target_vol/fixed_weight)는 스케줄·상시 진입에서만 적용.
    sz = pos.sizing
    weights = None
    event_budget = (pos.entry.mode == "on_signal")
    if not event_budget:
        vol_row = None
        if sz.mode in ("vol_inverse", "target_vol"):
            closep = pd.DataFrame({
                c: dataset[c]["Close"].reindex(alpha.index).ffill()
                for c in longs if c in dataset and "Close" in dataset[c].columns})
            vol = closep.pct_change().rolling(sz.vol_window).std()
            vol_row = vol.loc[d] if d in vol.index else None
        w = _target_weights(longs, [], alpha.loc[d], vol_row, sz)
        cap = sz.max_position_pct / 100.0
        w = w.clip(lower=-cap, upper=cap)
        tot = float(w.abs().sum())
        if sz.mode != "target_vol" and tot > 0:
            w = w / tot
        weights = w

    now_kst = datetime.now(_KST)
    today_kst = now_kst.date()
    for sym in longs:
        fresh, freshness_msg = _data_freshness_ok(dataset, sym, today_kst, now_kst=now_kst)
        if not fresh:
            out["skipped"].append({"symbol": sym, "reason": freshness_msg})
            continue
        prev_close = _prev_close(dataset, sym)
        if prev_close is None:
            out["skipped"].append({"symbol": sym, "reason": "전일 종가 없음"})
            continue
        meta = master_by_code.get(sym, {})
        if not _is_kr_symbol(sym):
            out["candidates"].append({
                "symbol": sym, "name": meta.get("name", ""), "qty": None,
                "prev_close": round(prev_close, 2), "est_limit_price": None,
                "est_total": None, "sizing_mode": sz.mode,
                "data_as_of": _last_date(dataset, sym), "source": "ir",
                "currency": "USD",
                "note": "미국 종목 — 발주 시점 USD 잔고로 사이징 (preview 미지원)"})
            continue
        if event_budget:
            qty = ir_live.event_buy_qty(s, cash=cash, prev_close=prev_close)
        else:
            budget = (float(weights[sym]) * cash
                      if weights is not None and sym in weights.index
                      else cash / len(longs))
            qty = int(budget // prev_close) if prev_close > 0 else 0
        if qty <= 0:
            out["skipped"].append({
                "symbol": sym,
                "reason": f"수량 부족 (전일종가 {prev_close:,.0f} 대비 예산 부족)"})
            continue
        est_price = int(prev_close)
        out["candidates"].append({
            "symbol": sym, "name": meta.get("name", ""), "qty": qty,
            "prev_close": round(prev_close, 2), "est_limit_price": est_price,
            "est_total": est_price * qty, "sizing_mode": sz.mode,
            "data_as_of": _last_date(dataset, sym), "source": "ir",
            "currency": "KRW"})
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


def _preview_dataset(strats: list, held_symbols: set) -> dict:
    """유저 전략들이 참조하는 컬럼·종목만 프로젝션한 dataset — 전 유니버스 45컬럼
    (~9.4GB) 빌드를 회피한다(preview cron OOM의 직접 원인이었던 경로).

    · 어떤 전략이든 컬럼 결정 불가(strat: 조합 등) → 전체(get_dataset) 안전 폴백(드묾).
    · all 전략이 하나라도 있으면 전 종목 × 참조컬럼 프로젝션.
    · 전부 single/list면 그 종목들 ∪ 보유종목만 프로젝션.
    보유종목 Close(청산 미리보기)는 OHLCV라 프로젝션에도 항상 포함된다.
    """
    union_cols: set = set()
    union_syms: set = set(held_symbols)
    full_universe = False
    for s in strats:
        try:
            sir = StrategyIR.model_validate(dict(s.definition or {}))
        except Exception:                            # noqa: BLE001 — 파싱 실패는 평가 단계가 skip
            continue
        cols = needed_columns(sir)
        if cols is None:
            return get_dataset()                     # 결정 불가 → 전체(안전, 드묾)
        union_cols |= cols
        syms = needed_symbols(sir)
        if syms is None:
            full_universe = True
        else:
            union_syms |= syms
    if full_universe:
        return get_projected(union_cols, symbols=None)
    return get_projected(union_cols, symbols=union_syms)


def build_user_preview(session: Session, user_id: int,
                        data_source: str) -> dict:
    """사용자 1명에 대한 next-day preview 생성.

    Args:
        session: SQL 세션
        user_id: 사용자 ID
        data_source: cron 식별자 ('dataset_global', 'krx_2nd', 등)
    """
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

    # 전략별 매수 평가 — IR 단일 체제(레거시 operand 행은 skip).
    strats = session.exec(
        select(Strategy).where(
            Strategy.user_id == user_id,
            Strategy.run_mode.in_(("paper", "live"))
        )
    ).all()
    ir_strats = [s for s in strats if getattr(s, "engine", "operand") == "ir"]

    # 데이터셋 — 이 유저 전략들이 실제 쓰는 컬럼·종목만(컬럼 프로젝션). 전 유니버스 빌드 회피.
    dataset = _preview_dataset(ir_strats, held_symbols)

    by_strategy = []
    total_buy_amount = 0
    n_buy_candidates = 0
    basket_formed = False           # 정적 바스켓을 이번 호출에서 1회라도 형성·persist 했는가

    for s in ir_strats:
        strat_def = dict(s.definition or {})
        strat_def["_id"] = s.id
        result = _evaluate_ir_strategy(strat_def, dataset, cash, held_symbols,
                                       master_by_code, live_basket=s.live_basket)
        result["strategy_id"] = s.id
        result["run_mode"] = s.run_mode
        by_strategy.append(result)

        # Task 12b — 정적 세부조건(once_at_start) 라이브 바스켓 lazy 형성.
        # 전환 후 바스켓이 아직 없고(falsy), 이번 평가가 당일 자격집합(screener_members)을
        # 산출했으면 그 집합으로 고정. 이후 평가는 s.live_basket을 그대로 받아 재선별 안 함.
        screener = (strat_def.get("universe") or {}).get("screener") or {}
        if (s.run_mode in ("paper", "live")
                and screener.get("refresh") == "once_at_start"
                and screener.get("condition")
                and not s.live_basket
                and result.get("screener_members")):
            s.live_basket = list(result["screener_members"])
            session.add(s)
            basket_formed = True

        for c in result.get("candidates", []):
            # US 종목은 USD/KRW 통화 mismatch로 est_total=None (preview_engine.py:164)
            # — 의도된 동작. 합산에서만 skip하고 후보 카운트엔 포함 (사용자 입장에선 매수 예정).
            # commit 92c2d6e가 line 164 None 설정만 추가하고 여기 합산 None-safe 누락 →
            # US 후보 1개라도 있으면 TypeError → preview 전체 fail → cron이 user snapshot에
            # 머지 못함 → 다음 cycle "preview 없음"으로 매수 0 (KRX 후보까지 함께 누락).
            est = c.get("est_total")
            if est is not None:
                total_buy_amount += est
            n_buy_candidates += 1

    # 형성된 정적 바스켓을 persist. caller(cron·sync·manual)는 이후 snapshot을 별도 commit하므로
    # 여기서 바스켓 변경을 먼저 commit해 다음 호출이 고정 집합을 그대로 받게 한다.
    if basket_formed:
        session.commit()

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

    Phase 49 — per-user session 격리: 한 user의 SQL error가
    InFailedSqlTransaction으로 다른 user를 cascade로 죽이지 않도록 사용자마다
    새 Session 생성. 이전엔 하나의 session 공유로 첫 user fail 시 14명+ 전원
    "current transaction is aborted" 로 실패하던 패턴 차단.
    """
    n_ok = n_skipped = n_failed = n_alerted = 0
    is_final_cron = data_source == "dataset_kr"
    # 1) 사용자 ID 목록만 가벼운 세션으로 가져옴
    with Session(engine) as session:
        user_ids = [u.id for u in session.exec(select(User)).all()]

    # 2) 각 사용자마다 독립 세션 — transaction 격리
    for uid in user_ids:
        webhook_sent = False
        try:
            with Session(engine) as session:
                preview = build_user_preview(session, uid, data_source)
                if not preview.get("available"):
                    n_skipped += 1
                    continue
                snap = _latest_snapshot(session, uid)
                if snap is None:
                    n_skipped += 1
                    continue
                new_payload = dict(snap.payload or {})
                new_payload["next_day_preview"] = preview
                snap.payload = new_payload
                session.add(snap)

                # 최종 cron 후 webhook 발송 (사용자가 webhook URL 설정한 경우)
                if is_final_cron:
                    s = session.get(UserSettings, uid)
                    if s and s.alert_webhook_url:
                        # _post_preview_webhook은 자체 try/except 처리 — bool 반환
                        webhook_sent = _post_preview_webhook(
                            s.alert_webhook_url, preview)

                session.commit()
            # commit 성공 후 카운트
            n_ok += 1
            if webhook_sent:
                n_alerted += 1
        except Exception as e:
            _log.exception("user %d preview 실패: %s", uid, e)
            n_failed += 1

    _log.info("preview 갱신 [%s]: 성공 %d · skip %d · 실패 %d · 알림 %d",
              data_source, n_ok, n_skipped, n_failed, n_alerted)
    return {"ok": n_ok, "skipped": n_skipped, "failed": n_failed,
            "alerted": n_alerted}
