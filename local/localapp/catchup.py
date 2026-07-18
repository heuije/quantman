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
    KRX cycle, 장 마감 후(15:50 cron) entry는 KRX settlement로 간주. Phase 4에서
    trader가 명시 set하면 fallback 사용 빈도 자연 감소.

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
from .state_store import save_json

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
    krx_settlement_days_missed: int = 0       # v0.9.13 D-2 — 며칠 누락
    us_settlement_needed: bool = False
    us_settlement_date: str | None = None
    us_settlement_days_missed: int = 0        # v0.9.13 D-2 — 며칠 누락

    # Full cycle catch-up (장중 PC 켰을 때)
    krx_cycle_needed: bool = False
    us_cycle_needed: bool = False

    # 손절 catch-up (장중 보유 종목 즉시 체크)
    krx_stop_loss_check: bool = False
    us_stop_loss_check: bool = False

    # R4-③ — KRX 종가창 catch-up: 창내 기동 시 놓친 종가 사이클 클래스 목록
    # ("stock"/"futures"). 창 밖 잔존 위험은 정산 I5(daytrade_unclosed)가 표면화.
    krx_close_classes: list = field(default_factory=list)

    # 디버그·로그용 — 어떤 entry 보고 판단했는지
    reasons: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        return any((self.krx_settlement_needed, self.us_settlement_needed,
                    self.krx_cycle_needed, self.us_cycle_needed,
                    self.krx_stop_loss_check, self.us_stop_loss_check,
                    bool(self.krx_close_classes)))

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


def _last_of(entries: list[dict], market: str,
              kind: str | tuple[str, ...]) -> datetime | None:
    """entries 중 (market, kind) 매칭하는 가장 최근 ts.

    kind는 단일 문자열 또는 tuple — tuple이면 어느 하나와 매칭. cycle vs
    catchup_cycle 같이 의미상 동등한 kind들을 같이 매칭하는 데 사용 (v0.9.12).

    v0.9.13 D-1 — summary.error 있는 entry는 "실행 완료"로 보지 않음. cycle 외부
    예외(데이터 fetch 실패·KIS 일시 거부 등)로 종료된 cycle이 cycles.jsonl에 남았을
    때 catch-up이 자동 재시도 가능. 자금 안전은 L-01 intent journal idempotency가
    차단 (submitting state로 끝난 intent는 KIS 당일 주문 매칭으로 마감).
    실제로 runner.run_cycle outer try/except 경로는 log_cycle 호출 안 함 → 보통
    error entry는 안 남지만, trader.cycle 내부 log_cycle 직후의 예외나 settlement
    오류 등 edge case 대비 안전망.
    """
    kinds = (kind,) if isinstance(kind, str) else kind
    for e in reversed(entries):
        # error entry는 "실행 완료"로 보지 않음 — 재시도 허용
        summary = e.get("summary") or {}
        if summary.get("error"):
            continue
        m, k = _classify_entry(e)
        if m == market and k in kinds:
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


