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
from quant_core.data_fetcher import SYMBOL_CATEGORY
from quant_core.exec_defaults import DEFAULT_EXECUTION as _DEFAULT_EXECUTION
from quant_core.exec_defaults import instrument_region as _instrument_region
from quant_core.exec_defaults import instrument_spec as _instrument_spec
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


def _dataset_as_of(dataset: dict) -> dict:
    """시장별 데이터 기준 거래일 — dataset 내 그 시장 종목 마지막 봉의 최댓값 (로드맵 C).

    로컬앱이 후보 사용 직전 "직전 거래일 이상인가"를 검증하는 하한 도장.
    max를 쓰는 이유: 개별 laggard(상폐 임박·저유동)로 인한 과차단을 피하고,
    "서버가 본 가장 새로운 데이터"가 기준 미달이면 확실히 낡은 것이기 때문.
    매크로(^)·암호화폐(24/7 — '-USD' 접미)는 시장 세션 기준일과 무관해 제외.
    """
    out: dict[str, str] = {}
    for sym, df in dataset.items():
        if sym.startswith("^") or sym.endswith("-USD"):
            continue
        last = _last_date(dataset, sym)
        if last is None:
            continue
        mkt = "KR" if _is_kr_symbol(sym) else "US"
        if mkt not in out or last > out[mkt]:
            out[mkt] = last
    return out


def _commission_rate(s: "StrategyIR") -> float:
    """예상 위탁수수료율(편도, 0~1). 단일 출처: 전략 SimSpec.commission이 명시됐으면
    그 값, 없으면 백테스트 default(exec_defaults bt_commission_bps/10000 = 3bps = 0.0003).

    백테스트가 같은 비용 가정을 쓰므로 preview est_fee_krw가 백테스트 회계와 정합한다."""
    c = s.simulation.commission
    if c is not None:
        return float(c)
    return _DEFAULT_EXECUTION["bt_commission_bps"] / 10000.0


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


