"""Catch-up — PC 꺼져 있어 missed된 cycle/settlement을 기동 시 자동 보완.

설계 원칙:
  - cycles.jsonl 기반 idempotency — 이미 실행된 cycle은 다시 안 함.
  - _CYCLE_LOCK 공유 (trader) — 정상 cron cycle과 race 방지.
  - 자금 안전 우선 — 매수 catch-up은 ref_price(어제 종가) × (1 + tol%) limit으로 발주.
    시간 무관 동일 가격이라 백테스트 alignment + selection bias 없음.
  - 시장가 매수는 catch-up 시 시초가 limit으로 자동 변환 (Phase 3에서 trader 분기).

핵심 시점 판단:
  - cycles.jsonl 최근 entry에 summary["market"]·["kind"] 명시되어 있으면 우선 사용.
  - 명시 누락(기존 entry) 시 ts 시각대로 추정 fallback — 평일 08:55~09:30 entry는
    KRX cycle, 15:35 이후는 KRX settlement로 간주. Phase 4에서 trader가 명시
    set하면 fallback 사용 빈도 자연 감소.

호출 흐름:
  scheduler.register_jobs() 끝 → background thread → run_catchup_on_startup()
    → _decide_catchup_plan() → 각 catch-up action 실행 (Phase 2~4에서 추가).

Phase 1 (이 commit): skeleton + plan 판단만. 실제 실행은 Phase 2~4.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from quant_core import market_calendar as mc

from .config import APP_DIR, CYCLES_PATH

# GUI가 polling으로 읽고 amber 배너 표시하는 결과 파일.
# 사용자가 [확인] 클릭하면 gui가 unlink.
CATCHUP_RESULT_PATH = APP_DIR / "catchup_result.json"

log = logging.getLogger("localapp.catchup")
KST = ZoneInfo("Asia/Seoul")

# 최근 entry만 조회 — 너무 옛 entry는 무관. 약 1주일치 cycle.
_RECENT_LIMIT = 100


@dataclass
class CatchupPlan:
    """기동 시점에 결정된 catch-up action 목록.

    각 *_needed 플래그가 True면 해당 catch-up을 실행해야 함. 실행은 Phase
    2~4에서 추가되는 _catchup_* 함수들이 담당.
    """
    # Settlement (장 마감 후 정산) catch-up
    krx_settlement_needed: bool = False
    krx_settlement_date: str | None = None    # 정산 대상 거래일 ISO
    us_settlement_needed: bool = False
    us_settlement_date: str | None = None

    # Full cycle catch-up (장중 PC 켰을 때)
    krx_cycle_needed: bool = False
    us_cycle_needed: bool = False

    # 손절 catch-up (장중 보유 종목 즉시 체크)
    krx_stop_loss_check: bool = False
    us_stop_loss_check: bool = False

    # 디버그·로그용 — 어떤 entry 보고 판단했는지
    reasons: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        return any((self.krx_settlement_needed, self.us_settlement_needed,
                    self.krx_cycle_needed, self.us_cycle_needed,
                    self.krx_stop_loss_check, self.us_stop_loss_check))

    def __str__(self) -> str:
        parts = []
        if self.krx_settlement_needed:
            parts.append(f"KRX settle({self.krx_settlement_date})")
        if self.us_settlement_needed:
            parts.append(f"US settle({self.us_settlement_date})")
        if self.krx_cycle_needed:
            parts.append("KRX cycle")
        if self.us_cycle_needed:
            parts.append("US cycle")
        if self.krx_stop_loss_check:
            parts.append("KRX stop-loss")
        if self.us_stop_loss_check:
            parts.append("US stop-loss")
        return ", ".join(parts) if parts else "(none)"


def _read_recent_cycles() -> list[dict]:
    """cycles.jsonl 끝에서 최근 _RECENT_LIMIT entry 읽기."""
    if not CYCLES_PATH.exists():
        return []
    try:
        lines = CYCLES_PATH.read_text(encoding="utf-8").splitlines()[-_RECENT_LIMIT:]
        out: list[dict] = []
        for ln in lines:
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    # 손상된 line은 skip. 다른 entry로 판단.
                    continue
        return out
    except OSError as e:
        log.warning("cycles.jsonl 읽기 실패 — catch-up 보수적으로 진행: %s", e)
        return []


def _entry_ts(entry: dict) -> datetime | None:
    ts = entry.get("ts")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone(KST)
    except (ValueError, TypeError):
        return None


def _classify_entry(entry: dict) -> tuple[str | None, str | None]:
    """entry → (market, kind) 추출. 명시 우선, 없으면 ts 시각대로 추정.

    명시 필드 (Phase 4 이후 trader가 set):
      summary["market"] ∈ {"KRX", "US"}
      summary["kind"]   ∈ {"cycle", "post_close_settlement",
                            "catchup_cycle", "catchup_settlement"}

    Fallback 추정 (기존 entry):
      평일 KST 08:50~09:30  → ("KRX", "cycle")
      평일 KST 15:30~16:00  → ("KRX", "post_close_settlement")
      KST 22:00~05:00       → ("US", "cycle" or "post_close_settlement")
      그 외 시각            → 분류 불가, None 반환

    분류 불가 entry는 catch-up 판단에서 무시 (안전 default — 잘못된 추정으로
    catch-up trigger를 막는 것보다 trigger 안 하는 게 더 위험하지만, 이건 다음
    cycle에서 자동 보완됨).
    """
    s = entry.get("summary") or {}
    market = s.get("market")
    kind = s.get("kind")
    if market in ("KRX", "US") and kind:
        return market, kind

    # Fallback 추정
    ts = _entry_ts(entry)
    if ts is None:
        return None, None
    t = ts.time()
    weekday = ts.weekday()  # 0=월

    # 평일 KRX cycle (08:50~09:30 KST)
    if weekday < 5 and time(8, 50) <= t <= time(9, 30):
        return "KRX", "cycle"
    # 평일 KRX settlement (15:30~16:00 KST)
    if weekday < 5 and time(15, 30) <= t <= time(16, 0):
        return "KRX", "post_close_settlement"
    # US cycle/settlement (KST 22:00~다음날 06:00 — 야간 윈도우)
    # cycle은 open-5분, settlement은 close+5분. 정확 구분 어려워 둘 다 가능성.
    # 가장 안전: settlement만 인정 (cycle 추정은 false positive 위험 큼 — US 장
    # 중간에 다른 작업도 시간대 겹침).
    if (weekday < 6 and (t >= time(22, 0) or t <= time(6, 0))):
        # close+5분 ≈ KST 05:00~06:30 → settlement
        if time(5, 0) <= t <= time(6, 30):
            return "US", "post_close_settlement"
        # open-5분 ≈ KST 22:25~23:25 (DST 따라) → cycle
        if time(22, 0) <= t <= time(23, 30):
            return "US", "cycle"
    return None, None


def _last_of(entries: list[dict], market: str, kind: str) -> datetime | None:
    """entries 중 (market, kind) 매칭하는 가장 최근 ts."""
    for e in reversed(entries):
        m, k = _classify_entry(e)
        if m == market and k == kind:
            return _entry_ts(e)
    return None


def _is_krx_intraday(now: datetime) -> bool:
    """KRX 정규장 시간(평일 09:00~15:30 KST + 영업일)."""
    try:
        if not mc.is_session_day("KR", now.date()):
            return False
    except mc.CalendarError:
        # 캘린더 stale — 보수적으로 평일 체크만
        if now.weekday() >= 5:
            return False
    return time(9, 0) <= now.time() <= time(15, 30)


def _is_us_intraday(now: datetime) -> bool:
    """US 정규장 시간 (매일 동적 — DST·휴장 반영)."""
    try:
        sess = mc.next_session_kst("US", now - timedelta(hours=20))
    except mc.CalendarError:
        return False
    if sess is None:
        return False
    open_kst, close_kst = sess
    return open_kst <= now <= close_kst


def _recent_krx_business_day(now: datetime) -> date | None:
    """오늘부터 거슬러 가장 최근 KRX 영업일. 오늘이 영업일이면 오늘."""
    today = now.date()
    for delta in range(7):
        d = today - timedelta(days=delta)
        try:
            if mc.is_session_day("KR", d):
                return d
        except mc.CalendarError:
            return None
    return None


def _decide_catchup_plan(now: datetime | None = None) -> CatchupPlan:
    """현재 시점·cycles.jsonl 기반 catch-up plan 결정.

    Idempotency: 이미 실행된 cycle/settlement은 다시 안 함 (cycles.jsonl 기준).
    Conservative: 판단 애매하면 trigger 안 함 (정상 cron이 곧 처리할 것).
    """
    now = now or datetime.now(KST)
    plan = CatchupPlan()
    entries = _read_recent_cycles()

    # ── KRX settlement (15:35 cron) catch-up 필요 판단 ────────────────────
    # 가장 최근 KRX 영업일이 settle 대상. 오늘이 영업일이면 15:35 지났는지 확인.
    last_krx_biz = _recent_krx_business_day(now)
    if last_krx_biz is not None:
        # "오늘이 영업일인데 15:35 안 됐으면" 그 날짜는 settlement 대상 아님
        # → 어제(또는 그 전 영업일)로 후퇴
        if last_krx_biz == now.date() and now.time() < time(15, 35):
            # 어제 또는 직전 영업일
            for delta in range(1, 7):
                d = now.date() - timedelta(days=delta)
                try:
                    if mc.is_session_day("KR", d):
                        last_krx_biz = d
                        break
                except mc.CalendarError:
                    last_krx_biz = None
                    break
            else:
                last_krx_biz = None

        if last_krx_biz is not None:
            last_settle = _last_of(entries, "KRX", "post_close_settlement")
            if last_settle is None or last_settle.date() < last_krx_biz:
                plan.krx_settlement_needed = True
                plan.krx_settlement_date = last_krx_biz.isoformat()
                plan.reasons.append(
                    f"KRX settlement 누락 — 마지막 settle={last_settle}, "
                    f"대상={last_krx_biz}")

    # ── US settlement (close+5분) catch-up 필요 판단 ──────────────────────
    # 가장 최근 US 세션 close가 5분 이상 지났는데 settle 없으면 trigger.
    try:
        prev_sess = mc.next_session_kst("US", now - timedelta(hours=30))
    except mc.CalendarError:
        prev_sess = None
    if prev_sess is not None:
        _, prev_close_kst = prev_sess
        if (prev_close_kst + timedelta(minutes=5)) < now:
            last_us_settle = _last_of(entries, "US", "post_close_settlement")
            if (last_us_settle is None
                    or last_us_settle.date() < prev_close_kst.date()):
                plan.us_settlement_needed = True
                plan.us_settlement_date = prev_close_kst.date().isoformat()
                plan.reasons.append(
                    f"US settlement 누락 — 마지막 settle={last_us_settle}, "
                    f"대상close={prev_close_kst}")

    # ── KRX 장중 catch-up (cycle + 손절) ─────────────────────────────────
    if _is_krx_intraday(now):
        last_cycle = _last_of(entries, "KRX", "cycle")
        if last_cycle is None or last_cycle.date() < now.date():
            plan.krx_cycle_needed = True
            plan.reasons.append(
                f"KRX cycle 누락 (장중) — 마지막 cycle={last_cycle}")
        # 손절은 cycle 유무와 무관하게 항상 체크 (보유 종목 즉시 위험 평가).
        # Phase 2에서 실제 실행 시 broker가 보유 종목 0건이면 자동 skip.
        plan.krx_stop_loss_check = True
        plan.reasons.append("KRX 장중 — 보유 종목 손절선 즉시 체크")

    # ── US 장중 catch-up (cycle + 손절) ──────────────────────────────────
    if _is_us_intraday(now):
        last_cycle = _last_of(entries, "US", "cycle")
        if last_cycle is None or last_cycle.date() < now.date():
            plan.us_cycle_needed = True
            plan.reasons.append(
                f"US cycle 누락 (장중) — 마지막 cycle={last_cycle}")
        plan.us_stop_loss_check = True
        plan.reasons.append("US 장중 — 보유 종목 손절선 즉시 체크")

    return plan


def _catchup_cycle(market: str) -> dict:
    """C11 — 장중 PC 켰을 때 missed cycle 1회 실행.

    runner.run_cycle(market, catchup=True)을 그대로 호출. trader가:
      - 지정가 매수: ref_price(어제 종가) × (1 + tol%) 그대로 → 백테스트 alignment
      - 시장가 매수: 시초가 × (1 + bt_slippage_bps) limit으로 자동 변환

    선택 편향·시간 효과 우려는 없음 — 가격 기준이 시간과 무관(어제 종가 또는
    당일 시가) fixed.

    Returns: run_cycle 결과 payload 또는 {"error": str}.
    """
    from .runner import run_cycle
    try:
        return run_cycle(market=market, catchup=True)
    except Exception as e:
        log.exception("catch-up cycle [%s] 실패: %s", market, e)
        return {"error": str(e), "market": market, "kind": "catchup_cycle"}


def _catchup_settlement(market: str, settle_date: str | None = None) -> dict:
    """C1·C2 — 장 마감 후 missed settlement 실행. 최대 3회 retry.

    runner.run_post_close_settlement(market) 호출. 미체결 정리·KIS reconcile·
    잔고 push 모두 수행. settle_date는 로깅용 (어느 거래일의 settlement인지).

    Settlement은 daily 1회만 발생이라 미루면 영향 큼 → KIS 일시 거부(네트워크·
    rate limit) 대비 짧은 retry. 30초 간격 ×3회. background thread라 UI block X.

    Returns: settlement 결과 또는 {"error": ..., "retries_exhausted": True}.
    """
    import time
    from .runner import run_post_close_settlement
    log.info("catch-up settlement [%s] 대상=%s", market, settle_date)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            return run_post_close_settlement(market=market)
        except Exception as e:
            last_err = e
            log.warning("catch-up settlement [%s] 시도 %d/3 실패: %s",
                         market, attempt, e)
            if attempt < 3:
                time.sleep(30)
    log.error("catch-up settlement [%s] 3회 모두 실패 — 사용자 수동 개입 필요",
               market)
    return {"error": f"3회 retry 실패: {last_err}", "market": market,
            "settle_date": settle_date, "retries_exhausted": True}


def _catchup_stop_loss(market: str, broker, trader,
                        get_strat_def) -> dict:
    """C9 — 보유 종목 현재가 일괄 조회 → IntradayStopManager.on_tick 평가+발주.

    IntradayStopManager.on_tick은 이미:
      - sell_rules로 익절/손절/트레일/ATR trigger 평가
      - L-04 over-sell 방지 (KIS 실 잔고 클램프)
      - _submit_sell 호출 (intent journal 포함)
      - sold_today 기록 (중복 발주 방지)
    모두 한다. catch-up은 단순히 보유 종목마다 on_tick 1번씩 호출하면 끝.

    정상 loop이 아직 안 돌고 있는 경우용 — loop이 돌고 있으면 어차피 WebSocket
    tick으로 실시간 평가 중이라 catch-up 불필요.

    Returns: {"checked": int, "fired": int, "decisions": list, "error": str|None}
    """
    from .intraday_stop import IntradayStopManager
    from .market_index import market_group_of
    from .trader import _CYCLE_LOCK

    try:
        snap = broker.account_snapshot()
    except Exception as e:
        log.error("catch-up stop-loss [%s] account_snapshot 실패: %s", market, e)
        return {"checked": 0, "fired": 0, "decisions": [], "error": str(e)}

    positions = [p for p in snap.get("positions", [])
                  if p.get("symbol")
                  and market_group_of(p["symbol"]) == market]
    if not positions:
        log.info("catch-up stop-loss [%s] 보유 종목 0건 — skip", market)
        return {"checked": 0, "fired": 0, "decisions": [], "error": None}

    manager = IntradayStopManager(broker, lambda: trader.ledger,
                                    get_strat_def)
    fired_before = len(manager.decisions)

    # Q5(AL-4): _CYCLE_LOCK으로 정상 cycle·settlement과 직렬화.
    with _CYCLE_LOCK:
        for pos in positions:
            symbol = pos["symbol"]
            try:
                cur = broker.price(symbol)
            except Exception as e:
                log.warning("catch-up stop-loss [%s] %s 현재가 조회 실패: %s",
                             market, symbol, e)
                continue
            if cur <= 0:
                log.warning("catch-up stop-loss [%s] %s 현재가 0/음수 — skip",
                             market, symbol)
                continue
            manager.on_tick(symbol, cur)

    fired = len(manager.decisions) - fired_before
    log.info("catch-up stop-loss [%s] checked=%d fired=%d",
              market, len(positions), fired)
    return {"checked": len(positions), "fired": fired,
            "decisions": list(manager.decisions), "error": None}


def _prepare_helpers() -> tuple[object, object, object] | None:
    """catch-up용 broker·trader·get_strat_def 준비. 실패 시 None.

    KIS 자격증명 없으면 make_broker가 RuntimeError → catch-up abort (안전).
    전략 pull 실패는 손절은 가능(전략 정의 불필요 — IntradayStopManager가 ledger
    pos에 strategy_id 보고 strat_def 조회) — 따라서 빈 dict로 fallback해도 손절은
    동작. cycle catch-up은 preview·전략 필요 → run_cycle 내부에서 다시 시도.
    """
    try:
        from .runner import make_broker
        from .trader import Trader
        from .sync_client import pull_strategies
    except Exception as e:
        log.error("catch-up: import 실패 — abort: %s", e)
        return None

    try:
        broker = make_broker()
    except Exception as e:
        log.warning("catch-up: broker 생성 실패 (KIS 자격증명 미등록?) — abort: %s", e)
        return None

    try:
        trader = Trader(broker)
    except Exception as e:
        log.error("catch-up: Trader 생성 실패: %s", e)
        return None

    try:
        strategies = pull_strategies()
        sdict = {str(s["id"]): s.get("definition", {}) for s in strategies}
    except Exception as e:
        log.warning("catch-up: 전략 pull 실패 — 손절 catch-up은 진행, cycle은 "
                     "run_cycle 내부 retry 의존: %s", e)
        sdict = {}

    def get_strat_def(sid: str):
        return sdict.get(str(sid))

    return broker, trader, get_strat_def


def run_catchup_on_startup() -> dict:
    """기동 시 1회 호출 — plan 결정 + 실행. scheduler.register_jobs() 끝에서
    background thread로 spawn.

    Returns: {"plan": CatchupPlan, "results": dict[str, dict]}
      results 키: "krx_stop_loss" / "us_stop_loss" / "krx_cycle" / "us_cycle"
                  / "krx_settle" / "us_settle". Phase 5에서 GUI가 results를 읽어
                  amber 배너로 사용자에게 표시.
    """
    plan = _decide_catchup_plan()
    results: dict[str, dict] = {}

    if not plan.has_any():
        log.info("catch-up plan 없음 (모든 cycle 정상 또는 윈도우 밖)")
        return {"plan": plan, "results": results}

    log.info("catch-up plan: %s", plan)
    for r in plan.reasons:
        log.info("  reason: %s", r)

    helpers = _prepare_helpers()
    if helpers is None:
        log.info("catch-up: helpers 준비 실패 — plan만 결정하고 종료")
        return {"plan": plan, "results": results}
    broker, trader, get_strat_def = helpers

    # 1) settlement catch-up 먼저 (어제·전일 누락 정리 → 오늘 cycle의 stale state
    #    위험 제거). 순서: KRX→US (사용자 한국 거주 가정 — KRX 우선 표시).
    if plan.krx_settlement_needed:
        try:
            results["krx_settle"] = _catchup_settlement(
                "KRX", plan.krx_settlement_date)
        except Exception as e:
            log.exception("catch-up settlement KRX 실행 실패: %s", e)
            results["krx_settle"] = {"error": str(e)}
    if plan.us_settlement_needed:
        try:
            results["us_settle"] = _catchup_settlement(
                "US", plan.us_settlement_date)
        except Exception as e:
            log.exception("catch-up settlement US 실행 실패: %s", e)
            results["us_settle"] = {"error": str(e)}

    # 2) 손절 catch-up — 자금 안전 가장 직접적. cycle보다 먼저.
    if plan.krx_stop_loss_check:
        try:
            results["krx_stop_loss"] = _catchup_stop_loss(
                "KRX", broker, trader, get_strat_def)
        except Exception as e:
            log.exception("catch-up stop-loss KRX 실행 실패: %s", e)
            results["krx_stop_loss"] = {"error": str(e)}
    if plan.us_stop_loss_check:
        try:
            results["us_stop_loss"] = _catchup_stop_loss(
                "US", broker, trader, get_strat_def)
        except Exception as e:
            log.exception("catch-up stop-loss US 실행 실패: %s", e)
            results["us_stop_loss"] = {"error": str(e)}

    # 3) full cycle catch-up (장중 missed) — 진입+청산. 마지막 (가장 무거움).
    if plan.krx_cycle_needed:
        try:
            results["krx_cycle"] = _catchup_cycle("KRX")
        except Exception as e:
            log.exception("catch-up cycle KRX 실행 실패: %s", e)
            results["krx_cycle"] = {"error": str(e)}
    if plan.us_cycle_needed:
        try:
            results["us_cycle"] = _catchup_cycle("US")
        except Exception as e:
            log.exception("catch-up cycle US 실행 실패: %s", e)
            results["us_cycle"] = {"error": str(e)}

    log.info("catch-up 실행 완료 — results=%s", list(results.keys()))

    # 결과 파일 저장 — gui가 polling으로 읽고 amber 배너 표시.
    if results:
        _save_result(plan, results)

    return {"plan": plan, "results": results}


def _save_result(plan: CatchupPlan, results: dict) -> None:
    """catch-up 결과를 사용자가 볼 수 있게 파일 저장.

    decisions·payload는 너무 길어 통계만 추출. gui가 _format_catchup_summary로
    한 줄 메시지 만들어 amber 배너에 표시. 사용자가 [확인] 클릭하면 unlink.
    """
    serializable = {
        "ts": datetime.now(KST).isoformat(timespec="seconds"),
        "plan": str(plan),
        "results": {},
    }
    for k, v in results.items():
        out: dict = {"error": v.get("error")} if v.get("error") else {}
        if k.endswith("_stop_loss"):
            out["checked"] = v.get("checked", 0)
            out["fired"] = v.get("fired", 0)
        elif k.endswith("_cycle"):
            cs = v.get("cycle_summary") or {}
            out["n_bought"] = cs.get("n_bought", 0)
            out["n_sold"] = cs.get("n_sold", 0)
        elif k.endswith("_settle"):
            recon = v.get("reconciliation") or {}
            out["reconcile_drift"] = bool(recon.get("has_drift"))
            out["reconcile_applied"] = len(recon.get("applied") or [])
        serializable["results"][k] = out

    try:
        CATCHUP_RESULT_PATH.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        log.warning("catch-up 결과 저장 실패: %s", e)