def _count_business_days_missed(market: str, last_settle_date: date | None,
                                  target_date: date) -> int:
    """v0.9.13 D-2 — last_settle_date 이후 ~ target_date(포함) 사이의 영업일 수.

    last_settle_date가 None이면 (catch-up 이력 자체 없음) target_date 기준 최대
    7영업일 거슬러 올라가며 카운트 — 신규 사용자의 경우 너무 길게 표시 안 되도록.
    캘린더 만료 등으로 판정 불가 시 0 반환 (안전 default).

    호출자는 "단일 settle 호출로 catch-up 충분"하다는 사실은 알고, 이 카운트는
    *사용자 가시성* (며칠 누락이라는 사실)만을 위해 사용.
    """
    try:
        if last_settle_date is None:
            # 이력 없음 — 7영업일 윈도우만 카운트.
            count = 0
            d = target_date
            for _ in range(14):  # 최대 2주 거슬러
                if mc.is_session_day(market, d):
                    count += 1
                    if count >= 7:
                        return count
                d = d - timedelta(days=1)
            return count
        # target_date 부터 last_settle_date 까지 거꾸로
        count = 0
        d = target_date
        while d > last_settle_date:
            if mc.is_session_day(market, d):
                count += 1
            d = d - timedelta(days=1)
            if count >= 30:  # 안전 상한
                break
        return count
    except mc.CalendarError:
        return 0


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

    # ── KRX settlement (15:50 cron) catch-up 필요 판단 ────────────────────
    # 가장 최근 KRX 영업일이 settle 대상. 오늘이 영업일이면 15:50 지났는지 확인.
    # θ: 임계는 scheduler.py 정산 cron(15:50)과 정합 — 15:35로 두면 선물 종가청산
    # (15:40 발주→단일가 15:45 체결) 확인 전에 catch-up reconcile이 돈다(역순 재생산).
    last_krx_biz = _recent_krx_business_day(now)
    if last_krx_biz is not None:
        # "오늘이 영업일인데 15:50 안 됐으면" 그 날짜는 settlement 대상 아님
        # → 어제(또는 그 전 영업일)로 후퇴
        if last_krx_biz == now.date() and now.time() < time(15, 50):
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
                # v0.9.13 D-2 — 며칠 누락됐는지 계산. 단일 settle 호출로
                # current-state reconcile 완료되지만, 사용자에게 "5일 누적
                # 미정산" 같은 가시성 제공. KRX 영업일만 카운트.
                plan.krx_settlement_days_missed = _count_business_days_missed(
                    "KR", last_settle.date() if last_settle else None,
                    last_krx_biz)
                plan.reasons.append(
                    f"KRX settlement 누락 — 마지막 settle={last_settle}, "
                    f"대상={last_krx_biz}, "
                    f"누락 영업일={plan.krx_settlement_days_missed}")

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
                # v0.9.13 D-2 — US 영업일 카운트.
                plan.us_settlement_days_missed = _count_business_days_missed(
                    "US", last_us_settle.date() if last_us_settle else None,
                    prev_close_kst.date())
                plan.reasons.append(
                    f"US settlement 누락 — 마지막 settle={last_us_settle}, "
                    f"대상close={prev_close_kst}, "
                    f"누락 영업일={plan.us_settlement_days_missed}")

    # ── KRX 종가창 catch-up (R4-③) — 창내 기동 시 놓친 종가 사이클 ────────
    # 종전엔 catchup 항목 자체가 없어 종가창(15:25/15:40) misfire·앱 다운이
    # 곧 당일매매 오버나이트(07-16 실측 부류)였다. 창(주식 15:20~15:30 /
    # 선물 15:35~15:45) 안에서 기동했고 오늘 그 클래스의 day_trade_close
    # 무-error 기록이 없으면 재실행한다(멱등 — 넷팅·intent 게이트).
    try:
        _kr_open_today = mc.is_session_day("KR", now.date())
    except Exception:
        _kr_open_today = False
    if now.weekday() < 5 and _kr_open_today:
        for _cls, _lo, _hi in (("stock", time(15, 20), time(15, 30)),
                               ("futures", time(15, 35), time(15, 45))):
            if not (_lo <= now.time() <= _hi):
                continue
            done = False
            for e in entries:
                s = e.get("summary") or {}
                ts = _entry_ts(e)
                if (s.get("kind") == "day_trade_close"
                        and s.get("market") == "KRX" and not s.get("error")
                        and s.get("instrument_class") in (_cls, None)
                        and ts is not None and ts.date() == now.date()):
                    done = True
                    break
            if not done:
                plan.krx_close_classes.append(_cls)
                plan.reasons.append(
                    f"KRX 종가창({_cls}) 창내 미실행 — 재실행 (오버나이트 방지)")

    # ── KRX 장중 catch-up (cycle + 손절) ─────────────────────────────────
    if _is_krx_intraday(now):
        # v0.9.12 — cycle + catchup_cycle 둘 다 매칭. 옛 코드는 catchup_cycle을
        # 무시해서 catch-up 후 PC 재부팅 시 또 catch-up 실행 (중복 자원 낭비).
        # 자금 안전은 L-01 intent journal idempotency가 차단했으나 cycle 자체 낭비.
        last_cycle = _last_of(entries, "KRX", ("cycle", "catchup_cycle"))
        # 아침 자산군 분리(선물 08:35 / 주식 08:55 — 문제 10) 이후 "오늘 KRX cycle
        # 있음"만으론 부족: 선물 사이클만 돌고 꺼진 날 주식 catchup이 억제된다.
        # 완료 = full-scope(None — catchup/구버전) 1회 or {stock, futures} 각 1회.
        # catchup 실행은 full-scope(run_cycle instrument_class=None)라 목표수렴
        # 멱등성으로 이미 돈 클래스를 재실행해도 무해(net≈0·intent 게이트).
        done_classes: set = set()
        for e in entries:
            summary = e.get("summary") or {}
            if summary.get("error"):
                continue
            m, k = _classify_entry(e)
            ts = _entry_ts(e)
            if (m == "KRX" and k in ("cycle", "catchup_cycle")
                    and ts is not None and ts.date() == now.date()):
                done_classes.add(summary.get("instrument_class"))
        covered = (None in done_classes) or ({"stock", "futures"} <= done_classes)
        if last_cycle is None or last_cycle.date() < now.date() or not covered:
            plan.krx_cycle_needed = True
            plan.reasons.append(
                f"KRX cycle 누락 (장중) — 마지막 cycle={last_cycle}, "
                f"완료 클래스={sorted(str(c) for c in done_classes)}")
        # 손절은 cycle 유무와 무관하게 항상 체크 (보유 종목 즉시 위험 평가).
        # Phase 2에서 실제 실행 시 broker가 보유 종목 0건이면 자동 skip.
        plan.krx_stop_loss_check = True
        plan.reasons.append("KRX 장중 — 보유 종목 손절선 즉시 체크")

    # ── US 장중 catch-up (cycle + 손절) ──────────────────────────────────
    if _is_us_intraday(now):
        # v0.9.12 — cycle + catchup_cycle 둘 다 매칭 (위 KRX와 동일 사유).
        last_cycle = _last_of(entries, "US", ("cycle", "catchup_cycle"))
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
        return run_cycle(market=market, catchup=True, trigger="catchup")
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