def _stale_signal_ref(dataset: dict, needed: set[str] | None,
                      universe_syms: list[str],
                      now_kst: datetime) -> tuple[str, str] | None:
    """R-1(2026-07-10 리뷰) — 신호 *참조* 심볼 신선도 검사. stale이면 (symbol, 사유), 정상 None.

    종전 게이트는 거래 후보(longs/shorts)만 per-candidate 검사해, 신호 입력(예: S&P500)이
    수집 실패로 낡아도 신호가 낡은 데이터로 평가된 채 발주됐다(조용한 오신호). 참조가
    낡으면 신호 자체가 낡은 것 — 전략 단위로 차단·사유 표면화한다.

    범위는 일봉 가격 자산(SYMBOL_CATEGORY=="자산")만: 주간/월간 캘린더 매크로(COT·미결제
    약정 등)는 며칠 지연이 정상이라 KR/US 일봉 달력 기준인 _data_freshness_ok를 적용하면
    영구 오차단된다(과차단 방지). universe kind=all은 needed_symbols가 None → 미적용.
    """
    uni = set(universe_syms)
    for ref in sorted((needed or set()) - uni):
        if SYMBOL_CATEGORY.get(ref) != "자산":
            continue
        fresh, msg = _data_freshness_ok(dataset, ref, now_kst.date(), now_kst=now_kst)
        if not fresh:
            return ref, msg
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

    # 데이터 신선도 캘린더 — 국내선물도 KRX 거래일 기준이어야(_is_kr_symbol은 한글 선물을
    # 미국으로 오분류). instrument_region(KRX/US)을 캘린더 코드(KR/US)로 매핑.
    market = "KR" if _instrument_region(symbol) == "KRX" else "US"
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
        # KR도 같은 원리의 수집시각 cutoff가 필요하다. KR 종가 수집은 18:15 cron —
        # 그 전에 평가하면 "오늘 종가"는 아직 존재할 수 없으므로 직전 거래일이
        # 신선도 기준(여유 마진 포함 19:00 경계). 이전 코드(`today` 고정)는 새벽
        # 재배포 boot refresh 등이 preview를 rebuild할 때 가장 신선한 상태인 전일
        # 종가를 "1일 지연 stale"로 오판 → KR 후보 전멸·무발주 (2026-06-10 인시던트,
        # docs/incidents/2026-06-10-autotrading-week-retrospective.md RC-4/D4-2).
        ref_anchor = today - timedelta(days=1) if now_kst.hour < 19 else today
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
    if s.template is not None:
        # 자동매매 템플릿(장중 실시간 관측) — 신호가 당일 장중 데이터라 일봉 preview로는
        # 후보를 만들 수 없다(만들면 전일 데이터의 1일 지연 오답). 후보 결정은 로컬앱이
        # 소유한다(장중 템플릿 설계 §3.3·§4) — 여기선 대기 상태만 표면화. 아침 사이클도
        # 이 빈 후보를 받아 템플릿 전략을 시가창에서 진입하지 않는다.
        if s.template.id == "watchlist_trigger_v1":
            out["skipped"].append({"reason": "장중 트리거 대기 — 장중(09:00~15:30) "
                                              "로컬앱이 워치리스트 돌파를 감시해 진입합니다"})
        else:
            out["skipped"].append({"reason": "장중 스캔 대기 — 종가창(15:25)에 로컬앱이 "
                                              "실시간 스캔으로 진입 후보를 결정합니다"})
        return out
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
    # R-1 — 신호 참조 심볼(예: S&P500)이 stale이면 평가 전에 전략 단위로 차단(사유 표면화).
    _stale = _stale_signal_ref(dataset, needed_symbols(s), syms, datetime.now(_KST))
    if _stale is not None:
        out["skipped"].append({"symbol": _stale[0],
                               "reason": f"신호 참조 {_stale[1]}"})
        return out
    screener = u.screener or {}
    filt = (Node.model_validate(screener["condition"])
            if screener.get("condition") else None)
    ds = _scoped(dataset, syms, s.signal, filt, pos.overlays.group_label)
    ctx = EvalContext.from_dataset(ds, universe=syms)
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
            out["skipped"].append({"reason": (
                "정적 세부조건 바스켓이 비어 있습니다(전환 시점 매칭 종목 0) — "
                "조건을 확인하거나 전략을 재전환하세요." if not basket_set
                else "고정 바스켓 종목의 데이터가 없습니다.")})
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
    # 부호방향 directional·단방향 숏 지원 — longs·shorts 둘 다 후보로(각 direction 부착).
    # executor가 후보 direction으로 _submit_buy/_submit_open_short 분기(엔진 _direction_for 거울).
    longs, shorts = _select(alpha.loc[d], pos, is_condition)
    if not longs and not shorts:
        out["skipped"].append({"reason": "다음날 진입 신호 종목 없음"})
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

    rate = _commission_rate(s)   # 예상 수수료율(편도) — 주식 est_fee_krw 계산용
    now_kst = datetime.now(_KST)
    today_kst = now_kst.date()
    _cands = [(s, "long") for s in longs] + [(s, "short") for s in shorts]
    for sym, side in _cands:
        fresh, freshness_msg = _data_freshness_ok(dataset, sym, today_kst, now_kst=now_kst)
        if not fresh:
            out["skipped"].append({"symbol": sym, "reason": freshness_msg})
            continue
        prev_close = _prev_close(dataset, sym)
        if prev_close is None:
            out["skipped"].append({"symbol": sym, "reason": "전일 종가 없음"})
            continue
        meta = master_by_code.get(sym, {})
        spec = _instrument_spec(sym)
        if spec.asset_class == "futures":
            # 선물 — 서버는 선물계좌 가용증거금현금을 모른다(보안경계: KIS 자격증명·계좌가
            # 로컬 PC 전용) → 미국 종목과 동일하게 preview 사이징 불가(qty=None). 단 한글 선물
            # 표시심볼('코스피200선물')은 _is_kr_symbol(6자리 숫자)에 안 걸려 아래 US 분기로
            # 빠지면 통화가 'USD'로 오분류된다 → 계약 스펙의 통화로 명시(KOSPI200=KRW,
            # 해외 CME 선물=USD). 실제 사이징은 발주 시점 로컬앱이 선물계좌로 수행.
            # leverage/multiplier/margin_rate는 계약의 정적 사실(사이징 무관) — 사전 투명성으로 표시.
            mr = spec.init_margin_rate
            out["candidates"].append({
                "symbol": sym, "name": meta.get("name", ""), "qty": None,
                "prev_close": round(prev_close, 2), "est_limit_price": None,
                "est_total": None, "est_fee_krw": None, "sizing_mode": sz.mode,
                "data_as_of": _last_date(dataset, sym), "source": "ir",
                "currency": spec.currency, "direction": side,
                "leverage": round(1 / mr, 1) if mr else None,
                "multiplier": spec.multiplier, "margin_rate": mr,
                "note": "선물 — 발주 시점 로컬 선물계좌로 사이징 (preview 미지원)"})
            continue
        if not _is_kr_symbol(sym):
            out["candidates"].append({
                "symbol": sym, "name": meta.get("name", ""), "qty": None,
                "prev_close": round(prev_close, 2), "est_limit_price": None,
                "est_total": None, "est_fee_krw": None, "sizing_mode": sz.mode,
                "data_as_of": _last_date(dataset, sym), "source": "ir",
                "currency": "USD", "direction": side,
                "note": "미국 종목 — 발주 시점 USD 잔고로 사이징 (preview 미지원)"})
            continue
        if event_budget:
            qty = ir_live.event_buy_qty(s, cash=cash, prev_close=prev_close)
        else:
            budget = (float(weights[sym]) * cash
                      if weights is not None and sym in weights.index
                      else cash / max(len(longs), 1))
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
            "est_total": est_price * qty,
            "est_fee_krw": round(est_price * qty * rate), "sizing_mode": sz.mode,
            "data_as_of": _last_date(dataset, sym), "source": "ir",
            "currency": "KRW", "direction": side})
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


