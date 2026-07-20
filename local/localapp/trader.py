"""모의투자 트레이딩 로직 (Phase 9 best practices 적용).

핵심 변경 사항:
- 시장가 → 지정가 + price tolerance (어제 종가 기준 한도)
- 갭 필터 (전일 종가 vs 현재가 갭 > 임계값이면 신규 진입 폐기)
- ATR 변동성 보정 포지션 사이징 (atr_risk 모드)
- 일일 손실 한도 + kill switch (자본 대비 −3% 도달 시 자동 청산 + 차단)
- 라이브 슬리피지 측정 (의도가 vs 체결가 bps 누적)

청산 우선순위는 백테스트와 동일: 익절 → 손절 → 트레일링 → 보유기간 → 매도신호.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import quant_core as qc
from quant_core import market_calendar as mc
from quant_core.exec_defaults import instrument_spec, merged_execution
from quant_core.futures_expiry import roll_lead_days

from .broker import Broker
from .config import (CLAIMED_FILLS_PATH, EQUITY_PATH, LEDGER_PATH,
                     PENDING_ORDERS_PATH, TRADES_PATH)
from .kis_broker import canonical_odno
from . import account_handle, analytics, coverage, intents, killswitch, order_log, state_store

log = logging.getLogger("localapp.trader")

# Q5(AL-4)/M3: 트레이더 공유 상태(ledger·pending·equity) 변경의 단일 직렬화 락.
# 모듈 레벨로 두는 이유: trader 인스턴스가 cycle/settlement에서 매번 새로
# 만들어지므로 인스턴스 lock으로는 직렬화가 안 된다. 같은 PC 단일 프로세스
# 가정이라 모듈 락이 안전. cycle/settlement뿐 아니라 _apply_fill·_after_submit·
# cancel_all_pending·reconcile_with_kis 등 모든 변경 진입점이 이 락 안에서만
# ledger/pending을 만진다 — WS 체결 thread·60초 monitor·스케줄러 cycle이 같은
# dict를 동시 변경하는 race를 차단한다.
# RLock인 이유: cycle이 락을 쥔 채 _resolve_pending→_apply_fill로 재진입하므로
# 같은 thread 재획득이 필요하다(일반 Lock이면 self-deadlock). 데드락의 다른
# 경로(_apply_fill→ks hook→cycle)는 hook을 임계구역 밖에서 호출해 회피한다.
_CYCLE_LOCK = threading.RLock()


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("파일 파싱 실패, 기본값 사용: %s", path)
    return default


def _save_json(path: Path, obj) -> None:
    """민감 상태 원자적 저장 + owner-only ACL — state_store 단일 경로 위임 (R5).

    원장·equity·pending 등은 잔고·손익을 담은 민감정보다. 원자성(L-02 크래시 시
    부분기록 손상 방지)과 ACL(같은 PC 타 사용자 차단)을 항상 함께 보장한다.
    """
    state_store.save_json(path, obj)


def kst_today() -> date:
    """현재 KST 날짜 — 사용자 PC tz와 무관하게 한국 시장 기준 (L-06 수정).

    원장·intent·체결 dedup의 'today' 키가 PC tz에 따라 달라지면 한국장 거래일이
    어긋난다(여행/해외 거주 사용자). 명시적으로 KST 환산.
    """
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def kst_now() -> datetime:
    """현재 KST 시각 — kst_today와 같은 이유(L-06)로 PC tz 무관. 테스트가 monkeypatch."""
    return datetime.now(ZoneInfo("Asia/Seoul"))


# ── WS-1(δ): 청구된 해외 체결행 레지스트리 ────────────────────────────────────
# 미국 예약주문은 접수번호와 체결행 odno의 번호공간이 달라(실측 2026-06-11: 접수
# 448 vs 체결행 10자리) 종목+사이드+수량으로 매칭한다. 같은 체결행을 두 주문/
# 사이클이 이중 기장하지 않도록, 청구한 행의 odno를 영속 dedup한다.

_CLAIMED_RETENTION_DAYS = 14    # 조회창(제출일 D-1~) + pending GC(7일)보다 넉넉히


def _load_claimed_fills() -> dict:
    return _load_json(CLAIMED_FILLS_PATH, {})


def _register_claimed_fills(odnos: list[str]) -> None:
    if not odnos:
        return
    reg = _load_claimed_fills()
    today = kst_today()
    for o in odnos:
        key = canonical_odno(o)
        if key:
            reg[key] = today.isoformat()
    cutoff = (today - timedelta(days=_CLAIMED_RETENTION_DAYS)).isoformat()
    reg = {k: v for k, v in reg.items() if v >= cutoff}
    _save_json(CLAIMED_FILLS_PATH, reg)


def _policy(strat_def: dict) -> dict:
    """전략 정의에서 ExecutionPolicy를 추출하고 글로벌 default와 병합."""
    return merged_execution(strat_def.get("execution") if strat_def else None)


def _currency_of(symbol: str) -> str:
    """결제 통화 — 미국 종목이면 USD, 그 외 KRW. 사이징·잔고 단위 결정.

    ⚠ 선물엔 이 함수를 사이징 통화로 쓰지 않는다(해외선물을 USD로 보면 buying_power_usd
    주식 API로 라우팅돼 깨짐). 지정가 호가단위는 _round_limit이 instrument_spec으로 분기."""
    from . import market_index
    return "USD" if market_index.is_us(symbol) else "KRW"


def _round_limit(price: float, direction: str, symbol: str) -> float:
    """지정가 호가단위 라운딩 — 선물은 계약 틱(instrument_spec.tick), 주식은 통화별 호가표(C3).

    선물(KOSPI200 0.05·GC 0.10·NQ 0.25·BTC 5 등)은 계약 틱 그리드에 맞춰야 KIS가 수용한다 —
    통화별 정수 라운딩은 BTC(틱5)처럼 그리드를 이탈하거나 0.05·0.25 틱 정밀도를 잃는다.
    round_to_tick은 tick>0이면 통화 무관 틱 배수로 라운딩하므로 선물엔 tick만 넘긴다.
    주식은 tick=0(기본) → 통화별 호가표 그대로 — **주식 동작 byte-identical**."""
    spec = instrument_spec(symbol)
    if spec.asset_class == "futures":
        return qc.round_to_tick(price, direction=direction, tick=spec.tick)
    return qc.round_to_tick(price, direction=direction, currency=_currency_of(symbol))


def _market_group_safe(symbol: str) -> str:
    """시장 그룹('US'|'KRX') — 라우팅 불확실 시 국내 기본(안전)."""
    from . import market_index
    try:
        return market_index.market_group_of(symbol)
    except Exception:
        return "KRX"


def _bar_is_stale(sdf, symbol: str, today: date) -> bool:
    """dataset 마지막 봉이 직전 거래일보다 오래됐나 — 신선도 게이트(fail-stale).

    2026-07-14 사고: 서버가 08:10 KRX 선물을 번들에 재포장하지 않아 07-10 봉(1210.5)이
    '전일 종가'인 척 참조가·사이징·넷팅에 쓰였다(로컬 신선도 검증 부재). 이 함수는 마지막 봉
    날짜를 직전 정규 거래일과 대조한다. 판정 불가(캘린더 범위 밖·인덱스 비날짜)면 False —
    근거 없이 정상 진입을 막지 않는다(over-block 금지). KRX→KR 캘린더 매핑."""
    try:
        last = sdf.index[-1]
        last_date = last.date() if hasattr(last, "date") else last
        grp = _market_group_safe(symbol)
        cal_market = "KR" if grp == "KRX" else grp
        prev = mc.prev_session_day(cal_market, today)
        return prev is not None and last_date < prev
    except Exception:
        return False


def _count_today_pending(pending: dict, side: str, market: str,
                         today_start_ts: float) -> int:
    """이 거래일 발주분(submitted_ts ≥ today_start_ts) 중 side·market 일치 미체결 수.

    '자동매매 시작'의 매수/매도 카운트(n_buy_placed·n_sell_placed)가 *이 사이클* 진입을
    반영하려면, 며칠째 pending에 남은 orphan(LS 체결인지 실패 → 7일 GC 전까지 잔존)을
    제외해야 한다(2026-06-26 라이브 회귀 '후보 2 → 매수 3': 06-22 orphan 18756이 당일
    매수에 합산). submitted_ts 없는 엔트리는 0 → 제외(과대표시 방지 우선)."""
    return sum(
        1 for p in pending.values()
        if p.get("side") == side
        and _market_group_safe(p.get("symbol", "")) == market
        and float(p.get("submitted_ts") or 0) >= today_start_ts)


def _unified_equity_krw(bal: dict) -> float:
    """국내+해외 통합 자산(KRW) — kill switch·drawdown용 계좌 전체 equity.

    주식 국내 평가금액 + 외화 평가총액(KRW) + USD 예수금(KRW 환산) + 선물계좌 추정예탁자산(KRW).

    선물계좌는 별도 KIS 계좌라 그 equity(BrokerRouter가 잔고병합 시 채우는 futures_eval_krw)를
    합산해야 kill-switch·drawdown이 선물 PnL을 인지한다(미합산 시 선물 손익이 안전망에 안 잡힘).
    ⚠단일 풀 모델: 주식+선물을 한 위험풀로 본다 — 선물 단독 급락이 주식 자본에 희석돼 보호가
    약해질 수 있다(주식 비중 큰 사용자). 계좌별 분리 kill-switch는 Phase 3(실거래) 전 재검토.
    선물 미등록 사용자는 futures_eval_krw 키 부재 → 주식 동작 byte-identical.
    """
    dom = float(bal.get("total_eval", 0) or 0)
    foreign = float(bal.get("foreign_eval_krw", 0) or 0)
    usd_cash = float(bal.get("cash_usd", 0) or 0)
    fx = float(bal.get("fx_usdkrw", 0) or 0)
    futures = float(bal.get("futures_eval_krw", 0) or 0)
    return dom + foreign + usd_cash * fx + futures


def _gap_pct(prev_close: float, cur_price: float) -> float:
    """갭 % (양수 = 갭상승, 음수 = 갭하락)."""
    if prev_close <= 0:
        return 0.0
    return (cur_price - prev_close) / prev_close * 100


def _held_trading_days(dataset: dict, symbol: str, entry_iso: str,
                       today: date) -> int:
    """보유 **거래일** 수 — dataset 봉 수 기준 (로드맵 D · 백테스트 파리티).

    백테스트의 held = i − entry_i(봉 수)와 동일 의미. 종전 달력일 산술
    ((today − entry).days)은 주말·휴장을 세어 hold_days가 긴 전략일수록
    실전이 백테스트보다 일찍 만기됐다(예: 금 진입 hold_days=5 → 달력일로는
    수요일, 거래일로는 다음주 금요일). 봉 수 = 실제 거래일이므로 캘린더
    커버리지(과거 ~수십 일)에도 묶이지 않는다 — 실데이터가 곧 진실.

    사이클은 휴장 게이트(L-03)를 통과한 거래일에만 돌므로, 오늘 봉이 아직
    없으면(아침 창 — 번들은 전일까지) 오늘을 진행 중인 봉 1개로 센다
    (백테스트의 "현재 바 i"에 해당). 시세 부재는 예외 — 호출자의 기존
    파싱 실패 경로(보류·표면화)로 합류한다.
    """
    df = dataset.get(symbol)
    idx = getattr(df, "index", None)
    if df is None or idx is None or len(idx) == 0:
        raise ValueError(f"보유 거래일 계산용 시세 없음: {symbol}")
    entry_d = date.fromisoformat(entry_iso)
    if entry_d >= today:
        # 진입 당일 = 진입 바 그 자체 → held 0 (백테스트 i−entry_i=0과 동일).
        # 이 가드가 없으면 아래 +1 규칙이 당일 재실행(08:52 수렴 등)에서 1을
        # 만들어 당일매매(hold_days=0)를 종가 전에 조기 청산시킨다.
        return 0
    n = 0
    last_d = None
    for ts in idx:
        d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
        last_d = d if (last_d is None or d > last_d) else last_d
        if entry_d < d <= today:
            n += 1
    if last_d is not None and last_d < today:
        n += 1        # 오늘(거래일·봉 미도착)을 현재 진행 봉으로 카운트
    return n


def _exit_reason_for(defn: dict, held_days: int,
                     dataset: dict, symbol: str,
                     *, is_close: bool = False) -> tuple[str | None, object | None]:
    """청산 사유를 IR 엔진(전략 연구소)으로 평가. (reason, None) 튜플 반환.

    ir_engine.live.cycle_exit_reason이 보유기간+매도조건 Node를 백테스트와 동일한
    IR exit 스펙으로 평가한다. 두 번째 원소는 항상 None — 호출자는 None이면
    리밸런스·sell_rules(operand) 경로를 건너뛰고 전량(100%) 청산한다.
    파싱 실패는 예외 전파(호출자가 잡아 skip).

    is_close: 종가 사이클 여부. 당일매매(hold_days==0)만 영향 — 종가 사이클(True)에서만
    "당일청산". 일반 cycle 루프는 종가 사이클이 아니므로 기본 False(종전 동작 byte-identical).
    """
    from quant_core.ir_engine import StrategyIR
    from quant_core.ir_engine import live as ir_live
    ir = StrategyIR.model_validate(defn)
    return ir_live.cycle_exit_reason(
        ir, held_days=held_days, dataset=dataset, symbol=symbol,
        is_close=is_close), None


def held_qty_from_snapshot(snap: dict, symbol: str, side: str = "long") -> int:
    """account_snapshot positions에서 symbol·side의 실 보유 수량 합 — 단일 출처(L-04).

    청산 발주 직전 over-close 클램프의 공유 헬퍼. 장중 손절(intraday_stop)과 EOD
    cycle 청산이 같은 기준으로 'KIS가 실제로 들고 있는 수량'을 읽어, ledger가
    외부 수동매매로 drift해도 보유 초과 청산을 발주하지 않는다.

    M5b: side 인지(롱/숏 분리). 주식·롱은 side='long' 기본(positions에 side 없으면 norm_side가
    long→포함) — 종전 동작 보존. 숏 환매 클램프는 side='short'로 호출.
    """
    from .analytics import norm_side
    return sum(int(p.get("qty") or 0)
               for p in (snap or {}).get("positions", [])
               if p.get("symbol") == symbol and norm_side(p.get("side")) == side)


def clamp_sell_qty(broker_qty: int | None, ledger_qty: int) -> int | None:
    """L-04 매도 수량 클램프(단일 출처) — 모든 매도 경로가 같은 규칙으로 KIS 실
    보유에 맞춰 수량을 제한한다. EOD cycle·장중 tick 손절이 각자 다르게 구현해
    한쪽(EOD)이 클램프를 통째로 빠뜨렸던 결함 class(8cd5e8b 사후 보완)를 막는다.

    반환: None=잔고 미상(skip·재시도), 0=외부 매도로 보유 0(skip), 그 외=min(보유, 원장).
    호출부는 None/0의 부수효과(로그·sold_today·decision)만 각자 처리한다.
    """
    if broker_qty is None:
        return None
    if broker_qty <= 0:
        return 0
    return min(int(broker_qty), int(ledger_qty))


def credit_for(capacity_credit: dict | None, symbol: str, pos_side: str) -> float:
    """§18 사이징 크레딧 조회 — 두 풀의 합(방향무관 + 같은-편 강등분).

    - `(symbol, None)` = **방향무관 풀(원칙)**. 계획 청산·원장 밖(수동) 보유 모두
      "전부 되돌린 빈-상태 잔고" 기준으로 진입 방향과 무관하게 크레딧한다 — 갈아타기
      (롱청산→숏진입)도 청산분을 반영한 여력으로 진입(2026-07-18 유저 모델 정정).
    - `(symbol, pos_side)` = **같은-편 강등 풀**. 선물인데 브로커 orderable의 '신규
      전용' 여부가 미확정이면 Trader._credit_key가 여기로 강등한다 — 합산(신규+청산)
      의미일 경우 반대편 orderable에 청산분이 이미 포함돼 방향무관 크레딧이
      이중계상(의도 초과 레버리지)되는 것을 막는 안전 게이트. LS·KIS 국내선물은 둘 다
      신규 전용 실측 확정(각 07-16·07-20)이라 현재 강등 대상은 미확정 어댑터뿐이다.

    크레딧이 이중계상이 아닌 근거(2026-07-16 LS 문서·실측 확정): CFOAQ10100은
    신규(`NewOrdAbleQty`)와 청산(`LqdtOrdAbleQty`)을 **분리 반환**하고(예시 38=36+2)
    우리는 신규만 읽는다 → 보유를 되돌려 더해야 빈-상태 여력이 된다. 이중계상이 아니다.
    (실측: 수동 숏4 보유 시 신규매수 5·청산매수 4 → 빈-상태 9. 크레딧 미적용이라
    5로 사이징돼 롱2 진입, 정답은 9×50%=롱4였다 — §18.2 갭.)
    """
    if not capacity_credit:
        return 0.0
    return (float(capacity_credit.get((symbol, pos_side), 0))
            + float(capacity_credit.get((symbol, None), 0)))


def consume_credit(capacity_credit: dict, symbol: str, pos_side: str,
                   consumed: float) -> None:
    """다중 진입의 회수 여력 순차 소진(E2·N9) — 같은-편 풀 먼저, 부족분은 방향무관 풀에서.

    두 풀의 **합**이 정확히 consumed만큼 줄어야 다음 진입이 같은 여력을 재사용하지 않는다.
    초과분은 방향무관 풀을 음수로 만들어 다음 진입의 orderable을 그만큼 깎는다(기존 동작 보존).
    """
    _ck = (symbol, pos_side)
    same = float(capacity_credit.get(_ck, 0))
    take = min(consumed, same) if same > 0 else 0.0
    if take:
        capacity_credit[_ck] = same - take
    rest = consumed - take
    if rest:
        _fk = (symbol, None)
        capacity_credit[_fk] = float(capacity_credit.get(_fk, 0)) - rest


class Trader:
    """Broker에 의존하는 모의투자 실행기. 보유 원장을 로컬에 유지한다.

    원장 항목은 전략 정의를 함께 보관하므로, 플랫폼에서 전략이 삭제돼도
    (고아 포지션) 저장된 규칙으로 안전하게 청산할 수 있다.
    """

    def __init__(self, broker: Broker):
        self.broker = broker
        self.ledger: dict[str, dict] = _load_json(LEDGER_PATH, {})
        self.equity: list[dict] = _load_json(EQUITY_PATH, [])
        self.pending: dict[str, dict] = _load_json(PENDING_ORDERS_PATH, {})
        # 사이클 사이징 근거(관측 전용·2026-07-20) — {symbol|side: {...}}. 사이클 시작에
        # 비우고 요약에 실어 보낸다. 목표 수량의 근거(브로커 원값·크레딧·사용률)가 서버에
        # 없어 mwmw 07-20 조사에서 원장 산술로 역산해야 했던 것의 근본 해소.
        self._sizing_trace: dict[str, dict] = {}
        # A1(외부·수동 미체결 인수) 결과 {계약키: 부호수량} — 종전엔 INFO 로그뿐이라
        # 원격 진단 불가였다. 수렴 패스마다 갱신하고 사이클 요약에 실어 보낸다.
        self._a1_trace: dict = {}
        # 이번 수렴 패스가 **목표를 확정한** 심볼(목표 0 포함) — 동시호가 가드가
        # 자기 우주를 정하는 데 쓴다. 원장에 행이 남지 않는 목표(청산 완료·넷팅
        # book)를 "목표 없음"과 구분하기 위한 것이다: 가드 우주가 `own 의도 ∪ 원장`
        # 뿐이면 book으로 원장 행이 지워진 심볼이 통째로 안 보인다(A1 인수 수동
        # 주문이 취소되면 그 심볼에 물리 노출이 남는데도 감지 불가).
        self._target_syms: list = []
        # 미국 매수여력 모드 (cycle에서 risk_limits로 설정). 기본 통합증거금.
        self._us_bp_mode: str = "integrated"
        # Q5: 체결 후(_apply_fill) 즉시 kill switch 평가용 한도. cycle 진입 시
        # risk_limits에서 채워진다. 호출자가 설정 안 했으면 평가 skip(보수적 무동작).
        self._daily_loss_limit_pct: float | None = None
        # Q5: kill switch 발동 시 추가 동작을 외부에 알리는 hook (intraday_loop이
        # 보유 종목 강제 청산 cycle을 트리거하도록). None이면 발동만 기록.
        self._ks_trigger_hook = None
        # Q5(데드락 방지): "현재 스레드가 cycle 안인가"를 나타내는 thread-local 플래그.
        # _apply_fill의 ks 평가/hook은 cycle 외부 스레드에서만 동작 — cycle 내부의
        # _apply_fill(미체결 정리·_wait_pending 폴링)에서 hook이 cycle을 재호출하면
        # _CYCLE_LOCK 재진입+무한재귀. 인스턴스 bool이면 cycle이 도는 동안 다른
        # 스레드(WS _on_exec_event)의 *정당한* 체결 ks 평가까지 오억제되므로(REV-D)
        # thread-local로 둔다. 아래 _in_cycle property가 이 tls에 위임한다.
        self._cycle_tls = threading.local()
        # 미국 정상 cycle에서 True — _submit_buy/_submit_sell이 예약주문(개장 전
        # 접수 → 개시 자동전송)으로 라우팅. _cycle_body가 매 cycle 갱신.
        self._reserved_us = False

    @property
    def _in_cycle(self) -> bool:
        """현재 스레드가 cycle 본문 안인지(thread-local). cycle 스레드만 True."""
        return getattr(getattr(self, "_cycle_tls", None), "active", False)

    @_in_cycle.setter
    def _in_cycle(self, value: bool) -> None:
        # __new__로 __init__ 우회한 테스트 스텁도 안전하도록 tls를 lazy 생성.
        tls = getattr(self, "_cycle_tls", None)
        if tls is None:
            tls = threading.local()
            object.__setattr__(self, "_cycle_tls", tls)
        tls.active = bool(value)

    # ── 영속화 ────────────────────────────────────────────────────────────────

    def _save(self):
        # L-02: 4파일 모두 원자적 저장 (_save_json은 tmp+os.replace 패턴).
        # 4파일 cross-consistency는 여전히 미보장(파일별 원자성만)이지만, 부분
        # truncate에 의한 원장 소실은 차단된다.
        _save_json(LEDGER_PATH, self.ledger)
        _save_json(EQUITY_PATH, self.equity)
        _save_json(PENDING_ORDERS_PATH, self.pending)

    def reload_state(self) -> None:
        """디스크 상태(ledger·equity·pending)를 메모리로 재적재 — 디스크가 SSOT(M9).

        같은 프로세스에 장수명 Trader(intraday loop)와 ephemeral Trader(cycle·
        settlement·gui push)가 공존한다. 각자 생성 시점의 메모리 사본을 들고 같은
        파일을 통째로 쓰기 때문에, stale 사본의 저장이 다른 인스턴스의 변경(매도·
        체결)을 되돌려 — 매도된 포지션이 부활하고 reconcile이 그걸 "외부 매도 추정"
        으로 오판(P&L 소실)하는 부류가 라이브에서 실증됐다. 장수명 인스턴스는 변경
        세션 진입부(intraday_loop의 WS 체결·장중 매도·ks 트리거)에서 이걸 호출해
        디스크 최신 상태 위에서만 변경한다. 모든 변경 지점은 락 안에서 즉시 _save
        하므로(아래 _apply_fill·_after_submit·_resolve_pending) reload가 미저장
        변경을 잃지 않는다.
        """
        with _CYCLE_LOCK:
            self.ledger = _load_json(LEDGER_PATH, {})
            self.equity = _load_json(EQUITY_PATH, [])
            self.pending = _load_json(PENDING_ORDERS_PATH, {})

    def _log_trade(self, event: dict):
        # 체결·거래 기록은 민감 — state_store 위임 (R5, 최초 생성 시 owner-only ACL).
        state_store.append_jsonl(event, TRADES_PATH)

    # ── Phase 40 — KIS 잔고 ↔ ledger 정합성 자동 정정 ──────────────────────
    def reconcile_with_kis(self, today_iso: str | None = None) -> dict:
        """KIS 실 잔고와 ledger를 비교 — **관측 전용**(목표수렴 §14·§17.6).

        원장=전략 의도 정본. orphan(원장>브로커)·external_extras(브로커>원장) 모두
        원장을 바꾸지 않고 표면화만 한다 — 물리 정합은 다음 사이클 _reconcile_pass의
        drift 교정이 수행(broker←ledger). 반환: reconcile dict(applied는 항상 []).

        호출 시점: 15:50 post_close_settlement — 모든 KRX 종가창(주식 15:30·선물
        15:45) 이후.
        """
        try:
            snap = self.broker.account_snapshot()
        except Exception as e:
            log.error("reconcile: KIS 잔고 조회 실패 — skip: %s", e)
            return {"error": f"KIS 잔고 조회 실패: {e}"}

        # I2(정합성 fail-safe) — 선물 신원계층이 비정상이면 선물 orphan의 파괴적 정정을
        # 차단한다. "원장에 있는데 브로커에 없음 = 외부 매도"라는 추론은 매칭이 신뢰
        # 가능할 때만 성립하는데, 다음 두 경우는 매칭 자체가 깨져 있다:
        #  ① balance.fetch_failed — 구성된 선물 leg 조회 실패로 그 포지션들이 스냅샷에
        #     없음 → 원장 선물 전부가 orphan처럼 보여 전량 삭제 사고로 직결.
        #  ② symbol_unmapped — 잔고 계약코드 정규화 실패(2026-07 분기 인시던트의 방아쇠:
        #     LS t0441 KRX형 코드 미인식) → (symbol,side) 키가 브로커 쪽에서 원시 코드로
        #     남아 원장(상품명)과 절대 일치하지 않음.
        # 이때는 무동작+표면화가 유일하게 안전하다. 주식은 symbol=종목코드로 양쪽 동일
        # (정규화 무관)이라 주식 orphan 자동 차감(승인된 수동매도 대응)은 종전대로 유지.
        fetch_failed = list((snap.get("balance") or {}).get("fetch_failed") or [])
        unmapped = sorted({str(p.get("symbol", "")) for p in snap.get("positions", [])
                           if p.get("symbol_unmapped")})
        futures_identity_broken = bool(fetch_failed or unmapped)

        # 목표수렴(kr-target-reconciliation.md §14) — reconcile은 **관측 전용**.
        # 방향 역전: 구 모델은 orphan(원장>브로커)을 "외부 매도 추정"으로 원장에서
        # 자동 차감했다(ledger←broker). 목표수렴에서 원장=전략 의도가 정본이고
        # 브로커가 원장을 따라온다(broker←ledger) — 수동 매도는 다음 사이클
        # _reconcile_pass의 drift 교정이 되돌린다. 여기서 원장을 차감하면 의도가
        # 소실돼 되돌림이 무력화되므로 어떤 원장 변경도 하지 않는다(표면화만).
        with _CYCLE_LOCK:
            result = analytics.reconcile_ledger(snap.get("positions", []), self.ledger)
        orphans = result.get("ledger_orphans", [])
        if orphans:
            log.warning(
                "reconcile: 원장>브로커 drift %d건 감지(수동 매도/외부 정리 추정) — "
                "원장 불변·다음 사이클 목표수렴이 교정: %s",
                len(orphans),
                [(o.get("symbol"), o.get("sid")) for o in orphans][:10])
        else:
            log.info("reconcile: drift 없음 (in_sync %d종목)", len(result.get("in_sync", [])))

        result["applied"] = []          # 관측 전용 — 원장 변경 없음(필드 형태 유지)
        result["external_extras_count"] = len(result.get("external_extras", []))
        if futures_identity_broken:
            # 표면화 — cycle summary→서버→웹으로 전달돼 "정합성 점검 불가" 상태를 알린다.
            # (스냅샷 신원이 깨진 동안엔 _reconcile_pass도 drift 교정을 보류한다.)
            result["reconcile_blocked"] = {
                "fetch_failed": fetch_failed, "unmapped_codes": unmapped,
                "blocked_futures_orphans": len(
                    [o for o in orphans if qc.is_futures(o.get("symbol", ""))]),
            }
        result["has_drift"] = (bool(orphans) or bool(result.get("external_extras"))
                               or futures_identity_broken)
        return result

    def daytrade_unclosed(self, market: str) -> list[dict]:
        """정산 시점에 남아 있는 당일매매(hold_days==0) 포지션 — 불변식 I5 감시용.

        당일매매 포지션은 그날 종가창이 청산해야 하므로, **장 마감 후 정산**(post_close_
        settlement) 시점에 하나라도 남아 있으면 원인 무관(종가창 미실행·발주 거부·부분
        체결)하게 "당일 청산 실패 = 의도치 않은 오버나이트 노출"이다. 2026-07-02 종가창
        cron 미발화가 이 상태를 무감지로 지나가 익일에야 드러났다 — 이 감시가 당일 15:50에
        표면화한다. 잡 실행 여부가 아니라 **상태**를 검사해 부류 전체를 잡는다.
        ⚠ 개장 직후 reconcile(post_open_reconcile)에선 호출 금지 — 당일매매 보유가 정상."""
        out = []
        for sid, pos in self.ledger.items():
            if _market_group_safe(pos.get("symbol", "")) != market:
                continue
            hd = (((pos.get("definition") or {}).get("position") or {})
                  .get("exit") or {}).get("hold_days")
            # I5+(재설계 D3): exit.fill=close 포지션도 만기일엔 그날 종가창이 청산해야
            # 한다 — 정산 시점 잔존이면 당일매매와 같은 "의도치 않은 오버나이트" 부류.
            _ef = (((pos.get("definition") or {}).get("position") or {})
                   .get("exit") or {}).get("fill")
            due_close_exit = False
            if _ef == "close" and hd is not None and hd >= 1:
                try:
                    # 로드맵 D — 만기 판정을 거래일 수로(달력일은 주말·휴장을 세어
                    # 조기 경보). 정산 문맥은 dataset 무의존(비상 격리 원칙)이라
                    # 봉 수 대신 세션 캘린더 카운트를 쓴다. 커버 범위 밖(장기
                    # 보유·None)이면 판정 보류 — 추정으로 거짓 경보를 만들지
                    # 않는다(관측 전용 경로·I5 감시).
                    held = mc.sessions_between(
                        "KR" if market == "KRX" else "US",
                        date.fromisoformat(pos.get("entry_date", "")), kst_today())
                    due_close_exit = held is not None and held >= hd
                except Exception:
                    due_close_exit = False
            if (hd == 0 or due_close_exit) and int(pos.get("qty") or 0) > 0:
                out.append({"sid": sid, "symbol": pos.get("symbol", ""),
                            "qty": int(pos.get("qty") or 0),
                            "strategy_name": pos.get("strategy_name", "")})
        return out

    def _safe_price(self, symbol: str) -> float | None:
        try:
            px = self.broker.price(symbol)
            return px if px > 0 else None
        except Exception as e:
            log.error("가격 조회 실패 [%s]: %s", symbol, e)
            return None

    def _resolve_contract_key(self, symbol: str) -> str:
        """넷팅용 물리 계약 식별자 — 선물=활성 만기 계약코드, 주식=종목코드(E6).

        선물은 상품명("코스피200선물")이 여러 만기물로 매핑되므로, 넷팅이 롤 경계에서
        근월↔원월을 오상계하지 않도록 실제 계약코드로 그룹핑한다. 청산 의도는 원장의
        contract_code(진입 시 _record_contract_meta가 기록)를, 진입 의도는 여기서 브로커
        해석을 쓰며 같은 발주창에선 같은 활성 월물로 수렴한다. 해석 불가(SimBroker·구배선)면
        상품명 fallback — 청산 의도도 같은 fallback이라 일관."""
        if not qc.is_futures(symbol):
            return symbol
        fn = getattr(self.broker, "contract_expiry", None)
        if fn is None:
            return symbol
        try:
            code, _ = fn(symbol)
        except Exception:
            return symbol
        return code or symbol

    # ── 미체결 추적·해제 ──────────────────────────────────────────────────────

    def _resolve_pending(self, decisions: list[dict]) -> None:
        """이전 사이클에서 남은 미체결 주문의 현재 상태를 갱신.

        Q7(DAY 단일): 로컬 timeout cancel 제거. KIS가 정규장 마감(15:30)에 미체결
        분을 자동 cancel하므로 우리는 상태 조회로 cancelled를 인지하고 ledger·
        pending을 정리하기만 한다. 일중에 limit 도달 시 자연 체결 허용.
        """
        # M3/INV-CONC-1: pending 순회·order_status·_apply_fill·del을 단일 락으로
        # 직렬화한다. WS 체결 스레드(_on_exec_event)가 같은 pending을 동시 변경하는
        # race(이중 del→KeyError, 부분반영 재가산→over-position)를 차단. RLock이라
        # cycle/settlement가 이미 락을 쥔 채 호출해도 재진입 안전.
        with _CYCLE_LOCK:
            self._resolve_pending_locked(decisions)

    @staticmethod
    def _resolve_intent(p: dict, outcome: str) -> None:
        """이 pending의 intent를 저널에서 종결 — 멱등 게이트 해제(intents.mark_resolved).

        pending에서 주문이 사라지는 **모든** 지점에서 호출한다. 종전엔 성공 종결
        경로가 없어 체결된 주문이 종일 게이트를 점유했다(N1 — japan1 438계약
        오버나이트). intent_id는 _after_submit이 pending 레코드에 실어 둔다.

        구버전 pending_orders.json(필드 부재)은 조용히 skip — 종전 거동(보수적
        차단)으로 남을 뿐이라 마이그레이션이 필요 없다.
        """
        iid = p.get("intent_id")
        idate = p.get("intent_date")
        if iid and idate:
            intents.mark_resolved(str(idate), str(iid), outcome)

    def _resolve_pending_locked(self, decisions: list[dict]) -> None:
        if not self.pending:
            return
        from . import market_index
        # δ: 예약주문 청구 dedup — 영속 레지스트리 + 다른 pending의 접수번호(그 행은
        # 그 주문 것) + 이번 패스에서 새로 청구한 행. 동형 주문 2건이 체결행 2개를
        # 1:1로 나눠 갖고, 같은 행의 이중 기장을 사이클을 가로질러 차단한다.
        claimed = set(_load_claimed_fills())
        own_odnos = {canonical_odno(k) for k in self.pending}
        newly_claimed: list[str] = []
        changed = False
        for order_no, p in list(self.pending.items()):
            hint = None
            if market_index.is_us(p.get("symbol", "")):
                exclude = (claimed | own_odnos | set(newly_claimed)) \
                    - {canonical_odno(order_no)}
                hint = {"side": p.get("side"), "qty": int(p.get("qty") or 0),
                        "reserved": bool(p.get("is_resv")),
                        "submitted_ts": p.get("submitted_ts"),
                        "exclude_odnos": sorted(exclude)}
            try:
                if hint is None:    # 비해외 — 레거시 2-인자 호출(구 더블 호환)
                    st = self.broker.order_status(order_no, p.get("symbol"))
                else:
                    st = self.broker.order_status(order_no, p.get("symbol"),
                                                  hint=hint)
            except Exception as e:
                log.warning("주문상태 조회 실패 [%s]: %s", order_no, e)
                continue
            status = st.get("status", "unknown")
            filled = int(st.get("filled_qty", 0) or 0)
            fill_px = float(st.get("fill_price", 0) or 0)
            exec_odno = canonical_odno(st.get("exec_odno") or "")

            if status == "filled" and filled > 0:
                # filled/partial 모두 KIS 누적(tot_ccld_qty) 기준 — 이미 WS/이전 폴링이
                # 반영한 filled_so_far를 차감해 잔여 delta만 반영한다. 차감 안 하면
                # 부분 선반영분이 재가산돼 over-position(INV-FILL-1 위반).
                already = int(p.get("filled_so_far", 0) or 0)
                delta = filled - already
                if delta > 0:
                    self._apply_fill(order_no, p, delta, fill_px, decisions)
                del self.pending[order_no]
                self._resolve_intent(p, "filled")
                changed = True
                if exec_odno:
                    newly_claimed.append(exec_odno)
            elif status == "partial":
                # 부분체결: 채운 만큼만 반영하고 잔여는 계속 추적
                already = int(p.get("filled_so_far", 0))
                delta = filled - already
                if delta > 0:
                    self._apply_fill(order_no, p, delta, fill_px, decisions,
                                      partial=True)
                    p["filled_so_far"] = filled
                    changed = True
                    if exec_odno:
                        newly_claimed.append(exec_odno)
            elif status in ("cancelled", "rejected"):
                # rejected — KIS 국내선물 order_status가 rjct_qty>0을 이 어휘로 준다
                # (kis_futures_broker). 종전엔 어느 분기에도 안 걸려 아래 unknown으로
                # 떨어졌고, 당일 조회창엔 흔적이 남아 익일 회수도 못 해 7일 GC까지
                # pending·멱등 게이트가 함께 잠겼다. 취소와 같은 종결이다(주문 소멸).
                _why = ("주문 거부(rjct) — 브로커가 주문을 생성하지 않음"
                        if status == "rejected"
                        else "미체결 cancelled (장마감 자동 취소 또는 외부 취소)")
                order_log.log_order("cancelled", p["symbol"], p["side"], p["qty"],
                                    order_no=order_no,
                                    intended_price=p.get("intended_price"),
                                    limit_price=p.get("limit_price"),
                                    strategy_name=p.get("strategy_name", ""),
                                    kind=p.get("kind", ""))
                decisions.append(order_log.decision(
                    "unfilled", p.get("strategy_id", ""),
                    p.get("strategy_name", ""), p["symbol"], _why))
                del self.pending[order_no]
                self._resolve_intent(p, status)
                changed = True
            else:
                # 여전히 미확인 — 다음 폴링/사이클에서 재확인. 로컬 timeout 없음.
                sub_ts = p.get("submitted_ts")
                if status != "unknown" or not sub_ts:
                    continue
                # R2-② DAY 만료 익일 회수 — KIS DAY 주문은 장마감에 소멸하므로
                # "제출일이 지났는데 당일 조회창에 무흔적"이면 종결 대상이다.
                # 단 제출 직후 크래시로 당일 반영을 놓친 '지각 체결' 가능성이
                # 있어, 반드시 **제출일자 체결내역**으로 확인한 뒤에만 종결한다
                # (fill 있으면 기장 — 미기장→drift 되팔기 실손 부류 차단).
                # 확인 불가(미지원 브로커·조회 실패·US/해외선물)는 아래 7일 GC가
                # 최후 방어로 유지된다.
                sub_day = datetime.fromtimestamp(
                    float(sub_ts), ZoneInfo("Asia/Seoul")).date()
                if (sub_day < kst_today()
                        and self._reclaim_expired_pending(order_no, p, sub_day,
                                                          decisions)):
                    changed = True
                    continue
                # δ GC 백스톱: 7일 넘게 unknown이면 추적 만료 — 조회창을 벗어난
                # 고아가 pending에 영구 잔존하던 결함(실측 448·0000040620) 차단.
                # 실 보유 정합은 settlement reconcile(보유 diff)이 담당.
                if time.time() - float(sub_ts) > 7 * 86400:
                    log.warning("pending GC [%s] %s — 상태 미확인 7일 경과(추적 만료)",
                                order_no, p.get("symbol"))
                    decisions.append(order_log.decision(
                        "unfilled", p.get("strategy_id", ""),
                        p.get("strategy_name", ""), p.get("symbol", ""),
                        "상태 미확인 7일 경과 — 추적 만료(GC)"))
                    del self.pending[order_no]
                    self._resolve_intent(p, "gc")
                    changed = True
        if newly_claimed:
            _register_claimed_fills(newly_claimed)
        if changed:
            # M9: 변경 즉시 영속 — 다른 인스턴스(reload_state)가 항상 최신을 본다.
            self._save()

    def _reclaim_expired_pending(self, order_no: str, p: dict, sub_day,
                                  decisions: list) -> bool:
        """제출일이 지난 unknown pending의 확정 종결 시도 (R2-② DAY 만료 익일 회수).

        제출일자 체결내역(fills_on)으로 재확인:
          · 체결 흔적 없음/취소 → DAY 만료 확정 — timeout 기록 후 추적 종결
            (종전엔 7일 GC까지 좀비 잔존 — mwmw 510 실측 부류)
          · 체결 흔적 있음 → **지각 기장** 후 종결 — 제출 직후 크래시로 당일
            반영을 놓친 체결이 미기장 → 다음 사이클 drift 되팔기(실손)로
            이어지는 부류를 막는다(리뷰 부작용 가드: fill 부재 확인 후에만 dead).

        범위: KR 주식(KIS 국내 일별체결) + KR 선물(LS CFOAQ00600 주문체결내역
        기간조회 — 어휘 실측 2026-07-20). US·해외선물(CME)·미지원 브로커(KIS 선물)·
        조회 실패는 False — 호출자의 7일 GC가 최후 방어로 유지된다(확정 불가 시
        추적 유지가 안전 측). 라우터가 None(미지원)을 주면 빈 리스트(= 무체결 확정)와
        구분해 종결하지 않는다.
        """
        symbol = p.get("symbol", "")
        from . import market_index
        if market_index.is_us(symbol):
            return False                      # US는 별도 조회 어휘(해외 체결내역) — 미배선
        fills_on = getattr(self.broker, "fills_on", None)
        if fills_on is None:
            return False
        try:
            rows = fills_on(sub_day.strftime("%Y%m%d"), symbol)
        except Exception as e:
            log.warning("익일 회수 조회 실패 [%s] — 7일 GC 백스톱 유지: %s",
                        order_no, e)
            return False
        if rows is None:
            return False                      # 라우터 '미지원' 명시 — 빈 리스트(무체결 확정)와 구분
        row = next((r for r in rows
                    if canonical_odno(r.get("odno", "")) == canonical_odno(order_no)),
                   None)
        filled = int(row.get("filled_qty", 0) or 0) if row else 0
        if row and filled > 0:
            already = int(p.get("filled_so_far", 0) or 0)
            delta = filled - already
            if delta > 0:
                self._apply_fill(order_no, p, delta,
                                 float(row.get("fill_price", 0) or 0), decisions)
            log.warning("지각 체결 회수 [%s] %s %d주 — 제출일(%s) 체결이 미기장 "
                        "상태였음(크래시 추정). 기장 후 종결", order_no, symbol, delta,
                        sub_day.isoformat())
            del self.pending[order_no]
            self._resolve_intent(p, "reclaimed_filled")
            return True
        # 무흔적(접수 실패 추정) 또는 미체결/취소 — DAY 만료 확정.
        order_log.log_order("timeout", symbol, p.get("side", ""), p.get("qty", 0),
                            order_no=order_no,
                            intended_price=p.get("intended_price"),
                            limit_price=p.get("limit_price"),
                            strategy_name=p.get("strategy_name", ""),
                            reason="DAY 만료 확정(제출일 체결내역 무체결) — 익일 회수",
                            kind=p.get("kind", ""))
        decisions.append(order_log.decision(
            "unfilled", p.get("strategy_id", ""), p.get("strategy_name", ""),
            symbol, f"DAY 만료 확정 — 제출일({sub_day.isoformat()}) 체결내역 "
                    "무체결, 익일 회수(R2)"))
        del self.pending[order_no]
        self._resolve_intent(p, "reclaimed_expired")
        return True

    def _record_contract_meta(self, sid: str, symbol: str) -> None:
        """신규 진입 ledger에 라이브 계약코드·만기일ISO 부착 (M6 만기 자동청산).

        만기일은 진입 시점에 확정해 보관한다(현 front-month는 보유 중 롤로 바뀌므로 진입 때
        고정). 비선물·브로커 미지원(SimBroker·구 배선)·해석 실패 시 키를 붙이지 않는다 —
        **주식은 키가 안 붙어 ledger byte-identical**, 선물은 만기 미해석 시 백스톱만 비활성."""
        if not qc.is_futures(symbol):
            return                            # 주식: 만기 개념 없음 → 키 무부착(무변경)
        lg = self.ledger.get(sid)
        if lg is None:
            return
        fn = getattr(self.broker, "contract_expiry", None)
        if fn is None:                        # SimBroker·구 배선 등 — 만기 해석 미지원
            return
        try:
            code, exp = fn(symbol)
        except Exception as e:                # noqa: BLE001 — 해석 실패는 미기록(진입 비차단)
            log.warning("계약/만기 해석 실패 [%s]: %s", symbol, e)
            return
        if code:
            lg["contract_code"] = code
        if exp:
            lg["expiry_date"] = exp.isoformat()
        else:
            log.warning("선물 진입 만기일 미해석 [%s] — 만기 백스톱 비활성", symbol)

    def _expiry_close_reason(self, pos: dict, today: date) -> str | None:
        """만기 백스톱(M6 tier-2) — 선물이 만기 임박이면 강제청산 사유, 아니면 None.

        유저 청산규칙(_exit_reason_for)이 None일 때만 평가된다(tier-1 우선: 유저 롤오버/청산
        규칙이 먼저, 미발동 시 이 백스톱이 만기 前 강제청산해 물리인도·현금정산으로 포지션이
        사라지는 사고를 막는다). 만기일은 진입 시 ledger에 기록(_record_contract_meta).
        lead = 상품 default_roll(days_before:N), 없으면 5일(futures_expiry.roll_lead_days).
        만기 미기록(M6 이전 진입 등)이면 만기를 알 수 없어 백스톱 불가 → 경고 후 None."""
        if not qc.is_futures(pos.get("symbol", "")):
            return None
        exp_iso = pos.get("expiry_date")
        if not exp_iso:
            log.warning("선물 보유 만기일 미기록 [%s] — 만기 백스톱 평가 불가(수동 점검 필요)",
                        pos.get("symbol"))
            return None
        try:
            exp = date.fromisoformat(exp_iso)
        except (TypeError, ValueError):
            return None
        lead = roll_lead_days(instrument_spec(pos["symbol"]).default_roll)
        if today >= exp - timedelta(days=lead):
            return f"만기 자동청산(만기 {exp_iso})"
        return None

    def _apply_fill(self, order_no: str, p: dict, filled_qty: int,
                    fill_price: float, decisions: list[dict],
                    partial: bool = False, netted: bool = False) -> None:
        """체결을 원장·이벤트 로그에 반영.

        netted=True — 넷팅 합성 체결(브로커 미접촉·수수료 0). ledger·realized·통화·side
        분기를 실체결과 동일하게 재사용하되, 주문/거래/결정 레코드에 netted 표식을 남긴다
        (슬리피지는 호출부가 intended_price=None으로 넘겨 log_order가 자동 skip·N2·N3).
        기본 False = 현행 byte-identical.

        p["drift"]=True — 목표수렴 drift 교정 체결(수동매매 되돌림·비전략 보유 청산).
        **원장 불변**: 원장=전략 의도는 leg booking(_apply_netted_leg/leg 실체결)이 이미
        반영했고, 이 주문은 브로커 실보유를 목표로 옮기는 물리 이동만 담당한다
        (kr-target-reconciliation.md §2·§9③). 기록만 남기고 반환.
        """
        sid = str(p.get("strategy_id", ""))
        symbol = p["symbol"]
        side = p["side"]
        intended = p.get("intended_price")
        today = kst_today().isoformat()
        nx = {"netted": True} if netted else None

        order_log.log_order("partial" if partial else "filled", symbol, side,
                             filled_qty, order_no=order_no,
                             intended_price=intended,
                             limit_price=p.get("limit_price"),
                             fill_price=fill_price,
                             strategy_name=p.get("strategy_name", ""),
                             reason=p.get("reason", ""), kind=p.get("kind", ""),
                             extra=nx)

        if bool(p.get("drift")):
            # 목표수렴 drift 교정 — 원장 불변(위 docstring). 체결 사실만 표면화.
            decisions.append(order_log.decision(
                "drift_corrected", sid, p.get("strategy_name", ""), symbol,
                f"목표수렴 교정 {side} {filled_qty} @ {fill_price:,.2f} — "
                "수동매매 되돌림/비전략 보유 정리(원장 불변)"))
            log.info("[목표수렴] drift 교정 체결 %s %s %d @ %.2f (원장 불변)",
                     symbol, side, filled_qty, fill_price)
            return

        # M3: self.ledger 읽기-수정-쓰기를 단일 락으로 직렬화 — WS 체결 thread·
        # monitor·cycle이 같은 원장을 동시 변경하는 race(lost update) 차단.
        with _CYCLE_LOCK:
            # M4 — 선물 인지 회계: ledger에 side(long/short) 추적, 청산 시 정산손익 기록.
            # 주식(equity)은 항상 long·multiplier=1이라 기존 경로와 동일(side="long" 키만 추가).
            # 선물 정산손익 = (청산−진입)×계약수×승수×부호(롱+1/숏−1).
            spec = instrument_spec(symbol)
            is_fut = spec.asset_class == "futures"
            mult = spec.multiplier
            realized: float | None = None       # 선물 청산 실현손익(정산)

            def _invest_of(qty: int, px: float) -> dict:
                """체결 per-order 투입 투명성(snapshot 표면화용). 주식=투입금액, 선물=명목·증거금·레버리지.
                새 조회 0 — spec(이미 보유)·체결값만. 금액은 체결요약 범주(INV-SEC 무관)."""
                if is_fut:
                    notional = qty * px * mult
                    mr = spec.init_margin_rate or 0.0
                    return {"notional": round(notional, 2),
                            "margin": round(notional * mr, 2) if mr else None,
                            "leverage": round(1.0 / mr, 1) if mr else None,
                            "currency": spec.currency}
                return {"amount": round(qty * px, 2), "currency": spec.currency}

            pxs = f"${fill_price:,.2f}" if spec.currency == "USD" else f"{fill_price:,.2f}"

            if bool(p.get("liquidation")):
                # R6/D6 — 비상청산 booking: sid 무시, (종목, 반대 side) 매칭 차감,
                # 신규 포지션 절대 생성 금지(I7·I8). 2026-07-06 고아 부류 차단.
                self._book_liquidation_fill(symbol, side, filled_qty, fill_price,
                                            spec, mult, is_fut, p, decisions)
            elif side == "buy":
                lg = self.ledger.get(sid)
                if lg is not None and lg.get("side", "long") == "short":
                    # 숏 환매(close/축소) — 선물만 숏 보유 가능. 정산 = (진입−청산)×계약×승수.
                    realized = (lg["entry_price"] - fill_price) * filled_qty * mult
                    lg["qty"] -= filled_qty
                    if lg["qty"] <= 0:
                        del self.ledger[sid]
                elif lg is not None:
                    # 추가 매수(롱) — 평균단가 갱신
                    total = lg["qty"] + filled_qty
                    # L-05 — 정상 경로엔 두 값 모두 양수라 안전하나, 경로 변경 또는
                    # 비정상 fill_qty/ledger qty=0 잔존 시 ZeroDivisionError 잠재. 1줄 가드.
                    if total <= 0:
                        return
                    lg["entry_price"] = (lg["entry_price"] * lg["qty"]
                                          + fill_price * filled_qty) / total
                    lg["qty"] = total
                else:
                    # 신규 롱 진입
                    self.ledger[sid] = {
                        "symbol": symbol, "qty": filled_qty,
                        "entry_date": today, "entry_price": fill_price,
                        "peak_price": fill_price, "side": "long",
                        "strategy_name": p.get("strategy_name", ""),
                        "definition": p.get("definition", {}),
                    }
                    self._record_contract_meta(sid, symbol)
                inv = _invest_of(filled_qty, fill_price)
                ev = {"ts": today, "action": "buy", "symbol": symbol,
                      "qty": filled_qty, "price": fill_price,
                      "strategy": p.get("strategy_name", ""),
                      "reason": ("숏청산" if realized is not None else "매수신호"),
                      "invest": inv}
                if realized is not None:
                    ev["realized_pnl"] = round(realized, 2)
                if netted:
                    ev["netted"] = True
                self._log_trade(ev)
                if is_fut:
                    detail = f"{filled_qty}계약 @ {pxs}"
                    if realized is not None:
                        detail += f" 정산 {realized:+,.0f}"
                    if inv["margin"] is not None and inv["leverage"] is not None:
                        detail += (f" · 명목 {inv['notional']:,.0f} 증거금 "
                                   f"{inv['margin']:,.0f} (레버리지 {inv['leverage']}x)")
                else:
                    detail = f"{filled_qty}주 @ {fill_price:,.0f}원"
                    detail += f" · 투입 {inv['amount']:,.0f}원"
                decisions.append(order_log.decision(
                    "bought", sid, p.get("strategy_name", ""), symbol, detail,
                    {"intended": intended, "fill": fill_price, "invest": inv,
                     **(nx or {})}))
            else:
                lg = self.ledger.get(sid)
                if lg is not None and lg.get("side", "long") == "short":
                    # 숏 추가(확대) — 평균단가 갱신
                    total = lg["qty"] + filled_qty
                    if total <= 0:
                        return
                    lg["entry_price"] = (lg["entry_price"] * lg["qty"]
                                          + fill_price * filled_qty) / total
                    lg["qty"] = total
                elif lg is not None:
                    # 롱 청산/축소 — 실현손익 = (청산−진입)×수량×승수. 선물 승수=계약승수,
                    # 주식 승수=1이라 같은 식으로 주식 실현손익도 계산(주문 내역 손익 표시용,
                    # 수수료·세금 차감 전 총액 — 선물 정산손익과 동일 규약).
                    realized = (fill_price - lg["entry_price"]) * filled_qty * mult
                    lg["qty"] -= filled_qty
                    if lg["qty"] <= 0:
                        del self.ledger[sid]
                elif is_fut:
                    # 신규 숏 진입 (선물만 — sell-to-open). 주식은 보유 없는 매도=무동작(보존).
                    self.ledger[sid] = {
                        "symbol": symbol, "qty": filled_qty,
                        "entry_date": today, "entry_price": fill_price,
                        "peak_price": fill_price, "side": "short",
                        "strategy_name": p.get("strategy_name", ""),
                        "definition": p.get("definition", {}),
                    }
                    self._record_contract_meta(sid, symbol)
                inv = _invest_of(filled_qty, fill_price)
                ev = {"ts": today, "action": "sell", "symbol": symbol,
                      "qty": filled_qty, "price": fill_price,
                      "strategy": p.get("strategy_name", ""),
                      "reason": p.get("reason", ""),
                      "invest": inv}
                if realized is not None:
                    ev["realized_pnl"] = round(realized, 2)
                if netted:
                    ev["netted"] = True
                self._log_trade(ev)
                if is_fut:
                    detail = f"{filled_qty}계약 @ {pxs}"
                    if realized is not None:
                        detail += f" 정산 {realized:+,.0f}"
                    if p.get("reason"):
                        detail += f" ({p.get('reason')})"
                    if inv["margin"] is not None and inv["leverage"] is not None:
                        detail += (f" · 명목 {inv['notional']:,.0f} 증거금 "
                                   f"{inv['margin']:,.0f} (레버리지 {inv['leverage']}x)")
                else:
                    detail = f"{filled_qty}주 @ {fill_price:,.0f}원 ({p.get('reason', '')})"
                    detail += f" · 투입 {inv['amount']:,.0f}원"
                decisions.append(order_log.decision(
                    "sold", sid, p.get("strategy_name", ""), symbol, detail,
                    {"fill": fill_price, "invest": inv, **(nx or {})}))

            # M9: 체결 반영 즉시 영속(락 안) — 디스크가 SSOT. 다른 인스턴스
            # (cycle↔intraday loop)가 reload_state로 항상 최신 체결을 본다.
            self._save()

        # Q5 Tier 1 — 체결 직후 kill switch 평가. 시초가 매수가 장중에 잡혀 자본이
        # day_start 대비 -X% 도달하는 정확한 순간을 잡는다. _daily_loss_limit_pct가
        # 설정되어 있을 때만 평가(cycle 또는 intraday_loop가 설정).
        # 단, cycle 내부에서 호출된 _apply_fill은 skip — cycle이 진입부에서 이미
        # 평가했고, hook이 cycle을 재호출하면 _CYCLE_LOCK 데드락 + 무한 재귀.
        if self._daily_loss_limit_pct is not None and not self._in_cycle:
            fired = self.evaluate_killswitch_now(
                self._daily_loss_limit_pct, decisions)
            if fired and self._ks_trigger_hook is not None:
                try:
                    self._ks_trigger_hook("apply_fill")
                except Exception as e:
                    log.error("[ks-hook] apply_fill 트리거 핸들러 실패: %s", e)

    def _book_liquidation_fill(self, symbol: str, side: str, filled_qty: int,
                               fill_price: float, spec, mult: float, is_fut: bool,
                               p: dict, decisions: list[dict]) -> None:
        """비상청산(kill-switch) 체결 booking — R6/D6 불변식 I7·I8.

        비상청산은 브로커 실보유를 *종목 단위*로 청산하지만 booking 계층은 전략 id(sid)로
        open/close를 판정한다. 합성 sid(`liquidate:{symbol}`)는 원장과 매칭되지 않아 기존
        경로에선 청산이 신규 반대 포지션으로 오기록됐다(2026-07-06 고아). 여기서는 sid를
        무시하고 (종목, 반대 side)로 원장을 매칭해 차감하며, **신규 포지션은 절대 만들지
        않는다**(I7). 매칭 없는 브로커 외부 보유분은 external_liquidated로 기록만 한다.
        """
        strat_name = p.get("strategy_name", "")
        # buy=숏 환매, sell=롱 청산 — 청산 대상 side.
        target_side = "short" if side == "buy" else "long"
        # 결정적 순서(entry_date→sid)로 매칭 포지션 차감 (commingle 안전·I8).
        keys = sorted(
            (k for k, v in self.ledger.items()
             if v.get("symbol") == symbol and v.get("side", "long") == target_side),
            key=lambda k: (self.ledger[k].get("entry_date", ""), k))
        remaining = filled_qty
        realized_total = 0.0
        n_closed = 0
        for k in keys:
            if remaining <= 0:
                break
            lg = self.ledger[k]
            take = min(remaining, int(lg.get("qty", 0)))
            if take <= 0:
                continue
            if target_side == "short":
                realized_total += (lg["entry_price"] - fill_price) * take * mult
            else:
                realized_total += (fill_price - lg["entry_price"]) * take * mult
            lg["qty"] -= take
            if lg["qty"] <= 0:
                del self.ledger[k]
            remaining -= take
            n_closed += take
        external = remaining  # 원장 미추적 브로커 보유분 — I7: 신규 오픈 금지, 기록만.

        unit = "계약" if is_fut else "주"
        pxs = f"${fill_price:,.2f}" if spec.currency == "USD" else f"{fill_price:,.2f}"
        ev = {"ts": kst_today().isoformat(), "action": side, "symbol": symbol,
              "qty": filled_qty, "price": fill_price, "strategy": strat_name,
              "reason": "비상청산", "liquidation": True,
              "closed_qty": n_closed, "external_qty": external}
        if n_closed:
            ev["realized_pnl"] = round(realized_total, 2)
        self._log_trade(ev)

        parts: list[str] = []
        if n_closed:
            seg = f"{n_closed}{unit} 청산 @ {pxs}"
            if is_fut:
                seg += f" 정산 {realized_total:+,.0f}"
            parts.append(seg)
        if external:
            parts.append(f"외부보유 {external}{unit} 청산(원장 미추적)")
        if not parts:
            parts.append("청산 대상 없음")
        decisions.append(order_log.decision(
            "liquidated" if n_closed else "external_liquidated",
            p.get("strategy_id", ""), strat_name, symbol, " · ".join(parts),
            {"closed": n_closed, "external": external,
             "realized": round(realized_total, 2)}))

    def _trace_sizing(self, symbol: str, pos_side: str, *, orderable_raw,
                      credit: int, capacity, pct, target: int) -> None:
        """모델A 사이징 근거 1건 보존 — 관측 전용(사이징 결과에 영향 없음).

        capacity = orderable_raw + credit("빈 상태 최대"), target = floor(pct% × capacity).
        orderable_raw=None은 브로커 조회 실패(발주 보류)를 뜻한다."""
        self._sizing_trace[f"{symbol}|{pos_side}"] = {
            "orderable_raw": orderable_raw, "credit": int(credit),
            "capacity": capacity, "pct": pct, "target": int(target)}

    def _apply_netted_leg(self, leg, decisions: list[dict],
                          reason: str | None = None) -> None:
        """넷팅 핸드오프 1건(진입/청산 leg)을 합성 체결로 원장 반영 — 브로커 미호출·수수료 0.

        설계 §13. 기존 _apply_fill을 재사용해 ledger·realized·통화·side 분기·락을 그대로 태운다
        (합성 체결도 실체결과 동일 회계 → N8·N10 클래스 해소). 슬리피지는 intended_price=None으로
        자동 skip(N2), 킬스위치 훅은 _in_cycle 중 skip(N11). order_no="NETTED-<iid>"가 표식.
        호출부(runner)가 _CYCLE_LOCK 임계구역에서 book_legs 전체를 원자적으로 적용한다(N6).

        **intent 저널 시드를 쓰지 않는다.** intent 저널은 "브로커 발주됐으나 디스크 미기록"을
        복구하는 장치인데(L-01), 넷팅 leg은 브로커를 접촉하지 않아 복구 대상이 없다 — 원장 변경
        (원자 저장)이 완전한 기록이며, 재실행 시 held-check가 재처리를 막는다(N1 해소). 또 시드를
        쓰면 같은 (sid,symbol,side)의 *잔여 실주문*이 is_active 게이트에 막히므로(넷팅+잔여는 한
        청산/진입에서 분할됨) 반드시 쓰지 않는다.
        """
        order_no = "NETTED-" + intents.new_intent_id()   # 표식용 고유 번호(저널 기록 아님)
        p = {
            "strategy_id": leg.sid,          # _apply_fill의 원장 키
            "strategy_name": leg.strategy_name,
            "symbol": leg.symbol,
            "side": leg.order_side,
            "intended_price": None,          # 넷팅 → 슬리피지 미기록(시장 미접촉)
            "limit_price": None,
            "reason": reason or ("넷팅 청산" if leg.kind == "exit" else "넷팅 진입"),
            "kind": "청산" if leg.kind == "exit" else "진입",
            "definition": leg.definition,
        }
        self._apply_fill(order_no, p, leg.qty, leg.ref_price, decisions, netted=True)

    # ── Q5: 장중 kill switch (Tier 1·2 공용 평가/실행 helpers) ─────────────────

    def evaluate_killswitch_now(self, daily_loss_limit_pct: float,
                                  decisions: list[dict] | None = None) -> bool:
        """현재 KIS 잔고 기반 통합 자본을 평가해 일일 손실 한도 초과 시 발동.

        반환: 발동되어 새로 active 됐으면 True (이미 active였거나 미도달이면 False).
        decisions가 주어지면 발동 사유를 결정 로그에 기록.

        Q5: 사이클 시점(08:55/15:35)만 평가하던 기존 동작에 더해, 체결 후(_apply_fill)
        와 장중 60초 monitor에서도 동일 임계로 평가하기 위한 공용 진입점.
        """
        if killswitch.is_active():
            return False
        try:
            snap = self.broker.account_snapshot()
            equity = _unified_equity_krw(snap["balance"])
        except Exception as e:
            log.warning("[ks-eval] account_snapshot 실패 — skip: %s", e)
            return False
        # ★ε: 부분 잔고(해외/선물 조회 실패)로는 발동 금지 — 누락 계좌가 0으로
        # 잡혀 거짓 -98% 폭락이 되고, 06-09에 실제로 US 보유 전량을 청산했다.
        # 전체 조회 실패(위 except)와 동일하게 보수적 무동작 + 표면화.
        fetch_failed = (snap.get("balance") or {}).get("fetch_failed")
        if fetch_failed:
            log.critical("[ks-eval] 잔고 부분조회 %s — 부분 equity(%s원)로 평가 보류",
                         fetch_failed, f"{equity:,.0f}")
            if decisions is not None:
                decisions.append(order_log.decision(
                    "risk_eval_skipped", "", "", "",
                    f"잔고 부분조회 실패 {fetch_failed} — killswitch 평가 보류"))
            return False
        reason = killswitch.check_daily_loss(equity, daily_loss_limit_pct)
        if not reason:
            return False
        killswitch.activate(reason)
        log.critical("[ks-eval] kill switch 발동: %s", reason)
        if decisions is not None:
            decisions.append(order_log.decision(
                "kill_switch", "", "", "", reason))
        return True

    def cancel_all_pending(self, decisions: list[dict] | None = None) -> int:
        """미체결 주문 전체를 KIS에 즉시 cancel 발주. Q5 발동 시 자금 노출 차단용.

        업계 표준(FCA): kill switch 발동 시 "cancel all outstanding orders". 보유분
        강제 청산은 다음 사이클이 책임지지만, 미체결 매수가 늦게 잡혀 손실을 키우는
        시나리오를 차단한다. cancel 자체 실패는 다음 사이클의 _resolve_pending이
        KIS 상태 조회로 정리.

        반환: cancel 시도한 주문 건수.
        """
        # M3: pending 스냅샷만 락 안에서 — KIS cancel(네트워크)은 락 밖에서 수행.
        # cancel은 self.pending을 변경하지 않으므로(회수는 _resolve_pending이 담당)
        # 스냅샷 후 락을 놓아 critical section을 짧게 유지한다.
        with _CYCLE_LOCK:
            if not self.pending:
                return 0
            items = list(self.pending.items())
        n = 0
        for order_no, p in items:
            try:
                self.broker.cancel(order_no, p["symbol"], p["qty"])
                n += 1
                if decisions is not None:
                    decisions.append(order_log.decision(
                        "cancelled", p.get("strategy_id", ""),
                        p.get("strategy_name", ""), p["symbol"],
                        f"kill switch — 미체결 즉시 취소 ({order_no})"))
            except Exception as e:
                log.warning("[ks-cancel] %s 취소 실패: %s", order_no, e)
        log.info("[ks-cancel] %d건 cancel 시도", n)
        return n

    # ── 주문 발주 helpers ────────────────────────────────────────────────────

    def _is_reserved_us(self, symbol: str) -> bool:
        """미국 예약주문 라우팅 여부 — 정상 cycle US 진입(_reserved_us)이고 USD 종목.

        M3: _submit_buy/_submit_sell에 동일 derivation이 중복돼 있던 것을 단일화.
        _reserved_us는 __init__/매 cycle에서 항상 설정되므로 getattr 방어 불필요.

        ⚠ M5b: **선물 제외**. 예약주문은 *대상 시장이 닫힌 시점*(미국주식)에 예약하는 흐름인데,
        선물은 국내(KRX 주간)·해외(CME 거의 24h) 모두 예약 개념이 없다(KisFuturesBroker는
        예약 메서드가 NotImplementedError 가드). 통화 휴리스틱상 해외선물(USD)이 여기 걸려
        롱 진입조차 깨지던 것을 차단 — 선물은 즉시주문(buy_limit/sell_limit) 경로로 간다.
        """
        return (self._reserved_us and _currency_of(symbol) == "USD"
                and not qc.is_futures(symbol))

    def _us_limit(self, symbol: str, side: str, policy: dict,
                  fallback_ref: float) -> float:
        """미국 지정가 = 신선한 현재가 × (1 ± tol) — 시장가 근사(market-proxy).

        미국주식은 KIS가 연속장 시장가를 미지원해 지정가만 가능하다. 지정가가 개장가
        /종가를 넉넉히 brackets하도록 *신선한* 현재가(_safe_price=HHDFS00000300 실시간/
        프리마켓)에 tol 버퍼를 둔다 — 이게 라이브가 백테스트의 시가/종가 체결을 재현하는
        핵심. 전일종가(fallback_ref)는 미국 애프터마켓·갭을 못 담아 미체결을 유발하므로
        *현재가 조회 실패 시에만* 후퇴한다(외부 시스템 한계 정당 fallback).
        side='buy'면 +tol·위로 라운드업(개장가 위), 'sell'이면 −tol·아래로 라운드다운(종가 아래).
        tol = policy.buy/sell_tolerance_pct (미국 전용·유저 override 가능·default ±3%)."""
        ref = self._safe_price(symbol) or fallback_ref
        if side == "buy":
            return _round_limit(ref * (1 + policy["buy_tolerance_pct"] / 100.0),
                                "up", symbol)
        return _round_limit(ref * (1 - policy["sell_tolerance_pct"] / 100.0),
                            "down", symbol)

    def _submit_buy(self, sid: str, strat_name: str, strat_def: dict,
                    symbol: str, qty: int, ref_price: float, policy: dict,
                    decisions: list[dict], catchup: bool = False) -> None:
        # L-01: 발주 직전 intent journal에 submitting 기록(fsync). 크래시-재기동
        # 시 reconcile이 KIS 당일 주문 조회로 매칭 → 중복 발주 방지.
        today_iso = kst_today().isoformat()
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "buy",
                      qty, ref_price)

        # 미국 예약매수 — 개장 전(접수창) 발주, 정규장 개시에 KIS가 자동 전송.
        # KIS는 미국 시장가 매수가 없어 지정가만 가능 → 신선한 현재가×(1+tol) limit
        # (전일종가 ref_price는 갭을 못 담아 _us_limit이 fallback으로만 씀).
        # catch-up과 배타적(_reserved_us는 정상 cycle US에서만 True).
        is_resv = self._is_reserved_us(symbol)
        if is_resv:
            limit = self._us_limit(symbol, "buy", policy, ref_price)
            try:
                r = self.broker.buy_resv_limit(symbol, qty, limit)
            except Exception as e:
                # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
                # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
                # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
                # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
                log.error("미국 예약매수 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"예약발주 예외: {e}"))
                return
            log.info("[us-resv] %s 예약매수 지정가 limit=%s", symbol, limit)
        # catch-up 매수: 시초가 limit으로 변환.
        # 이유: 정상 cycle의 시장가는 09:00 시초가에 체결되나 catch-up은 09:30
        # 현재가에 체결 → 백테스트 가정(시가 + slippage)과 어긋남. 시가 × (1 +
        # bt_slippage_bps) limit으로 변환하면 백테스트 모델과 alignment + selection
        # bias 없음(가격은 시가 fixed). ref_price(어제 종가)는 유지 — apply_daily_
        # price_limit이 prev_close 기준 ±30% cap 정확히 계산하도록.
        elif catchup:
            open_price = self.broker.today_open(symbol)
            if open_price <= 0:
                # v0.9.7-beta — PR-1 정당 fallback (KIS API 진짜 한계 대비).
                # _open_overseas는 HHDFS76200200으로 변경돼 정상 케이스는 open 받음.
                # 그래도 실패 시(휴장일·통신 오류 등) prev_close * (1+slippage)로
                # 보수적 매수 시도. catch-up을 silent skip하지 않고 사용자 매수
                # 의지 존중 — 최악의 경우에도 어제 종가에서 slippage 비용만큼
                # 비싸게 사는 보수적 가격.
                open_price = ref_price  # = prev_close * (1 + buy_tolerance_pct%)
                log.info("[catch-up] %s 시가 미제공 — prev_close 기반 발주 "
                          "(open=%.4f)", symbol, open_price)
            slip = qc.DEFAULT_EXECUTION["bt_slippage_bps"] / 10_000.0
            limit = _round_limit(open_price * (1 + slip), "up", symbol)
            limit = qc.apply_daily_price_limit(
                limit, ref_price, "buy", _currency_of(symbol))
            try:
                r = self.broker.buy_limit(symbol, qty, limit)
            except Exception as e:
                # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
                # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
                # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
                # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
                log.error("[catch-up] %s 시초가 limit 발주 실패: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol,
                    f"catch-up 발주 예외: {e}"))
                return
            log.info("[catch-up] %s 시장가→시초가 limit: open=%s limit=%s",
                      symbol, open_price, limit)
        else:
            # 국내 즉시 매수 — 시장가. 08:55 동시호가 발주 → 09:00 시초가 단일가
            # 체결(지정가 대비 슬리피지 손해 없음). 미국은 위 is_resv(지정가)로 분기.
            limit = 0
            try:
                r = self.broker.buy(symbol, qty)
            except Exception as e:
                # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
                # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
                # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
                # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
                log.error("매수 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        # KIS 응답 수신 — submitted 마감(order_no가 빈 문자면 거부 처리는 _after_submit이 함)
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, strat_def, symbol, "buy", qty,
                            ref_price, limit, policy, decisions, reason="매수신호",
                            today_iso=today_iso, intent_id=intent_id, kind="진입")

    def _submit_sell(self, sid: str, strat_name: str, symbol: str, qty: int,
                     ref_price: float, policy: dict, reason: str,
                     decisions: list[dict], liquidation: bool = False) -> None:
        # L-01: 매도 멱등 단일 게이트(모든 매도 경로 공유) — 오늘 같은 (sid, symbol)
        # 매도 intent가 활성이면 재발주 차단. EOD cycle·장중 tick 손절·catch-up이
        # 첫 매도 미체결(KIS 잔고 미감소)인 동안 같은 포지션을 동시 평가해도 이중매도를
        # 막는다. intent journal이 cycle/장중/catch-up·재기동을 가로지르는 단일 출처.
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, sid, symbol, "sell"):
            # 관측 대칭(2026-07-20): 진입측만 skip_idempotent 결정을 남기고 매도·환매·
            # drift는 INFO 로그뿐이라 원격에서 "왜 청산 주문이 없나"를 못 봤다.
            decisions.append(order_log.decision(
                "skip_idempotent", sid, strat_name, symbol,
                "오늘 이미 발주된 매도 intent 존재 — 중복 차단"))
            log.info("[L-01] 중복 매도 차단 %s/%s", sid, symbol)
            return
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "sell",
                      qty, ref_price)
        # 미국 예약매도 — 지정가(00). 개장 전 접수 → 개장 단일가 체결(MOO 대신 지정가:
        # 모의=실전 통일). limit=신선한가×(1−tol)이 개장가보다 낮아 매도 체결(_us_limit).
        is_resv = self._is_reserved_us(symbol)
        if is_resv:
            limit = self._us_limit(symbol, "sell", policy, ref_price)
            try:
                r = self.broker.sell_resv_limit(symbol, qty, limit)
            except Exception as e:
                # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
                # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
                # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
                # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
                log.error("미국 예약매도 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"예약발주 예외: {e}"))
                return
            log.info("[us-resv] %s 예약매도 지정가 limit=%s", symbol, limit)
        elif _currency_of(symbol) == "USD":
            # 미국 즉시매도(종가 청산 cycle) — 지정가(00). KIS 연속장 시장가 미지원이라
            # 신선한 현재가×(1−tol) 지정가로 시장가 근사. (국내는 아래 시장가 분기)
            limit = self._us_limit(symbol, "sell", policy, ref_price)
            try:
                r = self.broker.sell_limit(symbol, qty, limit)
            except Exception as e:
                # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
                # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
                # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
                # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
                log.error("미국 매도 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
            log.info("[us-close] %s 즉시 매도 지정가 limit=%s", symbol, limit)
        else:
            # 국내 즉시 매도 — 시장가. 종가 동시호가 발주 → 종가 단일가 체결.
            limit = 0
            try:
                r = self.broker.sell(symbol, qty)
            except Exception as e:
                # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
                # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
                # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
                # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
                log.error("매도 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, None, symbol, "sell", qty,
                            ref_price, limit, policy, decisions, reason=reason,
                            today_iso=today_iso, intent_id=intent_id, kind="청산",
                            liquidation=liquidation)

    def _submit_close_short(self, sid: str, strat_name: str, symbol: str, qty: int,
                            ref_price: float, policy: dict, reason: str,
                            decisions: list[dict], liquidation: bool = False) -> None:
        """숏 포지션 환매(buy-to-close) — 청산이므로 매수 주문이나 의미는 청산.

        _submit_sell의 매수판. 선물 전용(숏은 선물만)이라 예약주문 분기 없음(선물은 즉시주문).
        멱등 게이트는 'buy' intent. _after_submit side='buy' → _apply_fill(M4)이 숏 환매로
        해석해 ledger 숏을 차감·정산손익 기록. tolerance는 매수(위로 허용).
        """
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, sid, symbol, "buy"):
            decisions.append(order_log.decision(
                "skip_idempotent", sid, strat_name, symbol,
                "오늘 이미 발주된 환매(숏청산) intent 존재 — 중복 차단"))
            log.info("[L-01] 중복 환매 차단 %s/%s", sid, symbol)
            return
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "buy", qty, ref_price)
        # 숏 환매 — 시장가(선물). 종가 동시호가 발주 → 단일가 체결.
        limit = 0
        try:
            r = self.broker.buy(symbol, qty)
        except Exception as e:
            # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
            # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
            # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
            # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
            log.error("숏 환매 시장가 발주 실패 [%s]: %s", symbol, e)
            decisions.append(order_log.decision(
                "error", sid, strat_name, symbol, f"환매 발주 예외: {e}"))
            return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, None, symbol, "buy", qty,
                            ref_price, limit, policy, decisions, reason=reason,
                            today_iso=today_iso, intent_id=intent_id, kind="청산",
                            liquidation=liquidation)

    def _submit_open_short(self, sid: str, strat_name: str, strat_def: dict,
                           symbol: str, qty: int, ref_price: float, policy: dict,
                           decisions: list[dict]) -> None:
        """숏 진입(sell-to-open) — 매도 주문이나 의미는 신규 숏 포지션 개시. 선물 전용.

        _submit_buy의 매도판. strat_def를 _after_submit에 전달해 ledger에 definition 저장(나중
        _submit_close_short가 그 청산규칙으로 환매). 멱등 'sell' intent. _after_submit side='sell'
        → _apply_fill(M4)이 보유없는 선물 매도를 숏진입으로 해석. 선물 전용→예약 분기 없음.
        """
        today_iso = kst_today().isoformat()
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "sell", qty, ref_price)
        # 숏 진입 — 시장가(선물). 동시호가 발주 → 단일가 체결.
        limit = 0
        try:
            r = self.broker.sell(symbol, qty)
        except Exception as e:
            # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
            # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
            # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
            # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
            log.error("숏 진입 시장가 발주 실패 [%s]: %s", symbol, e)
            decisions.append(order_log.decision(
                "error", sid, strat_name, symbol, f"숏진입 발주 예외: {e}"))
            return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, strat_def, symbol, "sell", qty,
                            ref_price, limit, policy, decisions, reason="숏진입",
                            today_iso=today_iso, intent_id=intent_id, kind="진입")

    def _after_submit(self, r: dict, sid: str, strat_name: str,
                      strat_def: dict | None, symbol: str, side: str, qty: int,
                      intended_price: float, limit_price: int,
                      policy: dict, decisions: list[dict], reason: str,
                      today_iso: str = "", intent_id: str = "",
                      kind: str = "", liquidation: bool = False,
                      drift: bool = False) -> None:
        """submit 결과를 후처리: pending 등록 / 즉시 체결 반영 / 거부 로깅.

        kind: "진입"|"청산"|"교정" — 발주 메서드가 명시(_submit_buy·_submit_open_short=진입,
        _submit_sell·_submit_close_short=청산, _submit_drift=교정). pending 레코드에 실어
        체결 이벤트까지 전파. drift=True면 체결이 원장을 바꾸지 않는다(_apply_fill 초입 분기).
        """
        order_no = r.get("order_no", "")
        if not r.get("success"):
            order_log.log_order("rejected", symbol, side, qty,
                                 order_no=order_no,
                                 intended_price=intended_price,
                                 limit_price=limit_price,
                                 strategy_name=strat_name, reason=reason, kind=kind,
                                 extra={"msg": r.get("message", "")})
            decisions.append(order_log.decision(
                "rejected", sid, strat_name, symbol,
                f"{side} {qty}주 거부: {r.get('message', '')}"))
            # KIS 호출 성공 ≠ 주문 접수 성공 — 거부는 주문 미생성이므로 intent를
            # 실패로 마감해 멱등 게이트 점유를 해제한다(리뷰 D5-5: 'submitted'로
            # 남으면 당일 정당 재시도가 전부 차단 — 06-09 장종료 거부 건이 그날
            # 재시도 불가였던 메커니즘). 재시도는 사이클당 1회 평가라 무한 반복 없음.
            if intent_id:
                intents.mark_failed(today_iso, intent_id,
                                    f"KIS 거부: {r.get('message', '')}")
            return
        p = {
            "order_no": order_no, "strategy_id": sid,
            "strategy_name": strat_name, "symbol": symbol, "side": side,
            "qty": qty, "limit_price": limit_price,
            "intended_price": intended_price,
            "submitted_ts": time.time(),
            # L-01 종결 배선 — 이 주문이 pending에서 사라질 때 어느 intent를 닫아야
            # 하는지. order_no로 저널을 역스캔할 수도 있지만, 정정·재접수로 번호가
            # 바뀌거나 ambiguous가 콤마결합 번호를 쓰는 경우가 있어 직접 싣는다.
            "intent_id": intent_id, "intent_date": today_iso,
            # Q7: timeout_sec 필드 제거 — _resolve_pending이 timeout cancel을
            # 더 이상 사용하지 않음. KIS DAY 정책으로 마감 시 자동 cancel.
            "definition": strat_def or {}, "reason": reason, "kind": kind,
            "filled_so_far": 0,
            # R6/D6 — 비상청산 체결이면 booking이 신규 포지션을 만들지 않고 종목 기준
            # 청산하도록 표식(즉시체결·pending 양 경로·재기동 후에도 유지).
            "liquidation": liquidation,
            # 목표수렴 drift 교정 — 체결돼도 원장 불변(_apply_fill 초입 분기·재기동 유지).
            "drift": drift,
            # δ: 미국 예약주문 여부 — 접수번호와 체결행 odno의 번호공간이 달라
            # (실측 448 vs 10자리) _resolve_pending이 종목+사이드+수량 매칭으로
            # 전환하는 분기 키. 발주 분기(_is_reserved_us)와 같은 시점 동일 값.
            "is_resv": self._is_reserved_us(symbol),
        }
        order_log.log_order("submitted", symbol, side, qty, order_no=order_no,
                             intended_price=intended_price,
                             limit_price=limit_price, strategy_name=strat_name,
                             reason=reason, kind=kind)
        # 일부 KIS 즉시체결 응답엔 체결 정보가 포함돼 있다 — pending 단계 건너뛰고 즉시 반영.
        filled = int(r.get("filled_qty", 0) or 0)
        fill_price = float(r.get("price", 0) or 0)
        if filled >= qty and fill_price > 0:
            self._apply_fill(order_no, p, filled, fill_price, decisions)
            self._resolve_intent(p, "filled")   # pending을 거치지 않는 종결 경로
            return
        # 그렇지 않으면 pending에 등록 → 다음 사이클 또는 _wait_pending이 폴링.
        # M3: pending 등록도 cycle·WS 체결 thread와 같은 락으로 직렬화.
        with _CYCLE_LOCK:
            self.pending[order_no] = p
            # M9/L-01: 발주 직후 즉시 영속 — cycle 끝 저장까지의 크래시 유실창 제거
            # (intents 저널이 중복발주는 막지만, pending 유실은 체결 추적을 끊었다).
            self._save()

    def _wait_pending(self, timeout_sec: int, poll_sec: int,
                      decisions: list[dict]) -> None:
        """이번 사이클에 제출한 주문들이 체결되기를 짧게 기다린다.

        timeout 안에 안 잡힌 건은 _resolve_pending이 다음 사이클에 처리.
        """
        if not self.pending:
            return
        end = time.time() + timeout_sec
        while time.time() < end and self.pending:
            time.sleep(poll_sec)
            self._resolve_pending(decisions)

    # Phase 38.4: _enter_screener·_buy_screener_pick 제거 — 진입은 preview path 전용.
    # 자동 선택 매칭은 서버 preview_engine이 18:15에 수행해 by_strategy에 담아 보냄.

    def _try_buy_one_symbol(self, ledger_key: str, strategy_id: str,
                              strat_name: str, strat_def: dict,
                              symbol: str,
                              dataset: dict, equity_now: float,
                              decisions: list[dict],
                              catchup: bool = False,
                              cand_direction: str | None = None,
                              is_close_entry: bool = False,
                              capacity_credit: dict | None = None,
                              capture: list | None = None) -> bool:
        """수동(단일/다중) 종목 1개에 대해 사이징 + 발주 (EOD-순수 모델).

        Phase 30: 매수 path는 전일 종가만으로 결정. KIS 현재가 호출 없음.
          - 발주 지정가 = 전일 종가 × (1 + buy_tolerance_pct%)
          - 사이징 분모도 전일 종가
          - 갭 필터 없음 — 갭상승 시 발주가 초과로 미체결 → 자연 회피
          - 매수 신호 평가는 호출 전에 1회만 수행 (다중 후보 모두에 동일 적용)
        잔고는 매 호출마다 재조회해 다중 매수 중 자금 소진을 정확히 반영한다.
        """
        sdf = dataset.get(symbol)
        if sdf is None or len(sdf) == 0 or "Close" not in sdf.columns:
            # dataset에 없는 종목 — 전일 종가 없음. 자동 fallback 없이 명시적 skip.
            decisions.append(order_log.decision(
                "skip_no_data", strategy_id, strat_name, symbol,
                "전일 종가 없음 — 매수 대상 종목이 dataset에 없음 (서버 dataset 갱신 대기)"))
            return False

        prev_close = float(sdf["Close"].iloc[-1])
        if prev_close <= 0:
            decisions.append(order_log.decision(
                "skip_no_data", strategy_id, strat_name, symbol,
                "전일 종가가 0 — 데이터 이상"))
            return False

        # 신선도 게이트(fail-stale) — 마지막 봉이 직전 거래일보다 오래면 stale. 07-14 사고의
        # 직접 원인(서버가 08:10 선물을 재포장 안 해 07-10 봉이 참조가로 쓰임). stale면 live
        # (_safe_price) 우선, live도 없으면 진입 skip(fail-closed — stale로 발주가·사이징·넷팅
        # 오염보다 이번 진입 보류가 안전). is_close_entry는 아래에서 어차피 live를 쓰지만,
        # next_open 진입(07-14 #27)은 이 게이트에서만 stale이 걸러진다.
        if _bar_is_stale(sdf, symbol, kst_today()):
            cur = self._safe_price(symbol)
            if cur and cur > 0:
                prev_close = cur
            else:
                decisions.append(order_log.decision(
                    "skip_stale_data", strategy_id, strat_name, symbol,
                    "데이터 stale — 마지막 봉이 직전 거래일보다 오래됨 · 실시간가도 없음 (진입 보류)"))
                return False

        # 종가 진입(fill=close): 참조가를 종가 무렵 현재가로 교체. 전일종가 기준은 15:40 시점엔
        # 오늘 등락만큼 stale → 지정가가 종가 단일가창을 빗나가 미체결된다. 청산(liquidate_day_
        # trades)이 쓰는 것과 동일하게 _safe_price(현재가) 기준으로 통일한다 — 이후 사이징·발주가·
        # 선물 orderable 힌트가 모두 이 참조가를 쓴다. 현재가 조회 실패 시 dataset 전일종가 유지
        # (발주 보류보다 나은 근사 — 청산 fallback과 동형).
        if is_close_entry:
            cur = self._safe_price(symbol)
            if cur and cur > 0:
                prev_close = cur

        # Phase 48 — 거래정지·관리종목·투자위험 자동 차단. KIS broker가 거부로
        # 2차 안전망을 제공하나 사이클 중 불필요한 발주 시도를 줄인다. status를
        # 알 수 없는 종목(서버 데이터 누락)은 일반 종목으로 취급 (보수 안전 fallback).
        status = (getattr(self, "_krx_status", None) or {}).get(symbol) or {}
        if status.get("is_halt"):
            decisions.append(order_log.decision(
                "skip_halted", strategy_id, strat_name, symbol,
                "거래정지·정리매매 종목 — 매수 발주 차단"))
            return False
        if status.get("is_managed"):
            decisions.append(order_log.decision(
                "skip_managed", strategy_id, strat_name, symbol,
                "관리·투자위험·투자경고 종목 — 매수 발주 차단"))
            return False

        # R-2(2026-07-10 리뷰) — 국내 당일매매(hold_days==0·개장 진입 설계)는 개장 창에서만
        # 신규 진입. 낮에 앱을 시작하면 catch-up 사이클이 임의 시각에 진입해(07-06 12:33
        # 실측) 백테스트(시가 진입→종가 청산)와 어긋난 반쪽 노출이 됐다. 컷오프 09:30은
        # 아침 사이클 재시도 사다리(08:55+60/300/900s ≈ 최종 09:16)를 포용하는 상한.
        # 종가매수(fill=close)는 종가창 라우팅이라 대상 아님. 미국 상품은 동적 야간
        # 플래너가 세션 시작을 판단하므로 대상 아님(KRW만).
        _exit_cfg = ((strat_def.get("position") or {}).get("exit") or {})
        _fill = ((strat_def.get("simulation") or {}).get("fill") or "next_open")
        if (_exit_cfg.get("hold_days") == 0 and _fill != "close"
                and _currency_of(symbol) == "KRW"):
            _now = kst_now()
            if (_now.hour, _now.minute) >= (9, 30):
                decisions.append(order_log.decision(
                    "skip_late_daytrade", strategy_id, strat_name, symbol,
                    "당일매매는 개장 창(~09:30)에만 신규 진입 — 지금 진입하면 백테스트"
                    "(시가 진입→종가 청산)와 어긋난 반쪽 노출. 다음 개장에 재평가"))
                return False

        # Phase 48 P1-D — 일일 거래 한도 차단 (한도 활성 시만 호출).
        tcount_limit = getattr(self, "_daily_trade_count_limit", 0)
        tturn_limit = getattr(self, "_daily_turnover_limit_krw", 0)
        if tcount_limit > 0 or tturn_limit > 0:
            today_iso = kst_today().isoformat()
            tcount, tturn = self._today_buy_summary(today_iso)
            if tcount_limit > 0 and tcount >= tcount_limit:
                decisions.append(order_log.decision(
                    "skip_daily_count", strategy_id, strat_name, symbol,
                    f"일일 거래 횟수 한도 도달 ({tcount}/{tcount_limit}) — 매수 차단"))
                return False
            if tturn_limit > 0 and tturn >= tturn_limit:
                decisions.append(order_log.decision(
                    "skip_daily_turnover", strategy_id, strat_name, symbol,
                    f"일일 거래 대금 한도 도달 ({tturn:,}/{tturn_limit:,} KRW) — 매수 차단"))
                return False

        policy = _policy(strat_def)

        # M5c/M5d — 진입 방향. long(기본)=매수진입·short=매도진입(sell-to-open, 선물만).
        # long_short(부호방향 directional)는 종목별 당일 방향을 preview 후보(_select 부호)가
        # 가져온다 → cand_direction으로 체결(엔진 _direction_for 거울). 방향 정보 없는
        # long_short 후보(구 preview·횡단 랭킹)는 무음 롱전환 방지 위해 명시 skip.
        direction = (strat_def.get("position") or {}).get("direction", "long")
        if direction == "long_short":
            if cand_direction not in ("long", "short"):
                decisions.append(order_log.decision(
                    "skip_unsupported", strategy_id, strat_name, symbol,
                    "long_short 후보에 방향 정보 없음 — 부호방향 directional 전략만 라이브 가능"))
                return False
            direction = cand_direction
        is_short = direction == "short"
        _pos_side = "short" if is_short else "long"   # 사이징 크레딧 side 키(§18)

        # 통화별 가용자금 결정.
        #  - 미국: psamount(매수가능금액) — KIS 통합증거금을 반영한 USD 주문가능액.
        #    USD 예수금이 0이어도 KRW 담보로 주문 가능하므로 예수금이 아니라
        #    "주문가능액"을 기준으로 사이징한다. max_qty로 상한도 클램프.
        #  - 국내: KRW 예수금.
        # cash·capital·prev_close 단위를 종목 통화로 일치시킨다.
        ccy = _currency_of(symbol)
        max_cap = None
        try:
            if ccy == "USD":
                # 매수여력 모드 (사용자 설정): integrated=통합증거금(주문가능액) /
                # usd_cash=USD 예수금 한정(보수적, FX 노출 없음).
                mode = getattr(self, "_us_bp_mode", "integrated")
                if mode == "usd_cash":
                    bal = self.broker.account_snapshot()["balance"]
                    cash = float(bal.get("cash_usd", 0) or 0)
                    fx = float(bal.get("fx_usdkrw", 0) or 0)
                else:   # integrated (기본)
                    bp = self.broker.buying_power_usd(symbol, prev_close)
                    cash = float(bp.get("usd_orderable", 0) or 0)
                    fx = float(bp.get("fx_usdkrw", 0) or 0)
                    max_cap = int(bp.get("max_qty", 0) or 0)
                # equity_now는 KRW 통합자산 → USD 환산해 atr capital에 사용
                capital = (equity_now / fx) if (fx > 0 and equity_now > 0) else cash
            else:
                # KRX 사이징은 국내 현금만 필요 — 해외 API 2건 skip (효율)
                bal = self.broker.account_snapshot(overseas=False)["balance"]
                if qc.is_futures(symbol):
                    # 선물 주문은 해당 시장 선물계좌 가용증거금으로 사이징(시장별 분리 —
                    # 다계좌 합산 과대사이징 방지). 미배선/구브로커면 주식 cash로 graceful fallback.
                    from quant_core.futures_contract import futures_market
                    _mkt = "us" if futures_market(symbol) == "CME" else "kr"
                    cash = float(bal.get(f"futures_order_cash_{_mkt}")
                                 or bal.get("cash") or 0)
                else:
                    cash = float(bal["cash"])
                capital = equity_now if equity_now > 0 else cash
        except Exception as e:
            log.error("가용자금 조회 실패 [%s]: %s", symbol, e)
            decisions.append(order_log.decision(
                "error", strategy_id, strat_name, symbol,
                f"가용자금 조회 실패: {e}"))
            return False

        # E1/§18: 주식은 같은-종목 보유가 되돌려줄 현금을 크레딧(선물은 아래 orderable 계약).
        # 사이징 입력이지 물리 주문이 아니다 — 물리는 목표수렴 net이 상쇄(_reconcile_pass).
        # credit_for = 계획청산(같은-편) + 원장 밖 수동보유(방향무관·원복 전제) 합산.
        if capacity_credit and not qc.is_futures(symbol):
            cash += credit_for(capacity_credit, symbol, _pos_side)

        # 사이징 — 전일 종가 기준 (cash·prev_close 모두 종목 통화).
        # IR(전략 연구소)은 position.sizing(이벤트 진입 예산)으로 사이징한다.
        # ir_live.event_buy_qty가 백테스트 엔진 _budget과 동일(amount_krw 또는
        # cash×amount_pct%, 단일 유니버스=100%) + max_position_pct 캡까지 처리한다.
        from quant_core.ir_engine import StrategyIR
        from quant_core.ir_engine import live as ir_live
        # F-01: symbol·dataset 전달 — amount_krw(₩정액)를 USD 종목에 쓸 때 dataset의
        # 원달러환율(엔진 _budget과 같은 fx_usdkrw_rate 룩업)로 환산한다. 미전달이면
        # USD 정액이 ₩금액을 $로 취급해 1,370배 과대 사이징(실증: GOOG 2,832주=$93만).
        _ir = StrategyIR.model_validate(strat_def)
        qty = ir_live.event_buy_qty(_ir,
                                    cash=cash, prev_close=prev_close, capital=capital,
                                    symbol=symbol, dataset=dataset)

        # ── 국내 선물 라이브 사이징 — 모델 A: 브로커 주문가능수량(실제 동적 증거금률 반영)이 1차 기준 ──
        # 카탈로그 추정 증거금률(init_margin_rate 0.10)의 ~2배 과다산정을 제거. 사용률%만 유저가 정하고
        # 계약당 증거금률은 브로커가 정한다 → 라이브 계약수 = floor(사용률% × 주문가능수량).
        from quant_core.futures_contract import futures_market
        if qc.is_futures(symbol) and futures_market(symbol) != "CME":
            # getattr 가드: SimBroker(paper/sim)엔 orderable_qty 없음 → 모델A 건너뜀(event_buy_qty 유지·degrade
            # 아님). 라이브(BrokerRouter)만 orderable_qty 보유 → 모델A 적용. 기존 sim 시나리오 byte-identical.
            oq_fn = getattr(self.broker, "orderable_qty", None)
            if oq_fn is not None:
                side = "sell" if is_short else "buy"
                orderable = oq_fn(symbol, side, prev_close)
                # 관측(2026-07-20): 브로커 원값과 크레딧을 분리 보존 — 합산 후 값만 남기면
                # "목표가 왜 N인가"를 사후에 역산해야 한다(mwmw 07-20 조사 실비용).
                _orderable_raw = int(orderable) if orderable is not None else None
                _credit = (int(credit_for(capacity_credit, symbol, _pos_side))
                           if (orderable is not None and capacity_credit) else 0)
                if orderable is not None and capacity_credit:
                    # E1/N7/§18: 같은-계약 보유가 되돌려줄 여력(계약수)을 크레딧 — orderable은
                    # 브로커의 **신규**주문가능수량(LS NewOrdAbleQty·청산분 제외)이라 여기에
                    # 보유를 되돌려야 "빈 상태 최대"가 된다(수동 취소 후 잔고 기준·유저 원칙).
                    # 계획청산=같은-편만, 수동보유=방향무관(credit_for 참조). 다중 진입은 소진(E2·N9).
                    orderable = int(orderable) + int(credit_for(capacity_credit, symbol, _pos_side))
                if orderable is None:
                    # 실 브로커(orderable_qty 보유)인데 조회 실패 → **발주 보류**(qty=0). 카탈로그
                    # 추정 증거금률(0.10)로 사이징하면 실제(~19.5%) 대비 ~2배 과대 계약수가 되어,
                    # 큰 금액 실전에서 의도(사용률%)의 2배 레버리지를 시도하게 된다. "조회 안 되면
                    # 안 한다"가 안전한 기본값 — qty=0으로 아래 `qty<=0 → skip_funds`가 사유 표면화.
                    # (sim/paper는 orderable_qty 메서드 자체가 없어 이 분기에 도달 안 함 — 무영향.)
                    qty = 0
                    log.warning("[모델A] %s 브로커 주문가능수량 조회 실패 — 발주 보류"
                                "(카탈로그 추정 2배 과대 사이징 회피)", symbol)
                    self._trace_sizing(symbol, _pos_side, orderable_raw=None,
                                       credit=0, capacity=None, pct=None, target=0)
                else:
                    pct = ir_live.futures_margin_pct_of(_ir)
                    qty = ir_live.model_a_qty(qty, orderable, pct)
                    log.info("[모델A] %s 주문가능 %d(브로커 %s + 크레딧 %d) → 목표 %d계약 (%s)",
                             symbol, orderable, _orderable_raw, _credit, qty,
                             f"사용률 {pct:g}%" if pct is not None else "정액 클램프")
                    self._trace_sizing(symbol, _pos_side, orderable_raw=_orderable_raw,
                                       credit=_credit, capacity=orderable, pct=pct,
                                       target=int(qty))

        # 가용 현금 한도 — 주식만. 선물은 event_buy_qty가 이미 증거금으로 클램프했고,
        # cash//prev_close(현금÷지수가)는 선물 계약수에 무의미(과대 → 비바인딩)하므로 제외.
        if not qc.is_futures(symbol):
            qty = min(qty, int(cash // prev_close))

        # 미국: 주문가능수량(통합증거금 상한) 초과 방지
        if max_cap is not None:
            qty = min(qty, max_cap)

        if qty <= 0:
            decisions.append(order_log.decision(
                "skip_funds", strategy_id, strat_name, symbol,
                f"수량 부족 (현금 {cash:,.0f} / 전일종가 {prev_close:,.0f})"))
            return False

        # L-01 멱등 게이트 — 오늘 같은 (sid, symbol, 진입방향)로 이미 발주됐다면 skip.
        # 진입측 = short면 'sell'(sell-to-open)·long이면 'buy'. 크래시 재기동 + reconcile이
        # submitted/ambiguous로 마감했으면 차단.
        entry_side = "sell" if is_short else "buy"
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, ledger_key, symbol, entry_side):
            decisions.append(order_log.decision(
                "skip_idempotent", strategy_id, strat_name, symbol,
                "오늘 이미 발주된 intent 존재 — 중복 차단"))
            log.info("[L-01] 중복 진입 차단 %s/%s (%s)", ledger_key, symbol, entry_side)
            return False

        # 수렴 PLAN(capture) — 발주하지 않고 진입 '의도'만 수집. 사이징·게이트는
        # 위에서 이미 실주문과 동일하게 통과했다(진입 1회 사이징·보유 중 고정 §9).
        # capacity_credit 차감 = 다중 진입의 회수 여력 순차 소진(E2·N9).
        if capture is not None:
            from .netting import Intent
            spec = instrument_spec(symbol)
            capture.append(Intent(
                sid=ledger_key, strategy_id=ledger_key, strategy_name=strat_name,
                contract_key=self._resolve_contract_key(symbol), symbol=symbol,
                kind="entry", position_side=("short" if is_short else "long"),
                order_side=("sell" if is_short else "buy"), qty=int(qty),
                ref_price=float(prev_close), entry_price=None,
                mult=spec.multiplier, currency=spec.currency, definition=strat_def))
            if capacity_credit is not None:
                consume_credit(capacity_credit, symbol, _pos_side,
                               qty if qc.is_futures(symbol) else qty * prev_close)
            return True

        # 발주가는 submit 내부에서 prev_close × (1 ± tolerance%) 계산.
        if is_short:
            # 숏 진입(sell-to-open) — 선물 전용. 예약·catchup 없음(선물은 즉시주문).
            self._submit_open_short(ledger_key, strat_name, strat_def, symbol, qty,
                                    prev_close, policy, decisions)
        else:
            # 롱 진입(buy). catchup=True면 시장가 매수만 시초가 limit으로 변환(지정가는 그대로).
            self._submit_buy(ledger_key, strat_name, strat_def, symbol, qty,
                              prev_close, policy, decisions, catchup=catchup)
        return True

    # ── 메인 사이클 ───────────────────────────────────────────────────────────

    def _enter_from_preview(self, by_strategy: list[dict], strategies: list[dict],
                              dataset: dict, equity_now: float,
                              decisions: list[dict],
                              sold_this_cycle: set[str],
                              market: str = "KRX",
                              catchup: bool = False,
                              entry_window: str = "open",
                              instrument_class: str | None = None,
                              capacity_credit: dict | None = None,
                              capture: list | None = None) -> None:
        """Phase 37: 서버 preview의 candidates 종목을 직접 발주.

        매수 신호 재평가는 skip (preview가 어제 18:15에 이미 평가).
        잔고·사이징은 _try_buy_one_symbol이 발주 직전 KIS 재조회로 재계산 →
        밤사이 수동 거래·입금 반영. 보유/한도·중복 진입 체크는 기존과 동일.

        market: 이번 사이클 시장 그룹. 해당 시장 후보만 진입(미국 종목은 미국
        정규장 사이클에서만 발주). 다른 시장 후보는 skip한다.

        candidates의 종목 코드는 신뢰하되 dataset에 없는 종목은 skip
        (방어적 — preview·dataset가 같은 서버 상태에서 만들어졌으면 일치).
        """
        strat_def_by_id = {str(s["id"]): (s.get("name", ""), s.get("definition", {}),
                                            s.get("account_ref"))
                             for s in strategies}
        # P5-3 — 활성 계좌 핸들 집합(사이클당 1회·로컬 keyring). 조회 실패 시 보수적 빈 집합
        # (account_ref 바인딩 전략은 skip, 레거시 None은 통과) — 불확실하면 실거래 안 함.
        try:
            active_ids = set(account_handle.active_account_ids())
        except Exception as e:
            log.warning("활성 계좌 핸들 조회 실패 — account_ref 전략 보수적 skip: %s", e)
            active_ids = set()
        n_preview_used = 0
        for entry in by_strategy:
            sid = str(entry.get("strategy_id", ""))
            cands = entry.get("candidates") or []
            if not cands:
                continue
            name_def = strat_def_by_id.get(sid)
            if name_def is None:
                # 서버 preview에 있지만 로컬엔 배정 안 된 전략 — skip
                continue
            strat_name, strat_def, acct_ref = name_def
            # 템플릿 이중 안전망 — 이 로컬앱이 모르는 템플릿 id의 전략은 어떤 경로로 후보가
            # 오든 진입하지 않는다(1차는 서버 앱버전 게이트 — 여기는 방어선). 조용한 반쪽
            # 실행 금지: 결정 로그로 표면화한다(장중 템플릿 설계 §2.5).
            _tpl_id = (strat_def.get("template") or {}).get("id")
            if _tpl_id:
                from quant_core.ir_engine.templates import TEMPLATES as _TPL
                if _tpl_id not in _TPL:
                    decisions.append(order_log.decision(
                        "skip_unknown_template", sid, strat_name, "",
                        f"이 앱 버전이 지원하지 않는 템플릿({_tpl_id}) — 로컬앱을 업데이트하세요"))
                    continue
            # 진입창 라우팅(fill) — 엔진 defer(fill=="next_open")와 동일 분할: next_open은
            # 아침 시가창(open), close/typical은 종가창(close)이 전담한다. 한 창에서 다른 fill
            # 전략을 진입하지 않는다 — 종가매수(fill=close)가 아침 시가창서 시가진입 전략들과
            # 증거금 경쟁에 밀려 미체결되던 근본(전략18)을 닫는다. 종가매수 진입은 종가창 전담.
            strat_fill = (strat_def.get("simulation") or {}).get("fill") or "next_open"
            is_close_fill = strat_fill in ("close", "typical")
            if strat_fill == "trigger":
                # 장중 트리거 전략(워치리스트 템플릿) — 표준 창(아침/종가)에선 진입하지 않는다.
                # 감시·발화는 intraday_loop의 EntryTriggerManager 전담(entry_window="intraday").
                # 창 오배정으로 트리거 전략이 아침 시가에 조용히 진입하는 divergence 차단.
                if entry_window != "intraday":
                    continue
            elif (entry_window == "open") == is_close_fill:
                continue
            # degenerate 방어 — 종가매수(fill=close)+당일청산(hold_days==0)은 진입한 바에 청산할
            # 창이 없다(백테스트는 진입=청산 바라 순노출 0). 라이브는 종가에 진입 후 청산 창이
            # 없어 오버나이트로 남아 backtest≠live가 된다 → 진입 안 하고 표면화(오버나이트 롱은
            # 보유일수≥1로). 아침창(fill=next_open+hold_days==0=정상 당일매매)은 이 분기에 안 온다.
            if entry_window == "close":
                _hd = ((strat_def.get("position") or {}).get("exit") or {}).get("hold_days")
                if _hd == 0:
                    decisions.append(order_log.decision(
                        "skip_unsupported", sid, strat_name, "",
                        "종가매수+당일청산(보유일수 0)은 청산 창이 없어 진입하지 않습니다 — "
                        "오버나이트 롱은 보유일수를 1 이상으로 설정하세요"))
                    continue
            # P5-3 (계좌-전략 연동) — 전략이 특정 계좌(account_ref)에 묶였는데 그 계좌가 현재
            # 활성 계좌가 아니면 전략 통째 skip. 모의 검증 전략이 실전 계좌로 무경고 실거래되는
            # 것(C7)을 식별자 차원에서 차단. account_ref=None(레거시·미바인딩)은 통과(기존 거동).
            if acct_ref and acct_ref not in active_ids:
                decisions.append(order_log.decision(
                    "skip_wrong_account", sid, strat_name, "",
                    "이 전략은 다른 계좌에 묶여 있어 현재 활성 계좌에서 실행되지 않습니다"))
                continue
            # P1 커버리지 게이트 — 이 전략 후보가 요구하는 자산군 중 자격증명 미등록이 있으면
            # 전략을 통째 skip(naked-leg·오라우팅 차단). 한 leg만 발주하지 않는다.
            missing = coverage.missing_categories(c.get("symbol", "") for c in cands)
            if missing:
                decisions.append(order_log.decision(
                    "skip_uncovered", sid, strat_name, "",
                    f"자격증명 미등록 자산군: {', '.join(sorted(missing))} — 전략 skip"))
                continue
            # IR(전략 연구소)은 universe.kind로 다중키 여부를 결정한다. 후보(cands)는
            # 서버 preview가 이미 선정했다.
            uni_kind = (strat_def.get("universe") or {}).get("kind", "single")
            is_multi_key = uni_kind in ("list", "all")
            if is_multi_key:
                prefix = f"{sid}:"
                held_keys = {k for k in self.ledger if k.startswith(prefix)}
                slots_left = max(0, len(cands) - len(held_keys))
                if slots_left <= 0:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, "",
                        f"IR 보유 한도 충족 ({len(held_keys)}종목)"))
                    continue
            else:
                if sid in self.ledger or sid in sold_this_cycle:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, "", "이미 보유 또는 당일 청산"))
                    continue
                slots_left = 1

            bought = 0
            n_attempted = 0
            for c in cands:
                if bought >= slots_left:
                    break
                symbol = c.get("symbol", "")
                if not symbol:
                    continue
                # 시장 배칭 — 이번 사이클 시장의 후보만 진입
                if _market_group_safe(symbol) != market:
                    continue
                # 종가창은 클래스별 분리 발주창(주식 15:25·선물 15:40)이라 클래스도 필터한다 —
                # 15:25 주식 종가창이 선물 후보를 진입(또는 그 반대)하지 않도록. 아침 시가창
                # (instrument_class=None)은 주식·선물을 같은 08:55에 함께 진입(기존 거동 유지).
                if instrument_class is not None:
                    if (instrument_class == "futures") != qc.is_futures(symbol):
                        continue
                ledger_key = f"{sid}:{symbol}" if is_multi_key else sid
                if ledger_key in self.ledger or ledger_key in sold_this_cycle:
                    continue
                n_attempted += 1
                if self._try_buy_one_symbol(
                        ledger_key, sid, strat_name, strat_def,
                        symbol, dataset, equity_now, decisions,
                        catchup=catchup, cand_direction=c.get("direction"),
                        is_close_entry=(entry_window in ("close", "intraday")),
                        capacity_credit=capacity_credit, capture=capture):
                    bought += 1
                    n_preview_used += 1
            # R3 단기 관측 — 사이클 진입 목표(슬롯) 대비 확정 미달을 즉시 표면화.
            # top-up 부재(SZ-1)로 미달이 다음 회차에서 skip_held+net0으로 조용히
            # 영구 확정되던 부류의 "신호 없음"을 닫는다(수정 본체인 top-up은
            # 멱등 원리 트레이드오프가 있어 별도 설계 승인 대상 — 관측 먼저).
            # 조건: 슬롯을 못 채웠고 + 실패한 시도가 실제로 있었을 때만 —
            # 후보 부족(bought==attempted)이나 슬롯 충족은 정상이라 무경보.
            if bought < slots_left and n_attempted > bought:
                decisions.append(order_log.decision(
                    "entry_shortfall", sid, strat_name, "",
                    f"진입 슬롯 {slots_left} 중 확정 {bought} (시도 {n_attempted}"
                    f"·실패 {n_attempted - bought}: 거부·사이징0·가드). 재실행은 "
                    "보유분을 목표로 재정의하므로 자동 보충되지 않음(SZ-1)"))

        log.info("preview 경로 진입 완료 — %d종목 발주 (신호 재평가 skip)",
                  n_preview_used)

    def _today_buy_summary(self, today_iso: str) -> tuple[int, int]:
        """오늘자 매수 거래의 (횟수, 누적 금액 KRW) 반환 (Phase 48 P1-D).

        TRADES_PATH(JSONL)를 한 번 스캔. 일 단위 한도 체크용이라 매수만 카운트.
        한도 비활성(둘 다 0)이면 호출 자체 skip하므로 비용은 활성 사용자만.
        """
        count = 0
        turnover = 0
        if not TRADES_PATH.exists():
            return 0, 0
        try:
            with open(TRADES_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("action") != "buy":
                        continue
                    ts = str(ev.get("ts") or "")
                    if not ts.startswith(today_iso):
                        continue
                    count += 1
                    qty = int(ev.get("qty", 0) or 0)
                    price = float(ev.get("price", 0) or 0)
                    turnover += qty * int(price)
        except Exception as e:
            log.warning("[P1-D] today_buy_summary 읽기 실패: %s", e)
        return count, turnover

    def liquidate_all_held(self) -> dict:
        """비상 전량 청산 — 일반 cycle과 독립. dataset·preview·전략파싱·시장스코프 무의존.

        근본 원칙: 비상정지는 안전 기능이므로 최소 의존·격리돼야 한다. 보유분을 파는
        데엔 신호·dataset이 필요 없으므로, 청산 대상을 **브로커 계좌 실보유**
        (account_snapshot)에서 직접 취득한다 — ledger/전략 파싱/dataset 다운로드와 무관.
        전략이 삭제된 고아·외부 매수분도 보유면 청산된다(run_cycle 재사용 시 dataset
        다운로드 hang·파싱 실패·단일 시장 스코프에 막히던 구조적 결함을 닫음).

        시세는 KIS 현재가(_safe_price), 못 구하면 시장가 주문(가격 의존 제거). KR·US·선물
        보유 전체를 시장 무관하게 즉시 매도(롱)·환매(숏). 열린 시장은 체결, 닫힌 시장은
        broker가 거부를 반환(decisions에 rejected로 명확 표면화). 매도 경로는 정규
        cycle과 동일한 _submit_sell/_submit_close_short(멱등 게이트·pending 기록 공유).

        호출자(gui LIQUIDATE_ALL)가 killswitch.activate를 먼저 수행하고 이 결과를
        push_snapshot한다.
        """
        decisions: list[dict] = []
        today = kst_today()
        # 직전 미체결 상태 먼저 정리(이중매도 방지 게이트 정합).
        self._resolve_pending(decisions)
        try:
            snap = self.broker.account_snapshot()
        except Exception as e:
            log.error("[비상청산] KIS 잔고 조회 실패 — 청산 보류(다음 시도 가능): %s", e)
            decisions.append(order_log.decision(
                "skip_kis_health", "", "", "", f"KIS 잔고 조회 실패: {e}"))
            return self._state_payload(decisions, today, kind="emergency_liquidation")

        policy = merged_execution(None)
        positions = snap.get("positions") or []
        log.info("[비상청산] 보유 %d종 전량 청산 시작 (dataset 무의존)", len(positions))
        for pos in positions:
            symbol = str(pos.get("symbol") or "")
            qty = int(pos.get("qty") or 0)
            # KIS 국내선물 잔고는 side를 'buy'/'sell'로 줘서 raw 비교('short')는
            # 'sell' 숏을 롱으로 오인 → 청산(환매) 대신 추가 매도 = 숏 2배 확대
            # (리뷰 D5-3). norm_side가 표기 분열을 닫는 단일 출처.
            from .analytics import norm_side
            side = norm_side(pos.get("side"))
            if not symbol or qty <= 0:
                continue
            ref_price = self._safe_price(symbol) or 0.0
            # 비상 청산은 보유 sid 무관 — 종목 단위 멱등 키.
            sid = f"liquidate:{symbol}"
            if side == "short":
                self._submit_close_short(sid, "비상청산", symbol, qty,
                                         ref_price, policy, "kill-switch", decisions,
                                         liquidation=True)
            else:
                self._submit_sell(sid, "비상청산", symbol, qty,
                                  ref_price, policy, "kill-switch", decisions,
                                  liquidation=True)
        # 즉시 체결/거부 반영(시장가 체결·장마감 거부를 결과에 표면화).
        self._resolve_pending(decisions)
        return self._state_payload(decisions, today, kind="emergency_liquidation")

    def plan_close_liquidations(self, dataset: dict, instrument_class: str,
                                market: str, snap_pre: dict) -> list:
        """종가창 청산 '의도'를 산출(발주 안 함) — 넷팅 PLAN 단계(설계 §13).

        liquidate_day_trades와 **동일 선택·클램프 기준**(hold_days==0·실보유 클램프·종가 참조가)으로
        청산 Intent 리스트를 만든다. 발주·대기는 하지 않는다 — 넷팅 후 잔여만 실발주(runner).
        선물은 contract_key=실제 계약코드(원장 contract_code)로 물리 계약을 식별(E6·롤 경계 방어).
        """
        from .netting import Intent
        today = kst_today()
        out: list = []
        for sid, pos in list(self.ledger.items()):
            if _market_group_safe(pos["symbol"]) != market:
                continue
            is_fut = qc.is_futures(pos["symbol"])
            if (instrument_class == "futures") != is_fut:
                continue
            hold_days = (((pos.get("definition") or {}).get("position") or {})
                         .get("exit") or {}).get("hold_days")
            # exit.fill=close(재설계 D3): 보유기간 만기 청산을 종가창에서 체결. due 여부는
            # 아래 _exit_reason_for(is_close=True)의 창 게이트가 판정(미도달=None→skip).
            _ef = (((pos.get("definition") or {}).get("position") or {})
                   .get("exit") or {}).get("fill")
            if hold_days != 0 and _ef != "close":
                continue
            try:
                held = _held_trading_days(dataset, pos["symbol"],
                                          pos["entry_date"], today)
                reason, _ = _exit_reason_for(
                    pos["definition"], held, dataset, pos["symbol"], is_close=True)
            except Exception as e:
                log.warning("종가청산 계획 파싱 실패 [%s]: %s", sid, e)
                continue
            if not reason:
                continue
            ref_price = self._safe_price(pos["symbol"]) or 0.0
            if ref_price <= 0:
                sdf = dataset.get(pos["symbol"])
                if sdf is not None and len(sdf) and "Close" in sdf.columns:
                    ref_price = float(sdf["Close"].iloc[-1])
            if ref_price <= 0:
                # R-5(2026-07-10 리뷰) — 종전엔 무로그 continue: 청산 계획에서 조용히 빠져
                # 포지션 잔존이 어디에도 안 보였다. 최소 로그로 표면화(계획 단계라 보류만).
                log.error("[청산계획] 참조가 없음 [%s] — 이번 창 청산 계획에서 보류", pos["symbol"])
                continue
            pos_side = pos.get("side", "long")
            held_now = held_qty_from_snapshot(snap_pre, pos["symbol"], pos_side)
            clamped = clamp_sell_qty(held_now, int(pos["qty"]))
            if not clamped:                       # None(미상)·0(외부매도) → 청산 계획 제외
                continue
            spec = instrument_spec(pos["symbol"])
            out.append(Intent(
                sid=sid, strategy_id=sid, strategy_name=pos.get("strategy_name", ""),
                contract_key=(pos.get("contract_code") or pos["symbol"]) if is_fut
                else pos["symbol"],
                symbol=pos["symbol"], kind="exit", position_side=pos_side,
                order_side=("sell" if pos_side == "long" else "buy"),
                qty=int(clamped), ref_price=float(ref_price),
                entry_price=float(pos["entry_price"]), mult=spec.multiplier,
                currency=spec.currency, definition=pos.get("definition") or {}))
        return out

    def _plan_exit_intents(self, window: str, dataset: dict, today: date,
                           market: str, instrument_class: str | None,
                           ks_active: bool, decisions: list[dict],
                           ) -> tuple[list, set[str], dict[str, str]]:
        """이번 창의 청산 의도 + 판정불가 심볼(§13 hold) 산출 — 발주·클램프 없음.

        목표수렴 설계(kr-target-reconciliation.md §13): 청산 규칙을 평가할 수 없는
        포지션(파싱 실패·참조가 부재)이나 청산 주문이 이미 in-flight인 심볼은
        **indeterminate**로 반환 — 그 심볼은 이번 사이클 수렴 자체를 건너뛴다(hold).
        "목표 없음 ≠ 목표 0": 판정불가를 0으로 취급하면 stale/에러가 전량청산을 유발한다.

        window="open"(아침): 구 §2와 동일 평가 — 신호·시간·만기 백스톱·ks 강제청산.
        window="close"(종가): 구 plan_close_liquidations와 동일 선별 — 당일매매
        (hold_days==0)·exit.fill=close 전략만, is_close=True 창 게이트.
        참조가: 아침=번들 전일봉(stale이면 live·A2), 종가=live 우선(현재가 정산).
        클램프 없음 — 수량은 원장 전량. 브로커 부족분은 net 계산이 흡수한다(§14).
        """
        from .netting import Intent
        out: list = []
        indeterminate: set[str] = set()
        reasons: dict[str, str] = {}
        today_iso = today.isoformat()
        for sid, pos in list(self.ledger.items()):
            symbol = pos["symbol"]
            if _market_group_safe(symbol) != market:
                continue
            is_fut = qc.is_futures(symbol)
            if instrument_class is not None and (instrument_class == "futures") != is_fut:
                continue
            if coverage.missing_categories([symbol]):
                decisions.append(order_log.decision(
                    "orphan_uncovered", sid, pos.get("strategy_name", ""), symbol,
                    "자격증명 미등록 자산군 — 청산 불가(수동 정리 필요)"))
                indeterminate.add(symbol)
                continue
            if window == "close":
                _exit = (((pos.get("definition") or {}).get("position") or {})
                         .get("exit") or {})
                if _exit.get("hold_days") != 0 and _exit.get("fill") != "close":
                    continue          # 종가창 소관 아님 — 유지분으로 target에 기여
            parse_failed = False
            try:
                held = _held_trading_days(dataset, pos["symbol"],
                                          pos["entry_date"], today)
                reason, _ = _exit_reason_for(
                    pos["definition"], held, dataset, symbol,
                    is_close=(window == "close"))
            except Exception as e:
                log.warning("원장 전략 파싱 실패 [%s]: %s", sid, e)
                reason = None
                parse_failed = True
            # kill switch 활성 시 모든 보유 강제 청산(파싱 실패 고아 포함) — 아침창.
            if window == "open" and ks_active and not reason:
                reason = "kill-switch"
            # M6 tier-2 만기 백스톱: 유저 규칙 미발동 + 선물 만기 임박 → 강제청산.
            if not reason and is_fut:
                reason = self._expiry_close_reason(pos, today)
            if not reason:
                if parse_failed:
                    # 청산 규칙 평가 불가(고아) — 임의 매도 금지·명시 표면화. 이 심볼은
                    # 목표 판정 불가이므로 수렴도 보류(§13 — 0으로 오인해 청산 금지).
                    decisions.append(order_log.decision(
                        "unparseable_orphan", sid, pos.get("strategy_name", ""),
                        symbol, "전략 정의 파싱 실패 — 자동 청산 불가(수동 정리 필요)"))
                    indeterminate.add(symbol)
                continue
            pos_side = pos.get("side", "long")
            exit_side = "sell" if pos_side == "long" else "buy"
            # in-flight 게이트(§14 멱등) — 이 포지션의 청산 intent가 이미 활성(오늘
            # 발주·미마감)이면 재계획하지 않는다. leg를 빼고 net 산술만 남기면 자기
            # 주문과 상쇄되는 워시 주문이 나가므로, 심볼 통째 hold가 유일하게 안전.
            if intents.is_active(today_iso, sid, symbol, exit_side):
                # 관측(2026-07-20): 종전엔 INFO 로그뿐이라 "왜 이 심볼만 계획에서
                # 빠졌나"가 원격에서 안 보였다(심볼 통째 hold = 진입까지 함께 보류).
                decisions.append(order_log.decision(
                    "skip_exit_inflight", str(sid), "", symbol,
                    f"청산 intent 활성({exit_side}) — 이 심볼 이번 사이클 보류"
                    "(자기주문 상쇄 워시 방지·다음 창 재평가)"))
                log.info("[목표수렴] 청산 in-flight [%s/%s] — 이번 사이클 hold", sid, symbol)
                indeterminate.add(symbol)
                continue
            # 참조가 = **정산가**(목표수렴 §3.2 — book leg 실현손익·진입가 기장에 쓰임).
            # 아침: 번들 전일봉(fresh 검증), stale이면 live로 대체(A2). 종가: live 우선.
            # ⚠ 구 A2는 "live 없으면 stale이라도 진행(fail-open)"이었다 — 구 모델의 ref는
            # 시장가 주문의 참고값이라 무해했지만, 목표수렴에선 ref가 정산가가 되므로
            # stale 봉(07-14 1210.5)으로 book하면 phantom이 재발한다. stale+무live=hold.
            ref_price = 0.0
            sdf = dataset.get(symbol)
            if window == "close":
                ref_price = self._safe_price(symbol) or 0.0
                if ref_price <= 0 and sdf is not None and len(sdf) \
                        and "Close" in sdf.columns \
                        and not _bar_is_stale(sdf, symbol, today):
                    ref_price = float(sdf["Close"].iloc[-1])
            else:
                if sdf is not None and len(sdf) > 0 and "Close" in sdf.columns:
                    try:
                        ref_price = float(sdf["Close"].iloc[-1])
                    except Exception:
                        ref_price = 0.0
                if _bar_is_stale(sdf, symbol, today):
                    ref_price = 0.0        # stale 봉은 정산가 금지(phantom 차단)
                if ref_price <= 0:
                    cur = self._safe_price(symbol)
                    if cur and cur > 0:
                        ref_price = cur
            if ref_price <= 0:
                # 정산가 없이 청산 leg을 만들면 phantom 손익(07-14)·오정산 — hold(§13).
                log.warning("청산 정산가 없음 [%s] — 이 심볼 수렴 보류(다음 사이클)",
                            symbol)
                indeterminate.add(symbol)
                continue
            spec = instrument_spec(symbol)
            reasons[sid] = reason
            out.append(Intent(
                sid=sid, strategy_id=sid, strategy_name=pos.get("strategy_name", ""),
                contract_key=(pos.get("contract_code") or symbol) if is_fut else symbol,
                symbol=symbol, kind="exit", position_side=pos_side,
                order_side=exit_side,
                qty=int(pos["qty"]), ref_price=float(ref_price),
                entry_price=float(pos["entry_price"]), mult=spec.multiplier,
                currency=spec.currency, definition=pos.get("definition") or {}))
        return out, indeterminate, reasons

    def _orderable_new_only(self, symbol: str) -> bool:
        """브로커 orderable(신규 주문가능수량)이 '신규 전용' 의미로 **확정**됐는가.

        리버설(반대편) 크레딧의 안전 게이트(§18.2·2026-07-18) — 브로커가 실측/문서로
        확정한 플래그(라우터 orderable_new_only)를 노출하면 그 값, 미노출(구 브로커·
        Mock 미선언)이면 False(같은-편 강등)."""
        fn = getattr(self.broker, "orderable_new_only", None)
        return bool(fn(symbol)) if fn is not None else False

    def _credit_key(self, symbol: str, side: str) -> tuple:
        """사이징 크레딧 풀 키 — **방향무관 (심볼, None)이 원칙**(§18.2 정정 2026-07-18).

        갈아타기(롱청산→숏진입)도 "청산분을 반영한 여력으로 진입"이 유저 모델이다
        (7/16의 '전략청산=같은편' 한정은 유저 의도 오해로 정정). 단 선물은 브로커
        orderable이 '신규 전용'일 때만 안전하다: 합산(신규+청산) 의미라면 반대편
        orderable에 보유 청산분이 이미 포함돼 있어 방향무관 크레딧이 이중계상(의도
        초과 레버리지)된다. LS는 신규 전용 확정(CFOAQ10100 NewOrdAbleQty — 문서
        38=36+2 분리+07-16 실측), KIS도 신규 전용 확정(TTTO5105R ord_psbl_qty —
        07-20 모의 실측: 무보유 9/9 → 롱1 보유 후 8/8, 합산이면 sell 9 유지였어야).
        여전히 미확정인 어댑터의 선물만 같은-편 키로 강등한다(같은-편 크레딧은
        어느 의미에서도 안전:
        롱 청산분은 매도 방향 청산가능수량이라 매수 orderable에 낄 수 없음). 주식은
        orderable이 아니라 현금(예수금) 크레딧이고 예수금 조회에 미체결 매도가 반영되지
        않음이 확정이라 이중계상 여지가 없다 → 항상 방향무관."""
        if qc.is_futures(symbol) and not self._orderable_new_only(symbol):
            return (symbol, side)
        return (symbol, None)

    def _stock_credit_price(self, symbol: str, dataset: dict | None) -> float:
        """주식 크레딧(청산·되돌림 회수 현금) 추정 기준가 — 실시간 우선(§18·2026-07-19).

        동시호가(사이징 시점)엔 가격이 미확정이라 종전엔 전일 종가로 추정했다 — KRX가
        동시호가 내내 제공하는 **예상체결가**가 더 정확한 추정치(유저 확정). 폴백 사슬:
        ① 예상체결가(브로커 seam — KIS FHKST01010200·연속거래면 현재가 반환)
        ② 현재가(_safe_price) ③ 번들 전일 종가. 사이징 보조 추정이라 조회 실패가
        계획을 막으면 안 됨 — 실패는 다음 폴백으로(전부 실패 시 0.0 = 호출자 보수 처리).
        """
        fn = getattr(self.broker, "expected_fill_price", None)
        if fn is not None:
            try:
                px = float(fn(symbol) or 0)
                if px > 0:
                    return px
            except Exception as e:                # noqa: BLE001 — 보조 조회·폴백 전제
                log.debug("예상체결가 조회 실패 [%s] — 현재가 폴백: %s", symbol, e)
        px = self._safe_price(symbol) or 0.0
        if px > 0:
            return px
        sdf = (dataset or {}).get(symbol)
        if sdf is not None and len(sdf) and "Close" in sdf.columns:
            try:
                return float(sdf["Close"].iloc[-1])
            except Exception:
                return 0.0
        return 0.0

    def _freed_capacity(self, liq_intents: list, dataset: dict | None = None) -> dict:
        """청산 의도가 회수할 여력을 크레딧 풀로 합산 — 진입 사이징 크레딧(E1).

        capacity_credit는 물리 주문 기계가 아니라 **사이징 입력**이다(목표수렴 하에서도
        유지 — 같은 사이클 롤에서 청산이 풀어줄 자본을 진입 사이징이 봐야 autotrade-only
        의도 크기가 나온다). 선물=계약수(orderable에 더함)·주식=현금(수량×기준가 —
        기준가는 _stock_credit_price 실시간 우선, 조회 전패 시 청산 의도의 ref_price).
        호출자(_reconcile_pass)가 브로커 실보유로 클램프한 청산량만 넘겨 유령 여력을
        만들지 않는다(N7 — 수동 선매도분은 이미 브로커 현금/orderable에 반영돼 있음).
        키잉은 _credit_key(방향무관 원칙 + 미확정 브로커 선물만 같은-편 강등)."""
        cap: dict = {}
        for lg in liq_intents:
            if qc.is_futures(lg.symbol):
                add = lg.qty
            else:
                px = self._stock_credit_price(lg.symbol, dataset) or lg.ref_price
                add = lg.qty * px
            k = self._credit_key(lg.symbol, lg.position_side)
            cap[k] = cap.get(k, 0) + add
        return cap

    def plan_entries_captured(self, by_strategy: list, strategies: list, dataset: dict,
                              equity_now: float, liq_intents: list, market: str,
                              instrument_class: str | None,
                              entry_window: str = "close",
                              catchup: bool = False,
                              extra_capacity: dict | None = None) -> tuple[list, list]:
        """진입 '의도'를 산출(발주 안 함) — 수렴 PLAN. 종가창(close)·시가창(open) 공용.

        _enter_from_preview를 capture 모드로 재사용 — fill 라우팅·계좌·커버리지·보유·멱등
        게이트와 사이징(현재 자본·orderable + 보유 되돌림 크레딧)을 실주문과 동일하게 통과.
        사이징은 진입 시점 1회 확정·보유 중 고정(목표수렴 §9).
        extra_capacity(§18): 계획 청산 외 **원장 밖 브로커 보유**의 되돌림 크레딧
        {(symbol,side):amount} — 계획청산 크레딧과 합쳐 "빈 상태 최대" 사이징. 반환 (entry_intents, decisions).
        """
        capacity = self._freed_capacity(liq_intents, dataset)
        # §18.2(2026-07-18 정정): 크레딧은 원칙 **방향무관** — 계획 청산·원장 밖(수동/외부)
        # 보유 모두 "전부 되돌린 빈-상태 잔고" 기준으로 진입 방향과 무관하게 크레딧한다.
        # 키잉은 _credit_key 단일 규칙: 선물인데 브로커 orderable의 '신규 전용' 여부가
        # 미확정이면 같은-편 키로 강등해 반대편 이중계상
        # (합산 의미일 때 orderable에 청산분이 이미 포함)만 막는다. 두 풀은 credit_for가
        # 합산·consume_credit이 순차 소진.
        for (sym, _side), v in (extra_capacity or {}).items():
            _fk = self._credit_key(sym, _side)
            capacity[_fk] = capacity.get(_fk, 0) + v
        captured: list = []
        decisions: list = []
        self._enter_from_preview(by_strategy, strategies, dataset, equity_now,
                                 decisions, set(), market=market,
                                 entry_window=entry_window, catchup=catchup,
                                 instrument_class=instrument_class,
                                 capacity_credit=capacity, capture=captured)
        return captured, decisions

    def _submit_drift(self, symbol: str, drift_qty: int, window: str,
                      decisions: list[dict]) -> None:
        """목표수렴 drift 교정 주문 — 원장 불변 물리 교정(설계 §2·§9③).

        수동매매 되돌림(원장>브로커=재매수 복원, 원장<브로커=초과분 매도)과 비전략
        심볼 청산(target 0)을 담당한다. 체결돼도 원장을 바꾸지 않는다(pending의
        drift=True → _apply_fill 초입 분기) — 원장=전략 의도는 leg booking이 이미
        반영했고, 이 주문은 브로커 실보유를 목표로 옮기는 물리 이동만 한다.
        가격: live 현재가 기준(수동 개입 후라 번들 전일가는 참조 부적절). 조회 불가면
        다음 사이클로 보류(fail-closed — 목표수렴은 매 사이클 재계산이라 자기치유).
        """
        side = "buy" if drift_qty > 0 else "sell"
        qty = abs(int(drift_qty))
        ref = self._safe_price(symbol) or 0.0
        if ref <= 0:
            decisions.append(order_log.decision(
                "drift_deferred", "", "", symbol,
                f"목표수렴 교정 {side} {qty} — 현재가 조회 불가, 다음 사이클 보류"))
            return
        today_iso = kst_today().isoformat()
        dkey = f"DRIFT:{window}"
        # §14 멱등 — 같은 창·심볼·방향의 교정이 오늘 이미 발주됐으면 재발주 금지.
        if intents.is_active(today_iso, dkey, symbol, side):
            decisions.append(order_log.decision(
                "skip_idempotent", dkey, "목표수렴", symbol,
                f"오늘 이미 발주된 교정({side}) intent 존재 — 중복 차단"))
            log.info("[L-01] 중복 drift 교정 차단 %s %s", symbol, side)
            return
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, dkey, "목표수렴", symbol, side, qty, ref)
        policy = merged_execution(None)
        limit = 0
        try:
            if side == "sell":
                if self._is_reserved_us(symbol):
                    limit = self._us_limit(symbol, "sell", policy, ref)
                    r = self.broker.sell_resv_limit(symbol, qty, limit)
                elif _currency_of(symbol) == "USD":
                    limit = self._us_limit(symbol, "sell", policy, ref)
                    r = self.broker.sell_limit(symbol, qty, limit)
                else:
                    r = self.broker.sell(symbol, qty)
            else:
                if self._is_reserved_us(symbol):
                    limit = self._us_limit(symbol, "buy", policy, ref)
                    r = self.broker.buy_resv_limit(symbol, qty, limit)
                elif _currency_of(symbol) == "USD":
                    limit = self._us_limit(symbol, "buy", policy, ref)
                    r = self.broker.buy_limit(symbol, qty, limit)
                else:
                    r = self.broker.buy(symbol, qty)
        except Exception as e:
            # 문제12 verify-then-retry: 예외=ambiguous — mark_failed 하지 않고
            # 'submitting'으로 남긴다(타임아웃은 접수됐을 수 있음). 다음 사이클 시작의
            # reconcile_submitting이 KIS 당일주문 조회로 submitted/failed 판정 →
            # 미접수면 재시도(창내 재실행 08:40/42 포함)·접수면 이중발주 차단.
            log.error("목표수렴 교정 발주 실패 [%s %s %d]: %s", symbol, side, qty, e)
            decisions.append(order_log.decision(
                "error", dkey, "목표수렴", symbol, f"교정 발주 예외: {e}"))
            return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, dkey, "목표수렴", None, symbol, side, qty, ref, limit,
                           policy, decisions, reason="목표수렴 drift 교정",
                           today_iso=today_iso, intent_id=intent_id, kind="교정",
                           drift=True)

    def _reconcile_pass(self, *, window: str, snap_pre: dict, dataset: dict,
                        today: date, market: str, instrument_class: str | None,
                        buy_candidates: list | None, strategies: list,
                        equity_now: float, entries_blocked: bool, ks_active: bool,
                        decisions: list[dict], catchup: bool = False,
                        ) -> tuple[int, float, int]:
        """목표상태 수렴 — 청산/진입 의도 → 심볼별 net 계획 → 합성 정산·발주.

        kr-target-reconciliation.md §2(모델)·§13(목표 없음≠0)·§14(멱등·가용성 가드).
        구 넷팅 pre-pass + §2 잔여청산 + §3 진입을 단일 패스로 대체한다.
        반환 (n_netted, commission_saved, n_drift) — cycle_summary 집계용.
        """
        from . import target_recon
        from .analytics import norm_side

        exit_intents, indeterminate, exit_reasons = self._plan_exit_intents(
            window, dataset, today, market, instrument_class, ks_active, decisions)

        def _in_scope(sym: str) -> bool:
            if _market_group_safe(sym) != market:
                return False
            if instrument_class is not None:
                return (instrument_class == "futures") == qc.is_futures(sym)
            return True

        # §19 A1 — 동시호가 창에 유저가 발주한 외부(수동) 미체결을 effective current로 반영
        # (같은 방향은 08:35/15:40 넷팅·사이징에, 반대/초과는 방향무관 08:52 수렴이 처리).
        # 자기 주문은 order_no(intents 저널)로 제외해 재시도 워시(§17.1 기각)를 막는다 —
        # 08:40/42 재시도에서 자기 08:35 미체결을 상쇄하는 매도가 나가지 않게. 키=계약코드
        # (선물)·심볼(주식)로 pending 심볼이 곧 positions/ledger와 동일 규약. 스코프는 dict
        # 필드(market·asset_class)로 판정(심볼 파싱 대신). 미지원 브로커·조회 실패는 안전
        # skip(08:52 수렴이 백업 — A1은 최적화, 수렴이 정합 보장).
        ext_signed: dict = {}
        ext_remain: dict = {}
        self._a1_trace = {}          # 이번 수렴 패스의 A1 결과만(관측 전용)
        _pend_fn = getattr(self.broker, "pending_orders", None)
        if _pend_fn is not None:
            try:
                _own = {str(e.get("order_no")) for e in
                        intents._read_today(today.isoformat()) if e.get("order_no")}

                def _pend_scope(pnd: dict) -> bool:
                    _mg = ("KRX" if str(pnd.get("market") or "") in ("DOMESTIC", "KRX")
                           else "US")
                    if _mg != market:
                        return False
                    if instrument_class is not None:
                        return str(pnd.get("asset_class") or "") == instrument_class
                    return True

                ext_signed, ext_remain = target_recon.external_pending_by_key(
                    _pend_fn() or [], _own, _pend_scope, lambda s: s)
                if ext_signed:
                    log.info("[A1] 외부(수동) 미체결 반영: %s", ext_signed)
                    # 관측(2026-07-20): 종전엔 이 INFO 로그가 유일한 흔적이라 원격
                    # (서버 스냅샷)에서 "외부 미체결을 얼마나 인수했는지"를 볼 수 없었다.
                    # 수동 개입 진단의 핵심 입력이므로 결정으로 승격한다(계약키·부호수량만
                    # — 자격증명·계좌번호 없음). 부호: +매수 / −매도.
                    self._a1_trace = {str(k): int(v) for k, v in ext_signed.items()}
                    decisions.append(order_log.decision(
                        "external_pending", "", "", "",
                        "외부(수동) 미체결 인수: "
                        + ", ".join(f"{k} {v:+d}" for k, v in sorted(self._a1_trace.items()))
                        + " — 목표 계산에 선반영(넷팅·사이징 크레딧)"))
            except Exception as e:
                log.warning("[A1] 외부 미체결 조회 실패 — pending 넷팅 skip"
                            "(08:52 수렴 백업): %s", e)
                ext_signed, ext_remain = {}, {}
                # 조회 실패는 **동작이 바뀌는** 사건(A1 skip → 창내 인수 없음 → 개장후
                # 수렴이 백업). 종전엔 WARNING 로그뿐이라 원격에서 안 보였다.
                self._a1_trace = {"__error__": repr(e)[:200]}
                decisions.append(order_log.decision(
                    "external_pending_unavailable", "", "", "",
                    f"외부 미체결 조회 실패 — A1 인수 skip(개장후 수렴이 백업): {e}"))

        entry_intents: list = []
        if not entries_blocked and buy_candidates is not None:
            # §18 빈-상태 사이징: 진입 심볼의 **브로커 실보유 전체(side별)를 되돌림 크레딧**으로
            # 삼아 target을 "수동 취소 후 빈 잔고 기준 최대"로 만든다(유저 원칙 Q-1-1-b).
            # _avail을 in-scope 브로커 보유로 초기화 → 계획 청산이 소비 → 잔여 = 원장 밖 보유.
            # 계획청산 크레딧(credit_intents)과 원장밖 크레딧(extra)은 서로 배타(합=총보유·비중복).
            # 브로커 실보유 기준이라 유령 여력 불가(N7). side별이라 부호뒤집기 오적용 방지(§16.2).
            from dataclasses import replace as _dc_replace
            credit_intents: list = []
            _avail: dict[tuple[str, str], int] = {}
            for p in (snap_pre.get("positions") or []):
                sym = p.get("symbol", "")
                if not sym or p.get("symbol_unmapped") or not _in_scope(sym):
                    continue
                k = (sym, norm_side(p.get("side")))
                _avail[k] = _avail.get(k, 0) + max(0, int(p.get("qty") or 0))
            for it in exit_intents:
                k = (it.symbol, it.position_side)
                avail = _avail.get(k, 0)
                take = min(int(it.qty), avail)
                if take > 0:
                    _avail[k] = avail - take
                    credit_intents.append(
                        it if take == it.qty else _dc_replace(it, qty=take))
            extra_capacity: dict = {}
            for (sym, side), remaining in _avail.items():
                if remaining <= 0:
                    continue
                # 관측(2026-07-20) — 계획 청산으로 소비되고 남은 이 잔량이 곧 **원장 밖
                # 브로커 보유**(수동/외부 체결분)다. 엔진은 이미 이걸 알고 사이징 크레딧에
                # 넣고 목표에 흡수하는데, 아무 기록도 남기지 않아 mwmw 07-20 조사에서
                # 넷팅 leg로 역산해야 했다. 여기서 명시한다(사이징·발주에 영향 없음).
                decisions.append(order_log.decision(
                    "absorbed_external", "", "", sym,
                    f"원장 밖 브로커 보유 {side} {int(remaining)} — 전략 목표에 흡수"
                    "(수동/외부 체결 추정 · 사이징 되돌림 크레딧 포함)"))
                log.warning("[흡수] 원장 밖 보유 %s %s %d계약 — 목표에 편입(외부 체결 추정)",
                            sym, side, int(remaining))
                if qc.is_futures(sym):
                    extra_capacity[(sym, side)] = remaining
                else:
                    # §18 주식 되돌림 현금 추정 — 실시간(예상체결가→현재가) 우선, 번들 폴백.
                    px = self._stock_credit_price(sym, dataset)
                    if px > 0:
                        extra_capacity[(sym, side)] = remaining * px
            # §19 A1 사이징 크레딧 — 외부 pending의 예약 여력을 "빈 상태"로 되돌림(§18과 동일
            # 목적: target을 수동 취소 후 최대로). ext_remain 키=(계약코드,side)를 진입 표시
            # 심볼로 역매핑해야 extra_capacity(표시심볼 키)와 맞는다 — positions·후보를
            # _resolve_contract_key로 코드↔표시 매핑(주식은 코드=심볼이라 무이슈). 역매핑 없는
            # 신선 계약은 표시심볼 미상 → skip(08:52 수렴이 top-up).
            if ext_remain:
                _code2disp: dict = {}
                for _p in (snap_pre.get("positions") or []):
                    _s = _p.get("symbol") or ""
                    if _s:
                        _code2disp[self._resolve_contract_key(_s)] = _s
                for _e in (buy_candidates or []):
                    for _c in (_e.get("candidates") or []):
                        _s = _c.get("symbol") or ""
                        if _s:
                            _code2disp.setdefault(self._resolve_contract_key(_s), _s)
                for (code, side), rem in ext_remain.items():
                    disp = _code2disp.get(code)
                    if disp is None or not _in_scope(disp):
                        continue
                    if qc.is_futures(disp):
                        extra_capacity[(disp, side)] = \
                            extra_capacity.get((disp, side), 0) + rem
                    else:
                        # C2(2026-07-19 감사) — 주식 '매도' 미체결 잔량은 크레딧 금지:
                        # 잔고 hldg_qty가 매도 미체결 중에도 전량 유지라 같은 주식
                        # 가치가 위 §18 보유 크레딧(_avail)에 이미 계상돼 있다(취소
                        # 시 늘어나는 현금 없음 — 이중계상). '매수' 미체결(side long)
                        # 은 예약 현금의 환급이라 중복 없음 — 유지.
                        if side == "short":
                            continue
                        px = self._stock_credit_price(disp, dataset)
                        if px > 0:
                            extra_capacity[(disp, side)] = \
                                extra_capacity.get((disp, side), 0) + rem * px
            entry_intents, ent_dec = self.plan_entries_captured(
                buy_candidates, strategies, dataset, equity_now, credit_intents,
                market, instrument_class, entry_window=window, catchup=catchup,
                extra_capacity=extra_capacity)
            decisions.extend(ent_dec)
            # 진입 in-flight(오늘 이미 발주·L-01 skip_idempotent) 심볼도 hold —
            # leg 없는 net 산술이 자기 주문과 상쇄되는 것 방지(§14).
            for d in ent_dec:
                if d.get("action") == "skip_idempotent" and d.get("symbol"):
                    indeterminate.add(d["symbol"])
        # §13 — 판정불가 심볼의 진입 leg 제거(목표 미정 심볼에 순주문 금지).
        if indeterminate:
            kept = []
            for it in entry_intents:
                if it.symbol in indeterminate:
                    decisions.append(order_log.decision(
                        "skip_indeterminate", it.sid, it.strategy_name, it.symbol,
                        "심볼 목표 판정 불가 — 이번 사이클 수렴 보류(hold)"))
                else:
                    kept.append(it)
            entry_intents = kept

        # 계획 키 = 물리 계약 식별자(E6 롤 경계): 선물=contract_code-or-심볼, 주식=심볼.
        # 심볼 단위로 net을 계산하면 롤 주간에 구계약 청산 + 신계약 진입이 같은 상품명으로
        # 오상계돼 물리 롤이 실행되지 않는다 — 반드시 키 단위(exit/entry Intent의
        # contract_key 규약과 동일). symbol_of는 발주 라우팅용 역매핑.
        def _plan_key(sym: str, code: str | None) -> str:
            return (str(code) if code else sym) if qc.is_futures(sym) else sym

        symbol_of: dict[str, str] = {}
        ledger_signed: dict[str, int] = {}
        for pos in self.ledger.values():
            sym = pos["symbol"]
            if not _in_scope(sym):
                continue
            key = _plan_key(sym, pos.get("contract_code"))
            symbol_of.setdefault(key, sym)
            q = int(pos["qty"])
            ledger_signed[key] = ledger_signed.get(key, 0) + (
                -q if pos.get("side", "long") == "short" else q)

        balance_fetch_failed = list(
            (snap_pre.get("balance") or {}).get("fetch_failed") or [])
        if balance_fetch_failed:
            # §14 가용성 가드 — 부분 스냅샷에선 "브로커가 실제로 뭘 들고 있나"를 모른다.
            # 보유가 얽힌 심볼(원장·브로커 조회분)은 전부 hold: 청산을 원장 전량으로 내면
            # 실보유 부족 시 오버셀(의도외 숏), drift 교정은 멀쩡한 보유를 외부분으로 오인
            # 청산할 수 있다(구 모델의 clamp-0-skip과 동일하게 보수적). 보유 무관한 순수
            # 신규 진입만 진행(가용성 — 구 모델도 부분조회 시 진입은 계속했다).
            broker_signed = {}
            held_syms = set(ledger_signed) | {
                p.get("symbol", "") for p in (snap_pre.get("positions") or [])
                if p.get("symbol") and _in_scope(p.get("symbol", ""))}
            indeterminate |= held_syms
            decisions.append(order_log.decision(
                "drift_eval_skipped", "", "", "",
                f"잔고 부분조회 {balance_fetch_failed} — 보유 심볼 수렴 보류"
                f"(신규 진입만 진행): {sorted(held_syms)[:10]}"))
        else:
            broker_signed = {}
            for p in (snap_pre.get("positions") or []):
                sym = p.get("symbol", "")
                if not sym or not _in_scope(sym):
                    continue
                if p.get("symbol_unmapped"):
                    # 정규화 실패 보유(I2) — 어떤 심볼인지 확정 불가. 수렴 보류·표면화.
                    decisions.append(order_log.decision(
                        "drift_eval_skipped", "", "", sym,
                        "브로커 심볼 정규화 실패 — 이 심볼 수렴 보류(수동 점검)"))
                    indeterminate.add(sym)
                    continue
                key = _plan_key(sym, p.get("contract_code"))
                symbol_of.setdefault(key, sym)
                q = int(p.get("qty") or 0)
                broker_signed[key] = broker_signed.get(key, 0) + (
                    -q if norm_side(p.get("side")) == "short" else q)

        # indeterminate는 심볼 단위로 수집됐다 — 그 심볼의 모든 계약 키를 제외(hold).
        # leg도 심볼로 재필터: 늦게 추가된 판정불가(fetch_failed·unmapped)의 진입 leg
        # 키(최근월물 코드)가 symbol_of(원장·브로커 유래)에 없으면 키 제외를 빠져나가는
        # 구멍 봉합(§13 — 판정불가 심볼에 어떤 순주문도 금지).
        excluded_keys = set(indeterminate) | {
            k for k, s in symbol_of.items() if s in indeterminate}
        plan_intents = [it for it in exit_intents + entry_intents
                        if it.symbol not in indeterminate]

        plans = target_recon.build_symbol_plans(
            plan_intents, ledger_signed, broker_signed,
            excluded_keys, symbol_of=symbol_of, external_pending=ext_signed)

        # 목표를 확정한 심볼 = plan이 만들어진 심볼. indeterminate(§13 판정 불가)는
        # build_symbol_plans가 이미 제외하므로 "목표 0"과 "목표 없음"이 여기서 갈린다.
        # 가드가 이 목록을 자기 우주에 더해 원장 행이 없는 목표도 감시한다.
        self._target_syms = sorted({p.symbol for p in plans})

        n_netted = 0
        commission_saved = 0.0
        n_drift = 0
        for plan in plans:      # 이미 매도(net≤0) 먼저 정렬 — 증거금 선회수 불변식
            for leg in plan.book_legs:
                self._apply_netted_leg(leg, decisions,
                                       reason=exit_reasons.get(leg.sid))
            commission_saved += self._netting_commission_saved(plan.book_legs)
            n_netted += plan.offset_qty
            for leg in plan.order_legs:
                self._submit_residual(leg, decisions,
                                      reason=exit_reasons.get(leg.sid),
                                      catchup=catchup)
            if plan.drift_qty:
                # 비-최근월물 가드: drift 주문은 심볼로 라우팅돼 브로커가 최근월물에
                # 체결한다. 계획 키가 다른(구·원월) 계약이면 잘못된 계약을 사고팔게
                # 되므로 발주하지 않고 표면화만(수동 정리·만기 백스톱 소관).
                if (qc.is_futures(plan.symbol) and plan.key != plan.symbol
                        and plan.key != self._resolve_contract_key(plan.symbol)):
                    decisions.append(order_log.decision(
                        "drift_deferred", "", "", plan.symbol,
                        f"비-최근월물({plan.key}) drift {plan.drift_qty:+d} — "
                        "심볼 라우팅 불가, 발주 보류(수동 점검/만기 백스톱)"))
                    continue
                n_drift += abs(plan.drift_qty)
                self._submit_drift(plan.symbol, plan.drift_qty, window, decisions)
        return n_netted, commission_saved, n_drift

    def _netting_commission_saved(self, book_legs: list) -> float:
        """넷팅으로 절감된 수수료(KRW·확정분) — 각 합성 체결 leg이 회피한 편도 수수료 합.

        청산·진입 leg이 모두 book_legs에 있어 왕복 양쪽 수수료가 자동 합산된다(설계 §7).
        매도세(bt_sell_tax_bps) 등은 보수적으로 제외 — 실제 절감은 이 값보다 크다."""
        rate = (merged_execution(None).get("bt_commission_bps", 0) or 0) / 10_000.0
        return sum(leg.qty * leg.ref_price * leg.mult * rate for leg in book_legs)

    def _submit_residual(self, it, decisions: list[dict],
                         reason: str | None = None, catchup: bool = False) -> None:
        """수렴 계획 leg 실주문 1건 — Intent를 기존 _submit_* 로 발주(청산/진입·롱/숏 분기).

        게이트·사이징은 PLAN에서 이미 통과했다. 각 _submit_*가 자체 멱등 게이트(is_active)를
        가지며, 합성 정산 leg은 시드를 안 남기므로 이 잔여가 막히지 않는다.
        reason: 청산 사유(§2 평가 결과 — 손절/보유기간/kill-switch 등) 전파. 미지정 시 종전
        문자열("당일청산") 유지. catchup: 진입 매수의 시장가→시초가 limit 변환(기존 §3 거동)."""
        policy = _policy(it.definition)
        if it.kind == "exit":
            if it.position_side == "short":
                self._submit_close_short(it.sid, it.strategy_name, it.symbol, it.qty,
                                         it.ref_price, policy, reason or "당일청산",
                                         decisions)
            else:
                self._submit_sell(it.sid, it.strategy_name, it.symbol, it.qty,
                                  it.ref_price, policy, reason or "당일청산", decisions)
        else:
            if it.position_side == "short":
                self._submit_open_short(it.sid, it.strategy_name, it.definition,
                                        it.symbol, it.qty, it.ref_price, policy, decisions)
            else:
                self._submit_buy(it.sid, it.strategy_name, it.definition, it.symbol,
                                  it.qty, it.ref_price, policy, decisions,
                                  catchup=catchup)

    def _close_entry_blocked(self, equity_now: float, snap_pre: dict,
                             risk_limits: dict | None, decisions: list[dict]) -> bool:
        """종가 진입 킬스위치·drawdown 게이트(아침 §1/§3·구 _enter_close_body 미러) — 막히면 True.

        청산은 무관(항상 진행·E5). 잔고 부분조회 실패면 위험평가 보류만 표면화하고 진입은 계속
        (가용성 우선·보수 사이징). 일일 손실 한도는 여기서 재평가(장중 loop 종료 후 종가창 손실 포착)."""
        rl = risk_limits or {}
        balance_fetch_failed = list((snap_pre.get("balance") or {}).get("fetch_failed") or [])
        if balance_fetch_failed:
            decisions.append(order_log.decision(
                "risk_eval_skipped", "", "", "",
                f"잔고 부분조회 실패 {balance_fetch_failed} — 손실한도·drawdown 평가 보류"))
        global_policy = merged_execution(None)
        _dll = rl.get("kill_switch_daily_loss_pct")
        self._daily_loss_limit_pct = float(_dll) if _dll is not None else None
        ks_state = killswitch.load()
        ks_active = bool(ks_state.get("active"))
        if (not balance_fetch_failed and not ks_active
                and self._daily_loss_limit_pct is not None):
            reason = killswitch.check_daily_loss(equity_now, self._daily_loss_limit_pct)
            if reason:
                killswitch.activate(reason)
                ks_active = True
                ks_state = killswitch.load()
        _user_dd = rl.get("max_drawdown_pct")
        max_dd = _user_dd if _user_dd is not None else global_policy.get("max_drawdown_pct")
        drawdown_active = False
        if max_dd is not None and not balance_fetch_failed and equity_now > 0:
            peak = equity_now
            for e in self.equity:
                v = float(e.get("value") or 0)
                if v > peak:
                    peak = v
            if peak > 0 and (equity_now - peak) / peak * 100 <= -abs(float(max_dd)):
                drawdown_active = True
        if ks_active:
            decisions.append(order_log.decision(
                "skip_killswitch", "", "", "",
                f"종가 진입 차단 — {ks_state.get('reason', '')}"))
            return True
        if drawdown_active:
            decisions.append(order_log.decision(
                "skip_drawdown", "", "", "", "종가 진입 차단 — 누적 drawdown 한도 도달"))
            return True
        return False

    def run_close_netting(self, by_strategy: list, strategies: list, dataset: dict, *,
                          market: str = "KRX", instrument_class: str = "stock",
                          risk_limits: dict | None = None,
                          record_cycle: bool = True) -> dict:
        """종가창 수렴 사이클 — 목표상태 수렴(kr-target-reconciliation.md §2).

        아침 _cycle_body와 동일한 _reconcile_pass를 종가창(window="close")으로 실행한다:
        당일매매(hold_days==0)·exit.fill=close 청산과 종가매수 진입을 심볼별 target으로
        합산해 순발주. 같은 심볼의 청산↔진입은 합성 정산(수수료 0), 수동매매 drift는
        자동 흡수. 전체를 _CYCLE_LOCK 임계구역에서 처리(N6)."""
        decisions: list = []
        today = kst_today()
        n_netted = 0
        commission_saved = 0.0
        n_drift = 0
        with _CYCLE_LOCK:
            self._in_cycle = True
            self._krx_status = {}
            self._reserved_us = False
            try:
                self._resolve_pending(decisions)
                snap_pre = self.broker.account_snapshot()
                try:
                    equity_now = _unified_equity_krw(snap_pre.get("balance") or {})
                except Exception as e:
                    log.error("[종가수렴] 잔고 조회 실패 — 진입 사이징 보수(0): %s", e)
                    equity_now = 0.0
                # 리스크 한도 인스턴스 상태 — 아침 진입과 동일(_try_buy_one_symbol 참조).
                rl = risk_limits or {}
                self._us_bp_mode = rl.get("us_buying_power_mode") or "integrated"
                self._daily_turnover_limit_krw = int(rl.get("daily_turnover_limit_krw") or 0)
                self._daily_trade_count_limit = int(rl.get("daily_trade_count_limit") or 0)
                # 킬스위치·drawdown 게이트 — 막히면 진입만 skip(청산·drift 교정은 계속·E5)
                entries_blocked = self._close_entry_blocked(
                    equity_now, snap_pre, risk_limits, decisions)
                n_netted, commission_saved, n_drift = self._reconcile_pass(
                    window="close", snap_pre=snap_pre, dataset=dataset, today=today,
                    market=market, instrument_class=instrument_class,
                    buy_candidates=(by_strategy or []), strategies=strategies,
                    equity_now=equity_now, entries_blocked=entries_blocked,
                    ks_active=False, decisions=decisions)
                self._resolve_pending(decisions)
            finally:
                self._in_cycle = False
                self._reserved_us = False
        # 종가 단일가 체결 지연 흡수 — 아침 cycle과 동일 wait
        gp = merged_execution(None)
        self._wait_pending(gp["post_submit_wait_sec"], gp["poll_interval_sec"], decisions)
        n_bought = sum(1 for d in decisions
                       if d["action"] == "bought" and not d.get("netted"))
        return self._state_payload(
            decisions, today, kind="day_trade_close", market=market,
            record_cycle=record_cycle,
            extra_summary={"instrument_class": instrument_class,
                           "n_netted": int(n_netted),
                           "commission_saved_krw": round(commission_saved, 2),
                           "n_drift": int(n_drift),
                           # 가드 우주용 — 아침 요약과 같은 계약(위 cycle_summary 주석).
                           # 마감창은 장 종료로 창밖 교정 기회가 없어 특히 중요하다.
                           "target_symbols": list(self._target_syms),
                           "n_bought": n_bought})

    def liquidate_day_trades(self, dataset: dict, instrument_class: str, *,
                             market: str = "KRX", record_cycle: bool = True) -> dict:
        """당일매매(hold_days==0) 종가 청산 사이클 — 일반 cycle과 독립·additive (Stage B).

        당일매매는 백테스트에서 진입 바 종가에 청산한다. 라이브는 사이클이 아침·종가로
        나뉘므로 종가 단일가 발주창(주식 15:20~15:30·선물 15:35~15:45)에 이 사이클을 돌려
        당일매매 포지션만 종가 기준 청산 → backtest=live를 맞춘다(아침 cycle은 is_close=False라
        당일매매를 건드리지 않는다 — cycle_exit_reason Stage B 분기).

        instrument_class ∈ {"stock","futures"} — 주식/선물 종가 발주창이 다르므로(스케줄러가
        분리 cron으로 호출) 종목 클래스로 라우팅한다. 국내는 시장가 단일(종가 단일가 체결),
        미국주식은 라이브 지정가(_submit_sell USD 분기, 신선한가×(1−tol) 시장가근사 — KIS 연속장
        시장가 미지원). 청산 수량은 main loop와 동일하게 KIS 실보유로 클램프(L-04, snap_pre).
        파싱 실패 고아는 hold_days 불명 → skip(main loop·Monitor가 표면화).

        θ(2026-06-12 선물 0000004525 미기록): 발주 후 일반 cycle과 동일하게
        _wait_pending으로 폴링 — 단일 조회는 모의 ~27초 체결 지연도 놓쳤다.
        N1(미장 GOOG 261주 방치): 순회 전에 _resolve_pending을 먼저 돌려 미기록
        진입 체결(δ류)을 ledger에 복원한다(settlement와 동일한 resolve→평가 순서).
        그래도 체결확인 불능인 당일매매 진입은 추측 발주 없이 '청산 불능'으로
        표면화한다 — 계좌 보유가 외부 수동 매수일 수 있어(병1 불변식) 발주는 금지.
        """
        decisions: list[dict] = []
        today = kst_today()
        # N1 — ledger 순회 전에 미체결 먼저 정합. 진입 체결이 미기록이면 여기서
        # ledger에 복원돼(해외 체결감지 WS-1 포함) 아래 루프가 정상 청산한다.
        self._resolve_pending(decisions)
        snap_pre = self.broker.account_snapshot()   # KIS 실보유(clamp 기준) — main loop와 동일
        for sid, pos in list(self.ledger.items()):
            if _market_group_safe(pos["symbol"]) != market:
                continue
            # instrument_class 라우팅: 선물 vs 주식
            is_fut = qc.is_futures(pos["symbol"])
            if instrument_class == "futures" and not is_fut:
                continue
            if instrument_class == "stock" and is_fut:
                continue
            # 당일매매(hold_days==0)만 — 정의에서 직접 읽음(파싱 실패 고아는 hold_days 불명 → skip)
            hold_days = (((pos.get("definition") or {}).get("position") or {})
                         .get("exit") or {}).get("hold_days")
            # exit.fill=close(재설계 D3): 보유기간 만기 청산을 종가창에서 체결. due 여부는
            # 아래 _exit_reason_for(is_close=True)의 창 게이트가 판정(미도달=None→skip).
            _ef = (((pos.get("definition") or {}).get("position") or {})
                   .get("exit") or {}).get("fill")
            if hold_days != 0 and _ef != "close":
                continue
            try:
                held = _held_trading_days(dataset, pos["symbol"],
                                          pos["entry_date"], today)
                reason, _ = _exit_reason_for(
                    pos["definition"], held, dataset, pos["symbol"], is_close=True)
            except Exception as e:
                # 파싱·시세 부재 → skip(고아는 main loop·monitor가 처리)
                log.warning("종가청산 정의 파싱 실패 [%s]: %s", sid, e)
                continue
            if not reason:
                # 방어적 — is_close=True면 "당일청산"이 떠야 정상. None이면 청산 보류.
                continue
            # ref_price = 현재가(종가 무렵). 종가 매도는 전일종가가 아니라 현재가 기준.
            ref_price = self._safe_price(pos["symbol"]) or 0.0
            if ref_price <= 0:
                sdf = dataset.get(pos["symbol"])
                if sdf is not None and len(sdf) and "Close" in sdf.columns:
                    ref_price = float(sdf["Close"].iloc[-1])
            if ref_price <= 0:
                # R-5(2026-07-10 리뷰) — 조용한 청산 보류 금지: error decision으로 웹/타임라인 표면화.
                log.error("[종가청산] 참조가(현재가·전일종가) 없음 [%s] — 청산 보류", pos["symbol"])
                decisions.append(order_log.decision(
                    "error", str(sid), pos.get("strategy_name", ""), pos["symbol"],
                    "청산 참조가 없음(현재가·전일종가 조회 실패) — 청산 보류, 다음 창에서 재시도"))
                continue
            policy = _policy(pos.get("definition"))   # 국내=시장가 / 미국=is_resv
            sell_qty = int(pos["qty"])
            # L-04: 발주 직전 KIS 실보유로 클램프 — 외부 수동매도 over-sell 방지(main loop와 동일).
            pos_side = pos.get("side", "long")
            held_now = held_qty_from_snapshot(snap_pre, pos["symbol"], pos_side)
            clamped = clamp_sell_qty(held_now, sell_qty)
            if not clamped:                           # None=잔고 미상, 0=외부 매도(보유 0) → skip
                # 관측(2026-07-20): 종가 청산이 조용히 건너뛰어지던 유일한 경로.
                # "종가에 왜 청산이 안 됐나"의 직접 사유라 원격에 남겨야 한다.
                decisions.append(order_log.decision(
                    "skip_close_no_holding", str(sid), pos.get("strategy_name", ""),
                    pos["symbol"],
                    f"종가청산 skip — 브로커 실보유 {'미상' if held_now is None else 0}"
                    f"(원장 {sell_qty}). 외부 매도·조회 실패 추정 — 정산 reconcile이 대조"))
                log.info("[종가청산] %s KIS 실보유 0/미상 — 발주 skip", pos["symbol"])
                continue
            if pos_side == "short":
                self._submit_close_short(sid, pos.get("strategy_name", ""), pos["symbol"],
                                         clamped, ref_price, policy, reason, decisions)
            else:
                self._submit_sell(sid, pos.get("strategy_name", ""), pos["symbol"],
                                  clamped, ref_price, policy, reason, decisions)
        # 즉시 체결/거부 반영(발주창 외 거부를 결과에 표면화).
        self._resolve_pending(decisions)
        # θ — 종가 단일가 체결 지연 흡수: 일반 cycle과 동일 wait(기본 60s/20s 폴링).
        # 실전 선물 단일가(15:45) 체결처럼 wait를 넘는 건은 15:50 settlement가 정리.
        gp = merged_execution(None)
        self._wait_pending(gp["post_submit_wait_sec"], gp["poll_interval_sec"],
                           decisions)
        # N1 표면화 — 오늘 낸 당일매매 진입이 여전히 체결확인 불능인데 계좌 보유가
        # 원장을 초과하면 '청산 불능'을 명시한다. 추측 매도는 금지: 초과 보유가
        # 사용자의 외부 수동 매수일 수 있고(병1 불변식 — 외부 보유 불가침), 체결
        # 진실 없이 내는 주문은 오인 매도가 된다. 복원은 resolve·settlement 몫.
        kst = ZoneInfo("Asia/Seoul")
        for p in list(self.pending.values()):
            sym = p.get("symbol", "")
            if _market_group_safe(sym) != market:
                continue
            if qc.is_futures(sym) != (instrument_class == "futures"):
                continue
            d_def = p.get("definition") or {}
            hd = ((d_def.get("position") or {}).get("exit") or {}).get("hold_days")
            if hd != 0:
                continue
            ts = float(p.get("submitted_ts") or 0)
            if datetime.fromtimestamp(ts, kst).date() != today:
                continue
            side = "short" if p.get("side") == "sell" else "long"
            held_now = held_qty_from_snapshot(snap_pre, sym, side)
            covered = sum(int(lg.get("qty") or 0) for lg in self.ledger.values()
                          if lg.get("symbol") == sym
                          and lg.get("side", "long") == side)
            if held_now is None or held_now <= covered:
                continue
            log.error("[종가청산] 당일청산 불능 — 진입주문 %s(%s) 체결확인 실패, "
                      "계좌 보유 %d > 원장 %d (미기록 체결 의심)",
                      p.get("order_no"), sym, held_now, covered)
            decisions.append(order_log.decision(
                "error", str(p.get("strategy_id", "")),
                p.get("strategy_name", ""), sym,
                f"당일청산 불능 — 진입주문 {p.get('order_no')} 체결확인 실패인데 "
                f"계좌 보유 {held_now} > 원장 {covered}. 미기록 체결 의심 — "
                "정산 reconcile·수동 확인 필요"))
        # N2 — 이 시장·클래스의 미확인 잔존 건수. 서버 타임라인이 "발주-but-미확인"
        # 사이클을 녹색 성공으로 표시하지 않도록 요약에 노출한다.
        n_unresolved = sum(
            1 for p in self.pending.values()
            if _market_group_safe(p.get("symbol", "")) == market
            and qc.is_futures(p.get("symbol", "")) == (instrument_class == "futures"))
        return self._state_payload(
            decisions, today, kind="day_trade_close", market=market,
            record_cycle=record_cycle,
            extra_summary={"instrument_class": instrument_class,
                           "n_pending_unresolved": n_unresolved})

    def enter_close_candidates(self, by_strategy: list[dict],
                               strategies: list[dict], dataset: dict,
                               risk_limits: dict | None = None, *,
                               market: str = "KRX",
                               instrument_class: str = "stock") -> dict:
        """종가창 진입 — fill=close/typical 전략을 종가 무렵 발주 (Stage C, additive).

        아침 시가창(run_cycle)은 fill=next_open만 진입하고, 종가매수(fill=close)는 이 경로가
        종가 발주창(주식 15:25·선물 15:40·미장 마감−5분)에 전담한다. 진입 로직(사이징·발주·
        ledger·게이트)은 아침과 동일한 _enter_from_preview 재사용 — 참조가만 종가 무렵 현재가
        (is_close_entry). 청산(익일 시가매도 등 hold_days≥1)은 다음날 아침 청산 패스가 처리하므로
        여기선 진입만 한다. 킬스위치·drawdown 게이트는 아침 진입(_cycle_body §3)과 동일 의미로
        적용해 일중 손실 회로가 열렸으면 신규 종가진입도 차단한다(day_start 앵커·손실한도 발동은
        아침 cycle·장중 loop가 담당 — 여기선 그 상태만 읽는다).

        instrument_class로 주식/선물 분리(스케줄러가 클래스별 cron으로 호출 — 종가창 시각이 다름).
        _CYCLE_LOCK으로 장중 ks·settlement 트리거와 직렬화(아침 cycle과 동일 규약).
        """
        with _CYCLE_LOCK:
            self._in_cycle = True
            self._krx_status = {}      # 종가창 halt 미조회 — 브로커 거부가 2차 안전망(아침 fallback과 동형)
            self._reserved_us = False  # 종가 진입은 즉시 주문(예약 아님)
            try:
                return self._enter_close_body(by_strategy, strategies, dataset,
                                              risk_limits or {}, market, instrument_class)
            finally:
                self._in_cycle = False
                self._reserved_us = False

    def _enter_close_body(self, by_strategy, strategies, dataset, rl,
                          market, instrument_class) -> dict:
        decisions: list[dict] = []
        today = kst_today()
        # 미체결 정합 후 잔고 — 아침 cycle과 동일 순서(미기록 체결 복원 뒤 사이징).
        self._resolve_pending(decisions)
        try:
            snap_pre = self.broker.account_snapshot()
            equity_now = _unified_equity_krw(snap_pre["balance"])
        except Exception as e:
            log.error("[종가진입] 잔고 조회 실패 — 진입 보류: %s", e)
            decisions.append(order_log.decision(
                "skip_kis_health", "", "", "",
                f"KIS 잔고 조회 실패 — 종가 진입 보류: {e}"))
            return self._state_payload(
                decisions, today, kind="day_trade_close", market=market,
                record_cycle=False,
                extra_summary={"instrument_class": instrument_class, "n_bought": 0})
        balance_fetch_failed = list(
            (snap_pre.get("balance") or {}).get("fetch_failed") or [])
        # 잔고 부분조회 실패 — 손실한도·drawdown 평가 보류를 표면화(아침 §1과 동일 계약). 부분
        # equity는 거짓 −98% 폭락으로 읽혀(06-09 킬스위치 오발동 실증) 위험 결정을 오염시키므로
        # 측정이 완전할 때만 평가한다. 진입 자체는 아침과 동일하게 계속(가용성 우선 — understated
        # equity는 보수적 사이징이라 과매수 위험 없음).
        if balance_fetch_failed:
            log.critical("[종가진입] 잔고 부분조회 %s — 손실한도·drawdown 평가 보류 "
                         "(부분 equity %s원)", balance_fetch_failed, f"{equity_now:,.0f}")
            decisions.append(order_log.decision(
                "risk_eval_skipped", "", "", "",
                f"잔고 부분조회 실패 {balance_fetch_failed} — 손실한도·drawdown 평가 보류"))

        # 리스크 한도 인스턴스 상태 — 아침 _cycle_body와 동일(발주 직전 _try_buy_one_symbol 참조).
        global_policy = merged_execution(None)
        self._us_bp_mode = rl.get("us_buying_power_mode") or "integrated"
        _dll = rl.get("kill_switch_daily_loss_pct")
        self._daily_loss_limit_pct = float(_dll) if _dll is not None else None
        self._daily_turnover_limit_krw = int(rl.get("daily_turnover_limit_krw") or 0)
        self._daily_trade_count_limit = int(rl.get("daily_trade_count_limit") or 0)

        # 킬스위치·drawdown 게이트 (아침 진입 §1/§3 미러) — 신규 진입만 차단(청산은 무관).
        ks_state = killswitch.load()
        ks_active = bool(ks_state.get("active"))
        # 일일 손실 한도 재평가 — 장중 손절 loop는 15:30 종료라, 선물 종가창(15:40)과 주식
        # 종가 단일가(15:25~15:30)에서 새로 터진 일일 손실을 (멈춘) loop도 아침 cycle도 못 잡는다.
        # day_start 앵커는 아침 cycle(또는 catch-up)이 이미 설정했으므로 여기선 update 없이
        # check만 — 한도 초과면 신규 종가 진입을 차단한다(장 막판 손실을 종가에 키우는 것 방지).
        if (not balance_fetch_failed and not ks_active
                and self._daily_loss_limit_pct is not None):
            reason = killswitch.check_daily_loss(equity_now, self._daily_loss_limit_pct)
            if reason:
                killswitch.activate(reason)
                ks_active = True
                ks_state = killswitch.load()
        _user_dd = rl.get("max_drawdown_pct")
        max_dd = _user_dd if _user_dd is not None else global_policy.get("max_drawdown_pct")
        drawdown_active = False
        if max_dd is not None and not balance_fetch_failed and equity_now > 0:
            peak = equity_now
            for e in self.equity:
                v = float(e.get("value") or 0)
                if v > peak:
                    peak = v
            if peak > 0 and (equity_now - peak) / peak * 100 <= -abs(float(max_dd)):
                drawdown_active = True

        if ks_active:
            decisions.append(order_log.decision(
                "skip_killswitch", "", "", "",
                f"종가 진입 차단 — {ks_state.get('reason', '')}"))
        elif drawdown_active:
            decisions.append(order_log.decision(
                "skip_drawdown", "", "", "",
                "종가 진입 차단 — 누적 drawdown 한도 도달"))
        elif by_strategy:
            # entry_window="close" → _enter_from_preview가 is_close_entry(현재가 참조)를 내부 파생.
            self._enter_from_preview(by_strategy, strategies, dataset, equity_now,
                                     decisions, set(), market=market,
                                     entry_window="close",
                                     instrument_class=instrument_class)

        # 종가 단일가 체결 지연 흡수 — 아침 cycle과 동일 wait(기본 60s/20s 폴링).
        self._wait_pending(global_policy["post_submit_wait_sec"],
                           global_policy["poll_interval_sec"], decisions)
        n_bought = sum(1 for d in decisions if d["action"] == "bought")
        # kind=day_trade_close + record_cycle=False: 종가창 진입을 별도 슬롯/로컬 cycle로 만들지
        # 않고 run_close_cycle이 청산(day_trade_close)과 병합해 1회 push한다 — 서버 종가 슬롯
        # (trading.py: kind="day_trade_close")에 진입 n_bought까지 함께 매칭(서버 무변경).
        return self._state_payload(
            decisions, today, kind="day_trade_close", market=market,
            record_cycle=False,
            extra_summary={"instrument_class": instrument_class, "n_bought": n_bought})

    def state_snapshot(self) -> dict:
        """현 상태(잔고·포지션·kill_switch) 스냅샷 — 거래 없이 상태 변경(kill-switch 해제·
        일시정지·재개·주문취소 등)을 웹에 즉시 반영하기 위한 push용. decisions 없음·cycle
        로그 미기록(타임라인 오염 방지). auto_status는 push_snapshot이 일괄 주입한다.
        """
        return self._state_payload([], kst_today(), kind="state_sync",
                                   record_cycle=False)

    def _state_payload(self, decisions: list[dict], today: date, *,
                       kind: str = "emergency_liquidation",
                       record_cycle: bool = True,
                       market: str = "ALL",
                       extra_summary: dict | None = None) -> dict:
        """현재 잔고·포지션·결정·kill_switch를 Monitor용 스냅샷 payload로 — 정규 cycle 출력
        (_cycle_body 꼬리)은 건드리지 않는다(주식 골든 byte-identical 보존, blast radius 0).
        비상청산(kind=emergency_liquidation)·상태동기화(kind=state_sync) 공용 빌더.

        market — cycle_summary.market. 비상청산·상태동기화는 전시장(ALL)이 맞지만
        day_trade_close는 실제 시장을 식별해야 서버 타임라인이 슬롯(주식 15:25/
        선물 15:40/미장 close−5분)별로 매칭한다(N2 — 'ALL' 하드코딩이 매칭 불가의
        원인이었다). extra_summary — kind별 추가 요약 필드(instrument_class 등).
        """
        try:
            snap = self.broker.account_snapshot()
            balance = snap.get("balance", {}) or {}
            positions = snap.get("positions", []) or []
        except Exception as e:
            log.warning("[%s] 스냅샷 조회 실패: %s", kind, e)
            balance, positions = {}, []
        # M9: 여기 있던 무조건 _save() 제거 — 모든 변경 지점이 락 안에서 즉시
        # 저장하므로(중복), 장수명 인스턴스의 stale 메모리가 다른 인스턴스의
        # 변경(매도·체결)을 덮어쓰는 부활 사고의 마지막 경로였다. 스냅샷 빌더는
        # 읽기 전용이어야 한다.
        try:
            broker_pending = self.broker.pending_orders()
        except Exception:
            broker_pending = []
        cycle_summary = {
            "today": today.isoformat(),
            "market": market,
            "kind": kind,
            "n_sold": sum(1 for d in decisions
                          if d["action"] == "sold" and not d.get("netted")),
            "n_rejected": sum(1 for d in decisions if d["action"] == "rejected"),
            "n_unfilled": sum(1 for d in decisions if d["action"] == "unfilled"),
            "n_errors": sum(1 for d in decisions if d["action"] == "error"),
            "kill_switch": bool(killswitch.load().get("active")),
        }
        if extra_summary:
            cycle_summary.update(extra_summary)
        if record_cycle:
            order_log.log_cycle(decisions, cycle_summary)
        positions_rich = analytics.enrich_positions(
            positions, self.ledger, today.isoformat())
        return {
            "balance": balance,
            "positions": positions_rich,
            "equity": self.equity[-365:],
            "trades": [d for d in decisions if d["action"] in ("bought", "sold")],
            "decisions": decisions,
            "broker_pending": broker_pending,
            "pending_local": list(self.pending.values()),
            "recent_orders": order_log.read_orders(50),
            "recent_cycles": order_log.read_cycles(10),
            "slippage": order_log.slippage_stats(),
            "kill_switch": killswitch.load(),
            "cycle_summary": cycle_summary,
            "drawdown": analytics.drawdown_state(),
            "health": analytics.local_health(),
        }

    def cycle(self, strategies: list[dict], dataset: dict,
              today: date | None = None,
              buy_candidates: list[dict] | None = None,
              risk_limits: dict | None = None,
              market: str = "KRX",
              krx_status: dict[str, dict] | None = None,
              catchup: bool = False,
              reserved: bool = False,
              cycle_id: str = "",
              instrument_class: str | None = None) -> dict:
        """전략 목록을 1회 평가하고 매매한 뒤 동기화용 스냅샷을 반환한다.

        market: 이번 사이클이 다룰 시장 그룹('KRX' 또는 'US'). 청산은 해당 시장
        보유분만, 진입은 해당 시장 후보만 처리한다 — 시장별 정규장 시각에 맞춰
        분리 실행하기 위함. kill switch·drawdown은 계좌 전체(통합 equity) 기준.

        instrument_class: 자산군 스코프(None=전체, "stock"/"futures") — 선물 개장
        (08:45)과 주식 개장(09:00)이 달라 아침 사이클을 08:35 선물 / 08:55 주식으로
        분리 실행하기 위함(파이프라인 문제 10 — kr-target-reconciliation.md §15 Phase 4).

        buy_candidates(by_strategy 리스트, 비어있어도 list)가 신규 진입 source.
        Phase 38.4: 항상 preview 경로 — buy_candidates가 빈 리스트면 진입 0,
        청산은 정상. 호출자(runner)가 preview 누락 시 []로 전달.

        risk_limits(Phase 38.7/38.10): 사용자 위험 한도. 예:
          {"kill_switch_daily_loss_pct": 2.0, "max_drawdown_pct": 15.0}
        키가 없거나 None이면 글로벌 default 사용.

        Q5(AL-4): cycle/settlement/장중 ks 트리거의 직렬화. _CYCLE_LOCK을 acquire
        후 진입 — 동시 진입을 막아 broker.account_snapshot·발주 순서를 보존한다.
        """
        # Q5: 외부 호출자가 이미 락을 쥔 채로 cycle을 호출하는 경우(예: 장중 ks
        # 핸들러)도 대비해 RLock이 아닌 Lock을 쓰되, 모든 진입은 같은 thread가
        # 중첩 호출하지 않도록 호출 규약으로 강제한다. timeout=None으로 blocking.
        with _CYCLE_LOCK:
            return self._cycle_locked(strategies, dataset, today,
                                       buy_candidates, risk_limits, market,
                                       krx_status, catchup=catchup,
                                       reserved=reserved, cycle_id=cycle_id,
                                       instrument_class=instrument_class)

    def _cycle_locked(self, strategies, dataset, today, buy_candidates,
                       risk_limits, market, krx_status,
                       catchup: bool = False, reserved: bool = False,
                       cycle_id: str = "",
                       instrument_class: str | None = None) -> dict:
        # R5(CY-1) — 락 확보 직후 디스크 상태 재적재. 이 Trader 인스턴스는 락 밖
        # (runner의 broker 준비 단계)에서 생성돼 그 시점 사본을 들고 있는데, 백오프
        # 재시도가 08:40/42/52 cron·WS 체결 스레드와 겹치면 상대가 그 사이 저장한
        # 변이(체결 반영·pending 마감)를 낡은 사본 위 통저장이 덮어쓴다(lost-update
        # — 체결 소실 → 무손절 노출 부류). 락 안 reload 1줄이 "낡은 사본" 자체를
        # 제거한다. 모든 변이 지점은 락 안 즉시 _save이므로 잃는 변경도 없다.
        self.reload_state()
        # Q5(데드락 방지): _in_cycle 플래그를 try/finally로 보장 — 예외 발생 시에도
        # 반드시 reset되어야 다음 cycle에서 _apply_fill의 평가가 정상 동작.
        self._in_cycle = True
        # Phase 48 — 종목 상태 dict는 인스턴스에 저장해 _try_buy_one_symbol에서 사용.
        # cycle 단위 stale 안전 (dict는 cycle 시작 시 fresh, 다음 cycle에서 다시 받음).
        self._krx_status: dict[str, dict] = krx_status or {}
        # 미국 예약주문 라우팅 — 개장 전 entry cycle에서만 True. 장중 손절/킬스위치
        # 청산 re-entry(market="US"여도 reserved=False)는 즉시 시장 주문이어야 하므로
        # market 추론이 아닌 명시 파라미터로 받는다. try/finally로 반드시 reset.
        self._reserved_us = bool(reserved)
        try:
            return self._cycle_body(strategies, dataset, today,
                                     buy_candidates, risk_limits, market,
                                     catchup=catchup, cycle_id=cycle_id,
                                     instrument_class=instrument_class)
        finally:
            self._in_cycle = False
            self._reserved_us = False

    def _cycle_body(self, strategies, dataset, today, buy_candidates,
                     risk_limits, market, catchup: bool = False,
                     cycle_id: str = "",
                     instrument_class: str | None = None) -> dict:
        today = today or kst_today()
        decisions: list[dict] = []
        self._sizing_trace = {}          # 이번 사이클 근거만 담기(관측 전용)

        # 옛 Phase 48 P1-B의 시간 기반 KIS 점검 가드(평일 03:00~06:00 등)는 제거.
        # 이유: KIS 공식 점검 시간 doc 부재로 보수적 추정 차단했으나, 실측 probe
        # (~/.quant-platform/probes/kis_maintenance.jsonl) 결과 03:28+ 정상 응답
        # 다수 → 가드 over-conservative. 미장 마감(KST 05:00) 직전 catch-up 매수
        # 차단되는 실 사고 (2026-05-28). KIS 진짜 점검 중이면 아래 잔고 health
        # check가 즉시 cycle 중단 (skip_kis_health). 4원칙 PR-1 — 추정 fallback
        # 보다 실측 fallback이 본질적 해결.

        # ── 0. 이전 사이클 미체결 정리 ─────────────────────────────────────
        log.info("[cycle-body] 미체결 정리 시작 (pending=%d건)", len(self.pending))
        _t0 = time.monotonic()
        self._resolve_pending(decisions)
        log.info("[cycle-body] 미체결 정리 완료 %.1fs (잔여=%d건) — 잔고 조회 시작",
                  time.monotonic() - _t0, len(self.pending))

        # ── 1. 자본·day_start 갱신, kill switch 평가 ──────────────────────
        # equity는 계좌 전체 통합(국내+해외, KRW) — kill switch는 시장 무관 계좌 단위.
        # Phase 48 P1-B — 헬스체크 강화. 잔고 조회 실패는 KIS API 단절 신호이므로
        # 0으로 fallback하지 말고 cycle 전체를 중단 (잘못된 equity로 매도 평가 방지).
        try:
            snap_pre = self.broker.account_snapshot()
            equity_now = _unified_equity_krw(snap_pre["balance"])
        except Exception as e:
            log.error("[P1-B] KIS 잔고 조회 실패 — cycle 중단: %s", e)
            decisions.append(order_log.decision(
                "skip_kis_health", "", "", "",
                f"KIS API 응답 실패 — 자동매매 보류 (다음 사이클 재시도): {e}"))
            # cycle_summary에 kind·market·error 부여 — 없으면 서버 타임라인이 이 자기중단을
            # "missed-B(cron 미발동)"로 오분류한다(2026-07-13 감사). kind로 스케줄 슬롯 매칭·
            # error로 "실행됐으나 실패(C)" 분류 + 건강 모니터 C6(cycle_execution) RED.
            return {"balance": {"cash": 0, "total_eval": 0},
                    "positions": [], "equity": self.equity[-365:],
                    "trades": [], "decisions": decisions,
                    "cycle_summary": {"kind": "catchup_cycle" if catchup else "cycle",
                                       "market": market,
                                       "skipped_reason": "kis_health_fail",
                                       "error": f"KIS 잔고 조회 실패 — cycle 중단: {e}",
                                       "cycle_id": cycle_id}}

        # ★ε: 부분 잔고(해외/선물 조회 실패) — 위험 결정(day_start 앵커·손실한도·
        # drawdown)은 보류하고 표면화한다. 누락 계좌가 0으로 잡힌 부분 equity는
        # 거짓 -98% 폭락을 만들고, 06-09 킬스위치 거짓 발동으로 US 보유 전량이
        # 청산됐다. 매매 자체(청산 규칙·진입)는 시장별 데이터로 계속 — 가용성은
        # 유지하되 자금 안전 결정만 완전 측정치를 요구한다.
        balance_fetch_failed = list(
            (snap_pre.get("balance") or {}).get("fetch_failed") or [])
        if balance_fetch_failed:
            log.critical("[P1-B] 잔고 부분조회 %s — day_start/killswitch/drawdown "
                         "평가 보류 (부분 equity %s원)",
                         balance_fetch_failed, f"{equity_now:,.0f}")
            decisions.append(order_log.decision(
                "risk_eval_skipped", "", "", "",
                f"잔고 부분조회 실패 {balance_fetch_failed} — "
                "손실한도·drawdown 평가 보류"))
        else:
            killswitch.update_day_start(equity_now, today.isoformat())
        ks_state = killswitch.load()
        ks_active = bool(ks_state.get("active"))

        # 글로벌 default를 미사용 시에도 적용하기 위해 빈 policy로 시작
        global_policy = merged_execution(None)

        # Phase 38.7 — 사용자 설정 우선, null이면 글로벌 default
        rl = risk_limits or {}
        # 미국 매수여력 모드 (사용자 설정) — _try_buy_one_symbol 사이징에 반영
        self._us_bp_mode = rl.get("us_buying_power_mode") or "integrated"
        # 일일 손실 한도: user 모니터링 설정에서만 가져옴. None이면 OFF.
        # (ExecutionPolicy.daily_loss_limit_pct 제거됨 — 종목 단위 실시간 매도로 위험 처리.)
        daily_loss_limit_pct = rl.get("kill_switch_daily_loss_pct")
        # max_drawdown 한도: user setting 우선, 없으면 global_policy. None이면 OFF.
        _user_dd = rl.get("max_drawdown_pct")
        _global_dd = global_policy.get("max_drawdown_pct")
        max_drawdown_limit_pct = _user_dd if _user_dd is not None else _global_dd
        # Q5: 체결 후 즉시 평가용으로 인스턴스에 저장. None이면 평가 skip.
        self._daily_loss_limit_pct = (float(daily_loss_limit_pct)
                                        if daily_loss_limit_pct is not None else None)
        # Phase 48 P1-D — 일일 거래 한도 (0 = 비활성).
        self._daily_turnover_limit_krw = int(rl.get("daily_turnover_limit_krw") or 0)
        self._daily_trade_count_limit = int(rl.get("daily_trade_count_limit") or 0)

        if (not balance_fetch_failed and not ks_active
                and daily_loss_limit_pct is not None):
            reason = killswitch.check_daily_loss(
                equity_now, daily_loss_limit_pct)
            if reason:
                killswitch.activate(reason)
                ks_active = True
                ks_state = killswitch.load()

        # Phase 38.10 — 누적 drawdown 측정 (자본 고점 대비). kill switch와 별개.
        # peak는 equity log의 max + 현재 equity 중 큰 값.
        # ★ε: 부분 잔고면 측정 보류(거짓 폭락으로 진입 차단 오발동 방지).
        peak_equity = equity_now
        for e in self.equity:
            v = float(e.get("value") or 0)
            if v > peak_equity:
                peak_equity = v
        drawdown_pct = 0.0
        if peak_equity > 0 and not balance_fetch_failed:
            drawdown_pct = (equity_now - peak_equity) / peak_equity * 100
        # max_drawdown_limit_pct=None이면 한도 없음(OFF) — drawdown 차단 평가 skip.
        drawdown_active = (max_drawdown_limit_pct is not None
                            and drawdown_pct <= -abs(float(max_drawdown_limit_pct)))
        if drawdown_active:
            log.warning(
                "drawdown 한도 도달 — 자본 고점 %s원 → 현재 %s원 (%.2f%%, 한도 -%.2f%%)",
                f"{peak_equity:,.0f}", f"{equity_now:,.0f}",
                drawdown_pct, float(max_drawdown_limit_pct))

        # ── 2·3. 목표상태 수렴 (kr-target-reconciliation.md §2) ─────────────────
        # 구 넷팅 pre-pass + §2 잔여청산 + §3 진입을 단일 패스로 대체. 심볼별로
        # target(유지+진입−청산)과 브로커 실보유의 차이만 순발주한다 — 수동매매 drift는
        # 자동 흡수(§6), 오버셀·이중계상·phantom 정산은 구조적으로 불가(§14).
        entries_blocked = ks_active or drawdown_active
        if ks_active:
            decisions.append(order_log.decision(
                "skip_killswitch", "", "", "",
                f"신규 진입 차단 — {ks_state.get('reason', '')}"))
        elif drawdown_active:
            decisions.append(order_log.decision(
                "skip_drawdown", "", "", "",
                f"신규 진입 차단 — 누적 drawdown {drawdown_pct:.2f}% "
                f"(한도 -{float(max_drawdown_limit_pct):.1f}%)"))
        n_netted, commission_saved, n_drift = self._reconcile_pass(
            window="open", snap_pre=snap_pre, dataset=dataset, today=today,
            market=market, instrument_class=instrument_class,
            buy_candidates=buy_candidates, strategies=strategies,
            equity_now=equity_now, entries_blocked=entries_blocked,
            ks_active=ks_active, decisions=decisions, catchup=catchup)

        # ── 4. 미체결 짧게 대기 (시초가 동시호가 직후 대부분 잡힘) ───────
        # Q7: 300초 → 60초 (post_submit_wait_sec). DAY 정책으로 못 잡힌 분은
        # 다음 사이클 또는 KIS 마감 자동 cancel이 정리.
        self._wait_pending(global_policy["post_submit_wait_sec"],
                           global_policy["poll_interval_sec"], decisions)

        # ── 5. 최종 스냅샷 ────────────────────────────────────────────────
        snap = self.broker.account_snapshot()
        post_balance = snap.get("balance") or {}
        post_fetch_failed = list(post_balance.get("fetch_failed") or [])
        # ε: equity 시계열 = 통합 자산(국내+해외+USD현금+선물) — 국내만(total_eval)
        # 적재하면 분자(시계열)/분모(통합 day_start)가 섞여 웹 자산곡선이 -98%로
        # 보였다(D3-3). 주식 전용 사용자는 해외/선물 키가 0이라 값 동일(무변경).
        # 부분 조회면 거짓 저점을 적재하지 않는다(곡선 오염 방지).
        equity_post = _unified_equity_krw(post_balance)
        if post_fetch_failed:
            log.warning("equity 기록 skip — 잔고 부분조회 %s", post_fetch_failed)
        else:
            self.equity.append({"date": today.isoformat(),
                                "value": equity_post})
        self._save()

        try:
            broker_pending = self.broker.pending_orders()
        except Exception as e:
            log.warning("미체결 조회 실패: %s", e)
            broker_pending = []

        # 넷팅 합성 체결(netted)은 실거래 카운트에서 제외한다(N3 — 브로커 미접촉).
        n_bought_now = sum(1 for d in decisions
                           if d["action"] == "bought" and not d.get("netted"))
        n_sold_now = sum(1 for d in decisions
                         if d["action"] == "sold" and not d.get("netted"))
        # A안(서버 타임라인 '자동매매 시작'이 그날 실제 진입을 반영): 시가 진입 주문은
        # 09:00 개장 단일가 체결 전이라 08:55 사이클 시점엔 n_bought/n_sold(인사이클 체결)에
        # 안 잡히고 pending에 남는다 → 발주 완료분(체결+체결대기)을 side별로 기록한다.
        # 매수=롱 진입(n_buy_placed), 매도=숏 진입(n_sell_placed·양방향 선물)을 각각 정확히
        # 표면화한다(side 분리로 오집계 방지). 체결대기는 *이 거래일 발주분*만 센다 —
        # 다일 잔존 orphan 제외(_count_today_pending docstring 참조).
        _today_start_ts = datetime(
            today.year, today.month, today.day,
            tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
        n_buy_pending = _count_today_pending(
            self.pending, "buy", market, _today_start_ts)
        n_sell_pending = _count_today_pending(
            self.pending, "sell", market, _today_start_ts)

        cycle_summary = {
            "today": today.isoformat(),
            "market": market,                        # Phase 7 catch-up — 시장 식별
            "kind": "catchup_cycle" if catchup else "cycle",   # catch-up 구분
            # 자산군 스코프(None=전체) — 08:35 선물/08:55 주식 분리 사이클 식별(문제 10).
            "instrument_class": instrument_class,
            "cycle_id": cycle_id,                    # 시작 저널(cycle_started)과 join
            "n_strategies": len(strategies),
            "n_bought": n_bought_now,
            "n_buy_placed": n_bought_now + n_buy_pending,
            "n_sold": n_sold_now,
            "n_sell_placed": n_sold_now + n_sell_pending,
            "n_netted": int(n_netted),                   # 합성 정산 상쇄 계약수(시가창)
            "commission_saved_krw": round(commission_saved, 2),
            # 목표수렴 drift 교정 계약수 — 수동매매 되돌림/비전략 정리(원장 불변 주문).
            "n_drift": int(n_drift),
            "n_skip_held": sum(1 for d in decisions if d["action"] == "skip_held"),
            # 데이터 결손으로 발주 판단 불가 — 서버 건강 모니터 C7이 소비(0발주가 정상 무후보인지
            # 데이터 결손인지 구분). 이전엔 decisions에만 있어 집계·경보에 승격 안 됐다.
            "n_skip_no_data": sum(1 for d in decisions if d["action"] == "skip_no_data"),
            "n_rejected": sum(1 for d in decisions if d["action"] == "rejected"),
            "n_unfilled": sum(1 for d in decisions if d["action"] == "unfilled"),
            "n_errors": sum(1 for d in decisions if d["action"] == "error"),
            "n_unparseable_orphan": sum(
                1 for d in decisions if d["action"] == "unparseable_orphan"),
            "n_skip_uncovered": sum(
                1 for d in decisions if d["action"] == "skip_uncovered"),
            "n_skip_wrong_account": sum(
                1 for d in decisions if d["action"] == "skip_wrong_account"),
            "n_orphan_uncovered": sum(
                1 for d in decisions if d["action"] == "orphan_uncovered"),
            # N2 — 이 시장의 미확인 잔존(발주-but-체결미확인 + 이월 미체결).
            # 아침 cycle은 DAY 지정가가 장중 자연 체결될 수 있어 잔존이 정상이지만
            # (서버 타임라인도 cycle 슬롯엔 경고 안 함), 관측을 위해 항상 노출한다.
            "n_pending_unresolved": sum(
                1 for p in self.pending.values()
                if _market_group_safe(p.get("symbol", "")) == market),
            # 관측(2026-07-20) — 목표 수량의 근거. "왜 N계약인가"를 서버에서 즉답하기
            # 위한 사이징 추적(브로커 원값·크레딧·사용률·목표). 빈 dict면 미해당(주식·sim).
            "sizing": dict(self._sizing_trace),
            # 원장 밖 브로커 보유를 전략 목표에 흡수한 건수 — 넷팅 leg에서 역산해야
            # 했던 것(mwmw 07-20)을 명시화. 상세는 decisions의 absorbed_external.
            "n_absorbed_external": sum(
                1 for d in decisions if d["action"] == "absorbed_external"),
            # A1 — 외부(수동) **미체결 주문**을 목표에 선반영한 결과 {계약키: 부호수량}.
            # 위 absorbed_external(원장 밖 **보유**)과 다른 축이다: 이쪽은 아직 체결 전인
            # 수동 주문. 종전엔 INFO 로그뿐이라 원격 진단이 불가했다.
            # {"__error__": ...}면 조회 실패로 A1 skip(개장후 수렴이 백업).
            "external_pending": dict(self._a1_trace),
            # 이번 창이 목표를 확정한 심볼(목표 0 포함) — 동시호가 가드가 우주에
            # 더해 원장 행 없는 목표까지 감시한다(auction_guard._window_summary).
            "target_symbols": list(self._target_syms),
            "kill_switch": ks_active,
            "equity_pre": equity_now,
            "equity_post": equity_post,    # ε: 통합 자산(KRW) — equity_pre와 동일 정의
            # Phase 38.10 — drawdown 모니터
            "drawdown_pct": round(drawdown_pct, 3),
            "peak_equity": round(peak_equity, 2),
            "drawdown_active": drawdown_active,
            "max_drawdown_limit_pct": (float(max_drawdown_limit_pct)
                                          if max_drawdown_limit_pct is not None else None),
        }
        # ★ε: 부분 잔고로 위험 평가를 보류한 사이클은 명시 표면화(웹/타임라인 인지).
        if balance_fetch_failed or post_fetch_failed:
            cycle_summary["balance_fetch_failed"] = sorted(
                set(balance_fetch_failed) | set(post_fetch_failed))
        order_log.log_cycle(decisions, cycle_summary)

        # 포지션 풍부화 + 분석 집계 (Monitor용)
        positions_rich = analytics.enrich_positions(
            snap["positions"], self.ledger, today.isoformat())

        return {
            "balance": snap["balance"],
            "positions": positions_rich,
            "equity": self.equity[-365:],
            "trades": [d for d in decisions if d["action"] in ("bought", "sold")],
            "decisions": decisions,
            "broker_pending": broker_pending,
            "pending_local": list(self.pending.values()),
            "recent_orders": order_log.read_orders(50),
            "recent_cycles": order_log.read_cycles(10),
            "slippage": order_log.slippage_stats(),
            "kill_switch": killswitch.load(),
            "cycle_summary": cycle_summary,
            # Phase 13 — Monitor 고도화
            "strategy_pnl": analytics.strategy_pnl_summary(),
            "slippage_by_hour": analytics.slippage_by_hour(),
            "rejection_reasons": analytics.rejection_reasons(),
            "drawdown": analytics.drawdown_state(),
            "health": analytics.local_health(),
        }