def _catchup_stop_loss(market: str, broker, trader) -> dict:
    """C9 — 보유 종목 현재가 일괄 조회 → IntradayStopManager.on_tick 평가+발주.

    IntradayStopManager.on_tick은 이미:
      - IR position.exit로 익절/손절/트레일/ATR trigger 평가
      - L-04 over-sell 방지 (KIS 실 잔고 클램프)
      - _submit_sell 호출 (intent journal 포함)
      - sold_today 기록 (중복 발주 방지)
    모두 한다. catch-up은 단순히 보유 종목마다 on_tick 1번씩 호출하면 끝.

    정상 loop이 아직 안 돌고 있는 경우용 — loop이 돌고 있으면 어차피 WebSocket
    tick으로 실시간 평가 중이라 catch-up 불필요.

    Returns: {"checked": int, "fired": int, "decisions": list, "error": str|None}
    """
    from .intraday_stop import IntradayStopManager
    from .trader import _CYCLE_LOCK, _market_group_safe

    try:
        snap = broker.account_snapshot()
    except Exception as e:
        log.error("catch-up stop-loss [%s] account_snapshot 실패: %s", market, e)
        return {"checked": 0, "fired": 0, "decisions": [], "error": str(e)}

    # 시장 분류는 _market_group_safe(단일 출처) — 한 종목의 RoutingError(마스터 미로드
    # 등)가 raw market_group_of에서 전파돼 catch-up 손절 전체를 abort하던 비대칭 제거.
    # EOD cycle 청산(_cycle_body)과 동일 헬퍼. 미해결 종목은 KRX로 안전 기본.
    positions = [p for p in snap.get("positions", [])
                  if p.get("symbol")
                  and _market_group_safe(p["symbol"]) == market]
    if not positions:
        log.info("catch-up stop-loss [%s] 보유 종목 0건 — skip", market)
        return {"checked": 0, "fired": 0, "decisions": [], "error": None}

    # IntradayStopManager는 반드시 키워드 인자로 구성한다. 위치 인자로 넘기면
    # submit_sell_fn↔dataset 순서가 어긋나 on_tick이 self.dataset.get()에서 즉시
    # AttributeError로 죽어 catch-up 손절이 단 한 건도 발주 못 하는 회귀가 난다.
    # on_tick은 ledger pos["definition"]으로 청산 룰을 읽으므로 strat 조회 함수는 불필요.
    # dataset은 ATR 트레일링 청산 평가용 — 정상 cycle과 동일하게 보유 종목만 scoped
    # 로드(실패/누락 시 atr_val=None으로 단순 손절/익절/트레일%만 평가).
    import quant_core as qc
    try:
        ds = qc.load_dataset_for([p["symbol"] for p in positions],
                                  with_indicators=True)
    except Exception as e:
        log.warning("catch-up stop-loss [%s] dataset 로드 실패 — ATR 트레일 제외 평가: %s",
                     market, e)
        ds = {}
    manager = IntradayStopManager(
        broker=broker,
        get_ledger=lambda: trader.ledger,
        submit_sell_fn=trader._submit_sell,
        dataset=ds,
    )
    # 알려진 한계(REV-③#1): 트레일링/ATR-트레일 청산은 peak_price 워터마크에 의존하는데,
    # PC가 꺼져 있던 동안의 장중 고점은 ledger.peak(=진입가 기준)에 반영되지 않는다.
    # on_tick이 peak=max(ledger.peak, 현재가)로 보정하지만 '진입↔재기동 사이의 고점'은
    # 못 잡아 catch-up 트레일이 약하게 트리거될 수 있다. 고정 손절/익절(entry 기준)은
    # 무영향. 장중 고점 조회(분봉)는 드문 PC-off 경로 대비 비용이 커 도입하지 않는다.
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