def _preview_dataset(defs: list, held_symbols: set) -> dict:
    """유저 전략들이 참조하는 컬럼·종목만 프로젝션한 dataset — 전 유니버스 45컬럼
    (~9.4GB) 빌드를 회피한다(preview cron OOM의 직접 원인이었던 경로).

    defs: 전략 definition dict 리스트(ORM 객체 아님 — C1: DB 커넥션 미점유 계산용).
    · 어떤 전략이든 컬럼 결정 불가(strat: 조합 등) → 전체(get_dataset) 안전 폴백(드묾).
    · all 전략이 하나라도 있으면 전 종목 × 참조컬럼 프로젝션.
    · 전부 single/list면 그 종목들 ∪ 보유종목만 프로젝션.
    보유종목 Close(청산 미리보기)는 OHLCV라 프로젝션에도 항상 포함된다.
    """
    union_cols: set = set()
    union_syms: set = set(held_symbols)
    full_universe = False
    for d in defs:
        try:
            sir = StrategyIR.model_validate(dict(d or {}))
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

    C1 (2026-06-02) — DB 커넥션을 무거운 계산(_preview_dataset 데이터셋 로드 +
    _evaluate_ir_strategy 신호평가, 콜드 시 수 초~수십 초) 동안 쥐지 않는다.
    외부 Neon 서버리스 Postgres가 유휴 연결을 suspend 중 끊으면 *늙은* 연결로의
    commit이 'psycopg ProtocolViolation: server conn crashed?'로 실패해 preview
    재생성(=매수 후보결정)이 통째로 누락되던 근본 결함을 제거한다. 구조:
      (A) 짧은 읽기 — 전략·스냅샷을 plain 값으로 추출
      (B) session.commit()으로 커넥션 반납 → 세션 미점유 상태로 순수 계산
      (C) 짧은 쓰기(basket persist)는 fresh checkout이라 pool_pre_ping(db.py)이
          stale 연결을 자동 폐기·재연결해 보호한다.
    무거운 계산 함수(_preview_dataset·_evaluate_ir_strategy·_evaluate_exits)는
    이미 DB 미접근 순수 함수이고 입력이 plain 값이라 호출 위치만 (B)로 옮기면 된다.

    Args:
        session: SQL 세션 (읽기/쓰기에만 잠깐 사용; 계산 중에는 미점유)
        user_id: 사용자 ID
        data_source: cron 식별자 ('dataset_global', 'manual', 'on_demand_pull' 등)
    """
    # ── (A) 읽기 — 필요한 모든 값을 plain 으로 추출 (ORM 객체를 계산까지 끌지 않음) ──
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

    # 전략을 plain dict 로 추출 — (B) 커넥션 반납 후 ORM detach 안전.
    rows = session.exec(
        select(Strategy).where(
            Strategy.user_id == user_id,
            Strategy.run_mode.in_(("paper", "live"))
        )
    ).all()
    ir_defs = [
        {"id": s.id, "run_mode": s.run_mode,
         "definition": dict(s.definition or {}),
         "live_basket": (list(s.live_basket) if s.live_basket is not None else None)}
        for s in rows if getattr(s, "engine", "operand") == "ir"
    ]

    # ── DB 커넥션 반납 — 무거운 계산 동안 어떤 연결도 쥐지 않는다(C1 핵심) ──────────
    # Neon 이 이 사이 유휴 연결을 끊어도, 아래 (C) 쓰기는 fresh checkout 이라
    # pool_pre_ping(db.py:30) 이 stale 연결을 자동 폐기·재연결한다.
    session.commit()

    # ── (B) 순수 계산 — 세션/커넥션 미점유 ───────────────────────────────────────
    # KIS 마스터 lookup (종목명 표시용)
    master_list = kis_master_cache.get_master_list()
    master_by_code = {m["symbol"]: m for m in master_list}

    # 데이터셋 — 이 유저 전략들이 실제 쓰는 컬럼·종목만(컬럼 프로젝션). 전 유니버스 빌드 회피.
    dataset = _preview_dataset([d["definition"] for d in ir_defs], held_symbols)

    by_strategy = []
    total_buy_amount = 0
    n_buy_candidates = 0
    basket_updates: dict = {}       # {strategy_id: [symbols]} — once_at_start 정적 바스켓 lazy 형성

    for d in ir_defs:
        strat_def = dict(d["definition"])
        strat_def["_id"] = d["id"]
        result = _evaluate_ir_strategy(strat_def, dataset, cash, held_symbols,
                                       master_by_code, live_basket=d["live_basket"])
        result["strategy_id"] = d["id"]
        result["run_mode"] = d["run_mode"]
        by_strategy.append(result)

        # Task 12b — 정적 세부조건(once_at_start) 라이브 바스켓 lazy 형성.
        # 바스켓이 아직 없고(None) 이번 평가가 당일 자격집합(screener_members)을 산출했으면
        # 그 집합으로 고정한다. 실제 persist 는 (C) 쓰기 단계에서 fresh 세션으로 수행.
        screener = (strat_def.get("universe") or {}).get("screener") or {}
        if (d["run_mode"] in ("paper", "live")
                and screener.get("refresh") == "once_at_start"
                and screener.get("condition")
                and d["live_basket"] is None):
            # 매칭 0이면 빈 바스켓([])으로 고정(거래 없음); preview가 경고 노출, 재전환 시 재형성.
            basket_updates[d["id"]] = list(result.get("screener_members") or [])

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

    # 청산 미리보기 (DB 미접근 순수 계산)
    exit_candidates = _evaluate_exits(positions, dataset, master_by_code)

    # ── (C) 짧은 쓰기 — 형성된 정적 바스켓 persist (fresh checkout → pool_pre_ping 보호) ──
    # caller(cron·sync·manual)는 이후 snapshot.next_day_preview 머지를 별도 commit한다.
    if basket_updates:
        for sid, basket in basket_updates.items():
            st = session.get(Strategy, sid)
            if st is not None and st.live_basket is None:   # 동시 설정 가드(재조회로 최신 확인)
                st.live_basket = basket
                session.add(st)
        session.commit()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": data_source,
        "available": True,
        # 로드맵 C — 후보 계산에 쓰인 데이터의 시장별 기준 거래일. 로컬앱이
        # 사용 직전 "직전 거래일 이상인가"를 검증해 묵은 후보 진입을 차단한다.
        "data_as_of": _dataset_as_of(dataset),
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