def _prepare_helpers() -> tuple[object, object] | None:
    """catch-up용 broker·trader 준비. 실패 시 None.

    KIS 자격증명 없으면 make_broker가 RuntimeError → catch-up abort (안전).
    손절·cycle catch-up 모두 ledger pos["definition"](자기완결 전략 정의)로 동작하므로
    여기서 전략 pull은 불필요 — cycle catch-up은 run_cycle 내부에서 전략을 pull한다.
    """
    try:
        from .runner import make_broker
        from .trader import Trader
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

    return broker, trader


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
        # v0.9.13 D-5 — 옛 코드는 silent return → 사용자 모름. plan은 있는데
        # 자격증명 누락 등으로 실행 0건이면 amber 배너로 명시 알림. KIS 자격증명
        # 등록 흐름을 모르는 신규 사용자에게 가장 흔한 silent fail 경로.
        log.warning("catch-up: helpers 준비 실패 — plan은 있으나 실행 불가, "
                    "사용자에게 amber 배너 표시")
        results["_helpers_unavailable"] = {
            "error": "KIS 자격증명 미등록 또는 broker 초기화 실패 — "
                     "[설정] 메뉴에서 KIS appkey·계좌번호 입력 필요",
            "plan_summary": str(plan),
        }
        _save_result(plan, results)
        return {"plan": plan, "results": results}
    broker, trader = helpers

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
                "KRX", broker, trader)
        except Exception as e:
            log.exception("catch-up stop-loss KRX 실행 실패: %s", e)
            results["krx_stop_loss"] = {"error": str(e)}
    if plan.us_stop_loss_check:
        try:
            results["us_stop_loss"] = _catchup_stop_loss(
                "US", broker, trader)
        except Exception as e:
            log.exception("catch-up stop-loss US 실행 실패: %s", e)
            results["us_stop_loss"] = {"error": str(e)}

    # 2.5) 종가창 catch-up (R4-③) — 창내 재실행. 손절 다음·풀사이클 전
    #      (창이 좁아 시간 우선). run_close_cycle 자체가 재시도·하드컷 보유.
    for _cls in plan.krx_close_classes:
        try:
            from .runner import run_close_cycle
            results[f"krx_close_{_cls}"] = {
                "summary": (run_close_cycle("KRX", _cls) or {}).get(
                    "cycle_summary", {})}
        except Exception as e:
            log.exception("catch-up 종가(%s) 실행 실패: %s", _cls, e)
            results[f"krx_close_{_cls}"] = {"error": str(e)}

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
        if k == "_helpers_unavailable":
            # v0.9.13 D-5 — helpers 준비 실패 알림. plan_summary로 무엇이 보류됐는지 표시.
            out["plan_summary"] = v.get("plan_summary", "")
        elif k.endswith("_stop_loss"):
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
            # v0.9.13 D-2 — 누락 영업일 수 (가시성)
            if k == "krx_settle":
                out["days_missed"] = plan.krx_settlement_days_missed
            elif k == "us_settle":
                out["days_missed"] = plan.us_settlement_days_missed
        serializable["results"][k] = out

    try:
        # 체결·정합성 결과는 민감 — state_store 위임 (R5, 원자+owner-only ACL).
        save_json(CATCHUP_RESULT_PATH, serializable)
    except OSError as e:
        log.warning("catch-up 결과 저장 실패: %s", e)
