"""동시호가 가드(#16 · 2026-07-19 유저 확정) — 창내 수동 개입의 실시간 정합.

원칙: 동시호가 미체결은 시장가든 지정가든 "전량 체결" 가정(유저 확정 — 지정가
유찰분은 창 종료 후 기존 수렴이 흡수). 목표(전략 의도)와 어긋나는 창내 개입을
장이 닫히기 전에 **주문 수정**으로 정합한다 — 반대 주문을 새로 내지 않으므로
같은 계좌 매수·매도가 단일가에 맞붙는 자전(가장매매) 소지가 원천적으로 없다.

  ① 자동 주문 유저 변경(취소·감량) → 잔여 전량 취소 + intent failed 마킹 후
     해당 창 사이클 재실행(멱등 재계획이 정확 수량을 재발주). 정정으로 수량
     증가가 불가한 브로커 제약상 "깨끗이 지우고 재발주"가 유일한 복원 경로.
  ② 외부(수동) 미체결이 목표를 초과 → **최신 주문부터 초과분만 취소**(유저 확정).
     잔량이 초과분보다 크면 그만큼만 부분취소한다 — 통째 취소(과잉 개입)도 초과
     방치도 없다. 근거 = LS CFOAT00300 CancQty 실계좌 실측(2026-07-20): 부분취소가
     수리되고 t0434 미체결에 **원주문번호가 유지된 채 ordrem만 감소**(신주문번호
     미발급)라 own/ext 분류·주문번호 추적이 깨지지 않는다. 같은 방향 수동 주문을
     다 걷어도 남는 초과분만 수렴 몫. 취소가 미체결 조회에 반영되기까지 10~20초가
     걸리는데(실측 2026-07-20) 폴링은 10초라, 창 누적 "남기려는 잔량"으로 유효
     잔량을 눌러 같은 초과분의 반복 취소를 막는다(_eff_remain).

스코프 = KRX 동시호가 4창(선물 개장·주식 개장·주식 마감·선물 마감). 창 중엔
단일가 체결 전이라 보유·원장이 변하지 않아 창 시작 스냅샷으로 고정 가능하고,
"자동 주문이 브로커 미체결에 없음 = 유저 취소"가 성립한다(체결 소멸 불가).

**가드 우주 = 이번 창 own 의도 심볼 ∪ 원장 심볼** (2026-07-19 감사 수정 G2).
엔진이 목표를 갖지 않는 심볼(비전략 보유·미등록 상품·unmapped)은 수렴 엔진이
indeterminate로 hold하는 것과 동일하게 가드도 손대지 않는다 — 그 심볼의 수동
주문은 유저 몫. 드리프트 교정 의도가 생기면 그 심볼은 own에 포함되어 관리된다.

검산식(틱마다·심볼별):  D = 예상 최종 − 목표 최종
    = [보유 + own잔여 + 외부잔여] − [원장 + 이번 창 own **전략** 의도]
우변 의도에서 목표수렴 드리프트 교정 의도(strategy_id "DRIFT:*")는 제외한다
(감사 수정 G3): 드리프트 주문은 체결돼도 원장 불변(브로커를 원장으로 옮기는
물리 이동)이라, 포함하면 우변이 실제 목표와 |drift|만큼 어긋나 도움 주문을
자르고 해로운 주문을 방치한다. 좌변 own잔여에는 드리프트 잔여도 포함(체결이
보유를 움직이므로). 넷팅 book leg(원장 즉시 기장)·A1 선반영은 양변 대칭이라
A1 인수 외부 미체결은 D=0으로 자연 보호된다(테스트 고정).

목표 확정 게이트(2026-07-20 유저 정련): 이번 창 담당 사이클의 무-error 완료
기록(0건 발주 포함)도, 이번 창 own 의도도 없는 동안(발주 전 지연·백오프·사이클
전멸)은 stale 원장을 목표로 강제하지 않는다 — 개입 전면 보류·관측만
(guard_hold_no_target). 그 틈의 수동 미체결은 취소되지 않고, 사이클이 성공하면
A1로 인수되며, 끝내 실패하면 창밖 수동과 동일하게 수렴·drift가 사후 정합한다.
own 의도가 하나라도 제출됐으면(부분 제출 크래시 포함) 그 leg 기준 정합을 즉시
시작한다.

관측: 모든 개입·보류는 order_log 결정 + cycles.jsonl(kind=auction_guard)로
기록되고(반복 관측성 결정은 심볼당 1회), 재실행이 발생하면 그 사이클의 스냅샷
push가 서버 타임라인에 반영된다. GUI 전용 배너는 미배선(후속).
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import intents, order_log

log = logging.getLogger("localapp.auction_guard")
KST = ZoneInfo("Asia/Seoul")

# 유저가 가드 복원과 반복 충돌(재취소)할 때의 창내 심볼당 재발주 상한 — 무한
# 맞대응 방지. 초과 시 관측만 남기고 물러난다(잔여는 수렴 몫).
_MAX_RESTORES_PER_SYMBOL = 2

# 목표 미달(살아있는 주문 0인데 D≠0) 재실행의 창 누적 상한. 재실행이 D를 못 지우는
# 경우(사이징 0·DRIFT 멱등·하드컷)가 있어 상한이 없으면 창 내내 매 틱 사이클을 돈다.
_MAX_CONVERGE_RERUNS = 2

# 반복 틱마다 같은 내용이 재발생하는 관측 전용 결정 — 창당 심볼당 1회만 기록.
_ONCE_ACTIONS = {"guard_ext_residual", "guard_own_giveup", "guard_skip_ambiguous",
                 "guard_hold_unsettled"}

# 브로커 접수 후 미체결 조회에 반영되기까지의 유예(초). 이 안에 있는 own 의도는
# "미체결에 안 보임"을 **유저 취소로 단정하지 않는다**.
#
# 근거: 브로커 미체결 조회는 접수를 즉시 반영하지 않는다(실측 2026-07-20 — 가드
# 취소가 t+10s엔 미반영·t+20s엔 반영). 가드 폴링은 10초라, 반영 전 틱에서
# op_signed(0) ≠ oi_total(±N)이 성립해 브랜치 ①이 "유저가 취소했다"로 오판한다.
# 그 결과 intent를 failed로 풀고 사이클을 재실행 → 원주문이 살아있는 채 같은
# 주문이 한 번 더 나가 **이중 발주**가 된다(동시호가 단일가라 둘 다 체결).
# 같은 부류를 intents.py의 no_fill 확정이 `_NO_FILL_MIN_AGE_SEC`로 이미 막고
# 있었는데(주석에 "이중 발주"라고 명시) 가드 브랜치 ①에만 빠져 있었다.
# 60초 = 실측 반영 지연(≤20s)의 3배 여유. 창(개장 8:36~8:44:30·마감 15:41~15:44:30)
# 안에서 유예가 끝나므로 유저 취소 복원 기능 자체는 유지된다.
_OWN_SETTLE_SEC = 60.0


def _canon(order_no) -> str:
    """주문번호 정규형 — 브로커·조회 TR마다 패딩이 다르다(감사 수정 G4ⓐ).

    KIS 발주응답 ODNO는 zero-pad-10, 선물 체결조회 odno는 공백패딩(strip 후
    무패딩), LS는 무패딩 정수 문자열. 선행 0·공백을 벗겨 비교한다."""
    return str(order_no or "").strip().lstrip("0")


def _sort_key(order_no) -> str:
    """"최신 주문부터" 정렬 키 — 정규형을 고정폭으로 재패딩해 자릿수 경계
    (99999→100000)에서도 사전순=숫자순이 되게 한다."""
    return _canon(order_no).zfill(24)


def _window_summary(today, window: str, instrument_class: str) -> dict | None:
    """이번 창 담당 사이클의 **가장 최근** 무-error 완료 요약 (없으면 None).

    두 가지를 함께 답한다:
      ① 목표 확정 여부 — None이 아니면 개입(트리밍·복원) 게이트가 열린다
         (2026-07-20 유저 정련). 확정 = 오늘 이 창 담당 사이클의 무-error 완료
         기록(개장=cycle/catchup_cycle·마감=day_trade_close·full-scope(None) 포함).
         0건 발주로 끝난 사이클도 "변화 없음"이라는 확정 목표라 게이트를 연다.
         판정 술어는 catchup의 창내 재실행 판정과 동일 계약.
      ② ``target_symbols`` — 그 사이클이 목표를 확정한 심볼(목표 0 포함).
         가드 우주(G2)를 이만큼 넓히는 데 쓴다.

    **가장 최근**을 쓰는 이유: 창내 재실행이 새 기록을 남기므로 옛 목표로 판정하면
    방금 세운 목표를 못 본다. read_cycles는 최신순이라 첫 매칭이 최신이다.
    own 의도 존재는 호출자가 여는 별도 빠른 경로 — 부분 제출 크래시도 제출된
    leg 기준 정합은 유지해야 하므로 이 함수는 완료 기록만 본다."""
    kinds = ("cycle", "catchup_cycle") if window == "open" else ("day_trade_close",)
    for e in order_log.read_cycles(limit=80):
        s = e.get("summary") or {}
        if (s.get("kind") not in kinds or s.get("market") != "KRX"
                or s.get("error")
                or s.get("instrument_class") not in (instrument_class, None)):
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts") or ""))
        except ValueError:
            continue
        if ts.date() == today:
            return s
    return None


@dataclass(frozen=True)
class GuardPlan:
    """한 틱의 조치 계획 — plan_guard_actions(순수)의 산출물."""
    cancels: list = field(default_factory=list)        # [(order_no, symbol, cancel_qty)]
    fail_intents: list = field(default_factory=list)   # [(intent_id, reason)]
    restored_symbols: list = field(default_factory=list)  # own 복원 대상 심볼(카운트용)
    rerun: bool = False
    decisions: list = field(default_factory=list)
    # cancels 중 잔량 일부만 취소한 주문번호 — 취소 후에도 원주문번호가 살아남는
    # 것이 정상이라(실측 2026-07-20) 실행부의 소멸 재확인에서 제외한다.
    partial_cancels: set = field(default_factory=set)
    # 외부 미체결 취소가 **수리되면** 그 주문에 기록할 잔량(주문번호 → 남길 수량).
    # 실행부가 창 누적 dict로 옮겨 다음 틱 유효 잔량에 쓴다(_eff_remain).
    trim_to: dict = field(default_factory=dict)
    # 목표 미달로 재실행을 요청한 심볼(창 누적 카운트용 — _MAX_CONVERGE_RERUNS).
    gap_symbols: list = field(default_factory=list)


def _signed(side, qty) -> int:
    return int(qty or 0) * (1 if str(side) == "buy" else -1)


def _unsettled(own_intents: list, now: datetime | None) -> bool:
    """이 심볼의 own 의도 중 **브로커 접수 반영 유예 안**인 것이 있나(_OWN_SETTLE_SEC).

    now=None(테스트 기본)이면 유예를 적용하지 않는다 — 기존 단위테스트의
    "시간 무관 판정" 계약을 유지하기 위함이고, 실행부는 항상 now를 넘긴다.
    accepted_ts 파싱 불가(구버전 저널·필드 부재)는 **유예 적용**(보수) — 나이를
    모르면 "충분히 늙었다"고 단정할 수 없다.
    """
    if now is None:
        return False
    for i in own_intents:
        ts = str(i.get("accepted_ts") or "")
        if not ts:
            return True
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            return True
        if age < _OWN_SETTLE_SEC:
            return True
    return False


def _eff_remain(p: dict, trimmed_to: dict) -> int:
    """외부 미체결의 **유효 잔량** — 이번 창 우리 취소 요청을 반영한 값.

    브로커 미체결 조회가 취소를 반영하는 데 10~20초 걸리는데(실측 2026-07-20:
    취소 후 t+10s 생존·t+20s 소멸) 가드 폴링은 10초다. 옛 잔량 그대로 초과분을
    다시 계산하면 같은 주문을 두 번 자른다 — 종전 전량취소는 재발행이 브로커
    거절("이미 취소")로 무해했지만 **부분취소는 거절되지 않고 실제로 더 취소된다**
    (과잉 개입).

    누적값은 "취소 수리 후 남기려는 잔량"이라 조회값과 **작은 쪽**을 취한다:
    미반영 구간(조회가 큼)엔 우리 기록이, 반영된 뒤(조회가 이미 줄어듦)엔 조회가
    진실이다. 누적을 "취소한 수량"으로 두고 조회값에서 빼는 형태는 반영 후 한 번
    더 빠져 D가 반대로 기울고(새 초과분 방치·반대편 과잉취소) 그 왜곡이 창 끝까지
    남는다 — 반영이 창 초반에 끝나므로 그게 오히려 정상 구간이다.
    """
    q = int(p.get("remain_qty") or 0)
    cap = trimmed_to.get(str(p.get("order_no")))
    return q if cap is None else max(0, min(q, cap))


def plan_guard_actions(positions_signed: dict, ledger_signed: dict,
                       own_intents: list, own_pendings: list,
                       ext_pendings: list,
                       restores_done: dict | None = None,
                       trimmed_to: dict | None = None,
                       now: datetime | None = None,
                       converge_reruns: dict | None = None,
                       target_symbols: set | None = None) -> GuardPlan:
    """한 틱의 조치 계획(순수 — IO 없음, 단위 테스트 대상).

    own_intents: 이번 창의 활성 자동 의도
        [{intent_id, strategy_id, symbol, side, qty, order_no, accepted_ts}]
    own_pendings / ext_pendings: 브로커 미체결 [{order_no, symbol, side, remain_qty}]
        — 호출자가 own/ext 분류를 마친 상태(오늘 전체 own 주문은 ext에서 제외).
    restores_done: 창 누적 심볼별 own 복원 횟수(상한 가드).
    trimmed_to: 창 누적 외부 주문별 "취소 수리 후 남기려는 잔량"(_eff_remain).
    now: 판정 기준 시각(KST). own 의도의 브로커 접수 반영 유예 판정에만 쓴다
        (_OWN_SETTLE_SEC). None이면 유예 미적용 — 실행부는 항상 넘긴다.
    """
    restores_done = restores_done or {}
    trimmed_to = trimmed_to or {}
    converge_reruns = converge_reruns or {}
    target_symbols = target_symbols or set()
    gap_symbols: list = []
    cancels: list = []
    partials: set = set()
    trim_to: dict = {}
    fails: list = []
    restored: list = []
    decisions: list = []
    rerun = False
    # 가드 우주 = own 의도 ∪ 원장 심볼 ∪ **이번 창이 목표를 확정한 심볼**(G2).
    # 비관리 심볼(엔진이 목표를 갖지 않는 보유·미등록 상품)은 여전히 불간섭이다 —
    # target_symbols는 이번 창 사이클이 실제로 목표를 세운 심볼만 담는다(§13
    # indeterminate는 build_symbol_plans가 제외).
    #
    # 세 번째 항이 필요한 이유: **"목표 0"과 "목표 없음"이 원장에서 같은 모습**이다.
    # 넷팅 book leg이 포지션을 지우면 원장 행이 사라지고 실주문도 0건이라 own 의도도
    # 없어, 목표가 0인 심볼이 우주 밖으로 떨어진다. A1이 인수한 수동 미체결이 취소되면
    # 바로 그 심볼에 물리 노출이 남는데 가드가 구조적으로 못 본다.
    syms = ({i["symbol"] for i in own_intents} | set(ledger_signed)
            | set(target_symbols))
    for sym in sorted(syms):
        oi = [i for i in own_intents if i["symbol"] == sym]
        op = [p for p in own_pendings if p["symbol"] == sym]
        exts_all = [p for p in ext_pendings if p["symbol"] == sym]

        # side 판독 불가 미체결(브로커 필드 미검증·"" 등) — 부호를 정할 수 없어
        # 검산 자체가 오염된다(G4ⓑ 비대칭). 이 심볼은 이번 틱 보류·관측만.
        if any(str(p.get("side")) not in ("buy", "sell") for p in op + exts_all):
            decisions.append(order_log.decision(
                "guard_skip_ambiguous", "", "", sym,
                "미체결 side 판독 불가 — 이 심볼 가드 보류(수렴 몫)"))
            continue

        oi_total = sum(_signed(i["side"], i["qty"]) for i in oi)
        # G3 — 드리프트 교정 의도(원장 불변)는 목표변에서 제외. 좌변(op)엔 포함.
        oi_strategy = sum(_signed(i["side"], i["qty"]) for i in oi
                          if not str(i.get("strategy_id", "")).startswith("DRIFT"))
        op_signed = sum(_signed(p["side"], p["remain_qty"]) for p in op)

        # ① 자동 주문 유저 변경 감지 — 의도(저널)와 브로커 잔여가 어긋남.
        if op_signed != oi_total:
            # 반영 유예 — 방금 접수된 own 주문은 미체결 조회에 아직 안 보일 수 있다.
            # 그 구간의 불일치는 "유저 취소"가 아니라 "미반영"이므로 판단을 미룬다.
            # 이 가드가 없으면 intent를 failed로 풀고 재실행해 **이중 발주**가 된다
            # (원주문은 살아있고 동시호가 단일가라 둘 다 체결 — 상세 _OWN_SETTLE_SEC).
            if _unsettled(oi, now):
                decisions.append(order_log.decision(
                    "guard_hold_unsettled", "", "", sym,
                    f"자동 주문 접수 반영 대기(의도 {oi_total:+d} vs 잔여 {op_signed:+d})"
                    f" — {_OWN_SETTLE_SEC:g}초 유예 내라 유저 취소로 단정하지 않음"))
                continue
            if restores_done.get(sym, 0) >= _MAX_RESTORES_PER_SYMBOL:
                decisions.append(order_log.decision(
                    "guard_own_giveup", "", "", sym,
                    f"자동 주문 복원 {_MAX_RESTORES_PER_SYMBOL}회 도달 — 창내 중단"
                    "(유저 의사 존중·잔여는 수렴 몫)"))
                continue
            for p in op:
                cancels.append((str(p["order_no"]), sym, int(p["remain_qty"] or 0)))
            for i in oi:
                fails.append((i["intent_id"],
                              "auction_guard: 자동 주문 유저 변경 감지 — 재발주"))
            decisions.append(order_log.decision(
                "guard_own_restore", "", "", sym,
                f"자동 주문 변경 감지(의도 {oi_total:+d} vs 잔여 {op_signed:+d})"
                " — 잔여 취소 후 창내 재발주"))
            restored.append(sym)
            rerun = True
            continue        # 복원 틱엔 외부 트리밍 보류 — 재발주로 목표 재정립 후 다음 틱

        # ② 외부(수동) 미체결 초과 — 최신 주문부터 초과분만 취소.
        # 잔량은 이번 창 우리 취소 요청을 반영한 **유효 잔량**으로 센다. 검산식 D의
        # 외부항도 같은 값이라야 이미 자른 몫이 D에서 자연히 빠져 추가 취소가 안
        # 나간다 — 개별 주문만 막고 D를 그대로 두면 같은 초과분이 다음 주문으로
        # 넘어가 과잉 취소가 재발한다.
        exts = [(p, _eff_remain(p, trimmed_to))
                for p in sorted(exts_all, key=lambda p: _sort_key(p.get("order_no")),
                                reverse=True)]
        ext_signed = sum(_signed(p["side"], q) for p, q in exts)
        d = ((positions_signed.get(sym, 0) + op_signed + ext_signed)
             - (ledger_signed.get(sym, 0) + oi_strategy))
        if d == 0:
            continue
        if not exts:
            # ③ 목표 미달 — D≠0인데 자를 외부 미체결이 없다. 우리 주문(op)도 없으면
            # 이 어긋남을 이 창에서 해소할 주체가 **아무도 없다**.
            #
            # §19 A1은 동시호가 수동 미체결을 "곧 체결될 보유"로 선반영한다. 그 전제로
            # 우리 청산이 넷팅(book)돼 실주문 0건이 되는데, 유저가 그 수동 주문을
            # 취소하면 정확히 이 형상이 된다 — 원장은 청산됐고 물리 보유는 그대로.
            # 아침창은 08:46 개장후 수렴이 교정하지만 **종가창은 장 종료(선물 15:45)로
            # 교정 기회가 없어**(§19.2 "익일 개장 carry") 창 안에서 재실행하지 않으면
            # 오버나이트가 확정된다. D는 이미 이 값을 정확히 계산하고 있었고, 종전
            # `not exts` 단축이 그것을 버리고 있었을 뿐이다.
            #
            # own 주문이 살아있으면 손대지 않는다 — 단일가에 체결될 몫이라 재발주는
            # 이중 발주 위험만 만든다(원주문·재발주가 같은 단일가에 둘 다 체결).
            if op:
                continue
            if converge_reruns.get(sym, 0) >= _MAX_CONVERGE_RERUNS:
                decisions.append(order_log.decision(
                    "guard_gap_giveup", "", "", sym,
                    f"목표 미달 재실행 {_MAX_CONVERGE_RERUNS}회 도달 — 창내 중단"
                    f"(어긋남 {d:+d} 잔존·수동 정리 필요)"))
                continue
            decisions.append(order_log.decision(
                "guard_uncovered_gap", "", "", sym,
                f"목표와 보유가 어긋났는데(D {d:+d}) 해소할 미체결이 없음 — 창내"
                " 재실행으로 목표 재수립(A1 인수 수동주문 취소 추정)"))
            gap_symbols.append(sym)
            rerun = True
            continue
        want = "buy" if d > 0 else "sell"
        remain = abs(d)
        for p, q in exts:
            if remain == 0:
                break
            if str(p["side"]) != want or q == 0:
                continue
            # 잔량이 초과분보다 크면 초과분만 부분취소 — 통째 취소는 과잉 개입,
            # 방치는 왕복 수수료·슬리피지. 부분취소 수리·원주문번호 유지는 실측
            # 확인(2026-07-20 LS CFOAT00300 · KIS 국내선물).
            n = min(q, remain)
            cancels.append((str(p["order_no"]), sym, n))
            trim_to[str(p["order_no"])] = q - n
            if n < q:
                partials.add(str(p["order_no"]))
            what = (f"{p['side']} {n} of {q} 부분취소" if n < q
                    else f"{p['side']} {n} 전량취소")
            decisions.append(order_log.decision(
                "guard_ext_trim", "", "", sym,
                f"목표 초과 수동 미체결 취소(최신 우선) — {what}"
                f" (주문 {p['order_no']})"))
            remain -= n
        if remain > 0:
            # 부분취소 도입 후 남는 잔여는 "같은 방향 수동 주문 고갈" 하나뿐이다
            # (초과분이 다음 주문 잔량보다 커서 멈추던 경우가 사라짐).
            decisions.append(order_log.decision(
                "guard_ext_residual", "", "", sym,
                f"수동 초과 잔여 {want} {remain} — 취소 가능한 같은 방향 수동"
                " 주문 소진 · 수렴 몫"))
    return GuardPlan(cancels, fails, restored, rerun, decisions, partials, trim_to,
                     gap_symbols)


def run_auction_guard(instrument_class: str, window: str,
                      start_hm: tuple, until_hms: tuple,
                      poll_sec: float = 10.0) -> dict:
    """한 동시호가 창을 감시·정합 — scheduler cron 진입점(창 종료까지 블로킹).

    instrument_class ∈ {"stock","futures"} · window ∈ {"open","close"} — 재실행을
    아침(run_cycle)/종가(run_close_cycle)로 라우팅. start_hm = 이 창의 자동 발주
    시작 시각(그 이후 own intent만 이번 창 소속), until_hms = 감시 종료(마감 단일가
    수십 초 전 — 취소 접수 여유).
    """
    import quant_core as qc
    from quant_core import market_calendar as mc

    from .analytics import norm_side
    from .runner import make_broker, run_close_cycle, run_cycle
    from .trader import _CYCLE_LOCK, Trader, _market_group_safe, kst_now, kst_today

    today = kst_today()
    try:
        if not mc.is_session_day("KR", today):
            log.info("KRX 휴장일 — auction guard skip (%s/%s)", window, instrument_class)
            return {"status": "skipped_holiday"}
    except Exception as e:
        # 캘린더 조회 불가(범위 밖 등) — 회차 가드(L-03)와 동일하게 차단하지 않음.
        log.warning("auction guard: 캘린더 판정 불가 — 감시 진행: %s", e)

    try:
        broker = make_broker()
    except Exception as e:
        log.warning("auction guard: broker 준비 실패 — skip: %s", e)
        return {"status": "no_broker"}
    if getattr(broker, "pending_orders", None) is None:
        log.info("auction guard: 미체결 조회 미지원 브로커 — skip")
        return {"status": "unsupported"}
    trader = Trader(broker)

    def _in_scope_sym(sym: str) -> bool:
        return (_market_group_safe(sym) == "KRX"
                and (instrument_class == "futures") == qc.is_futures(sym))

    def _pend_scope(p: dict) -> bool:
        mg = "KRX" if str(p.get("market") or "") in ("DOMESTIC", "KRX") else "US"
        return mg == "KRX" and str(p.get("asset_class") or "") == instrument_class

    # 창 중엔 단일가 체결 전이라 보유가 변하지 않는다 — 시작 스냅샷 고정.
    try:
        snap = broker.account_snapshot()
    except Exception as e:
        log.warning("auction guard: 잔고 스냅샷 실패 — skip: %s", e)
        return {"status": "no_snapshot"}
    if (snap.get("balance") or {}).get("fetch_failed"):
        # §14와 동일 보수 — 부분 스냅샷으론 목표·보유 대조가 오판할 수 있다.
        log.warning("auction guard: 잔고 부분 조회 — skip(수렴 몫)")
        return {"status": "partial_snapshot"}
    positions_signed: dict = {}
    for p in snap.get("positions") or []:
        sym = p.get("symbol") or ""
        if not sym or p.get("symbol_unmapped") or not _in_scope_sym(sym):
            continue
        q = int(p.get("qty") or 0)
        # G4ⓑ — KIS 선물 잔고 side는 "buy"/"sell"(LS는 long/short). norm_side로
        # 통일하지 않으면 숏이 +q로 계상돼 D가 2q 어긋난다.
        positions_signed[sym] = positions_signed.get(sym, 0) + (
            -q if norm_side(p.get("side")) == "short" else q)

    since_iso = datetime.combine(today, time(*start_hm), tzinfo=KST).isoformat()
    until = datetime.combine(today, time(*until_hms), tzinfo=KST)
    restores: dict = {}
    # 창 누적 심볼별 "목표 미달 재실행" 횟수 — restores와 같은 패턴(상한 가드).
    converge: dict = {}
    # 창 누적 "취소 수리 후 남기려는 잔량"(주문번호 → 수량) — restores와 같은 패턴의
    # 창 상태. 취소가 **수리된 뒤에만** 기록한다(거절분을 기록하면 반대로 과소 취소).
    trimmed_to: dict = {}
    all_decisions: list = []
    emitted: set = set()          # (symbol, action) — 반복 관측 결정 dedup(9f)
    n_cancel = 0

    def _classify(pend_rows, own_win_nos, all_own_nos):
        """미체결 3분류 — 이번 창 own / 오늘의 다른 창 own(불간섭) / 외부(수동).

        아침 잔존 자동 주문이 종가 창에서 ext로 오인·취소되던 결함(감사 9e) 봉합:
        오늘 저널의 모든 own 주문번호는 ext가 될 수 없다. 다른 창 own은 in-flight
        hold 규약(§14)대로 손대지 않는다(수렴·익일 회수 몫)."""
        own_p, ext_p = [], []
        for p in pend_rows:
            c = _canon(p.get("order_no"))
            if c and c in own_win_nos:
                own_p.append(p)
            elif c and c in all_own_nos:
                continue
            else:
                ext_p.append(p)
        return own_p, ext_p

    def _fetch_classified():
        # strict=True — 조회 실패를 빈/부분 목록으로 강등받지 않는다. 가드는 "미체결에
        # 없음"을 유저 취소로 읽으므로, 실패가 공집합으로 보이면 자기 주문을 취소된
        # 것으로 오판해 재발주한다(이중 발주). 예외는 호출자가 잡아 이번 틱을 보류.
        pend = [p for p in (broker.pending_orders(strict=True) or [])
                if _pend_scope(p)]
        own_list = [i for i in intents.submitted_window(today.isoformat(), since_iso)
                    if _in_scope_sym(i["symbol"])]
        own_win_nos = {_canon(i["order_no"]) for i in own_list if i["order_no"]}
        all_own_nos = {_canon(e.get("order_no"))
                       for e in intents._read_today(today.isoformat())
                       if e.get("order_no")}
        own_p, ext_p = _classify(pend, own_win_nos, all_own_nos)
        return own_list, own_p, ext_p

    gate_open = False
    while kst_now() < until:
        try:
            try:
                own_list, own_p, ext_p = _fetch_classified()
            except Exception as e:
                # 미체결 조회 실패 — 브로커 상태를 모르는 채로는 어떤 판정도 위험하다
                # (공집합으로 읽으면 own 오판·ext 과잉취소). 이번 틱 전면 보류.
                log.warning("guard 미체결 조회 실패 — 이번 틱 보류: %s", e)
                _time.sleep(poll_sec)
                continue
            _win_summary = _window_summary(today, window, instrument_class)
            if not gate_open:
                # 목표 확정 게이트 — 한번 열리면 창 끝까지 유지: own 복원이
                # intent를 failed로 돌린 뒤 재실행이 실패해 의도가 비어도, 이미
                # 확정된 목표 기준 정합을 계속한다(닫으면 복원 도중 무방비).
                gate_open = bool(own_list) or _win_summary is not None
            if not gate_open:
                # 사이클 미완료(발주 전 지연·백오프·전멸) — stale 원장을 목표로
                # 강제하지 않는다(수동 존중·관측만). 성공 시 A1 인수, 끝내 실패
                # 시 창밖 수동과 동일하게 수렴이 사후 정합.
                if ("", "guard_hold_no_target") not in emitted:
                    d = order_log.decision(
                        "guard_hold_no_target", "", "", "",
                        "이번 창 담당 사이클 미완료 — 목표 미확정: 개입 보류"
                        "(수동 존중·수렴 몫)")
                    all_decisions.append(d)
                    emitted.add(("", "guard_hold_no_target"))
                    log.warning("[가드] guard_hold_no_target — 목표 미확정, 개입 보류")
                _time.sleep(poll_sec)
                continue
            # 이번 창이 목표를 확정한 심볼 — 가드 우주에 더한다(원장 행이 없는
            # 목표 0까지 감시). 창 스코프 밖 심볼은 걸러 우주를 넓히지 않는다.
            _target_syms = {s for s in ((_win_summary or {}).get("target_symbols")
                                        or []) if _in_scope_sym(s)}
            trader.reload_state()
            ledger_signed: dict = {}
            for pos in trader.ledger.values():
                sym = pos["symbol"]
                if not _in_scope_sym(sym):
                    continue
                q = int(pos["qty"])
                ledger_signed[sym] = ledger_signed.get(sym, 0) + (
                    -q if pos.get("side", "long") == "short" else q)

            plan = plan_guard_actions(positions_signed, ledger_signed,
                                      own_list, own_p, ext_p, restores,
                                      trimmed_to, now=kst_now(),
                                      converge_reruns=converge,
                                      target_symbols=_target_syms)
            if plan.fail_intents:
                # 관측 race 봉합(감사 9c): 발주 직후 OMS 반영 지연으로 "저널엔
                # 있는데 미체결에 없음"이 유저 취소로 오인될 수 있다 — 한 번 더
                # 조회해 같은 판정일 때만 복원한다(거짓 복원=이중 발주).
                # ⚠ 즉시 재조회라 반영 지연 자체는 못 넘는다 — 그건 _OWN_SETTLE_SEC
                #    유예가 담당한다(이 재조회는 순간적 조회 흔들림만 거른다).
                own_list, own_p, ext_p = _fetch_classified()
                plan = plan_guard_actions(positions_signed, ledger_signed,
                                          own_list, own_p, ext_p, restores,
                                          trimmed_to, now=kst_now(),
                                          converge_reruns=converge,
                                          target_symbols=_target_syms)

            # 관측 결정은 조치 유무와 무관하게 기록(9f) — 반복류는 심볼당 1회.
            # emitted 마킹은 실제 기록 시점(하단)에만 — 실패 틱에 마킹만 되고
            # 기록이 유실되는 것 방지.
            fresh_dec = [d for d in plan.decisions
                         if d.get("action") not in _ONCE_ACTIONS
                         or (d.get("symbol"), d.get("action")) not in emitted]

            ok_all = True
            if plan.cancels or plan.fail_intents:
                with _CYCLE_LOCK:
                    for order_no, sym, qty in plan.cancels:
                        try:
                            r = broker.cancel(
                                order_no, sym, qty,
                                partial=order_no in plan.partial_cancels)
                        except Exception as e:
                            log.warning("guard 취소 실패 [%s 주문 %s]: %s",
                                        sym, order_no, e)
                            ok_all = False
                            break
                        # G1 — 어댑터는 거절을 예외가 아닌 {"success": False}로
                        # 반환한다(4개 브로커 공통 계약). 무시하면 옛 주문이
                        # 살아있는 채 재발주(이중 발주).
                        if isinstance(r, dict) and not r.get("success"):
                            log.warning("guard 취소 거절 [%s 주문 %s]: %s",
                                        sym, order_no, r.get("message", ""))
                            ok_all = False
                            break
                        # 수리된 취소만 창 누적에 기록 — 다음 틱이 이 몫을 뺀 유효
                        # 잔량으로 D를 세어 같은 초과분을 다시 자르지 않는다. 거절분을
                        # 기록하면 반대로 과소 취소(초과 방치)가 된다.
                        if order_no in plan.trim_to:
                            trimmed_to[order_no] = plan.trim_to[order_no]
                        n_cancel += 1
                    if ok_all and plan.fail_intents:
                        # 취소 "접수"≠"확정" — 재발주 전에 실제 소멸을 재확인.
                        # 남아 있으면 이번 틱 보류(다음 틱 재평가·이중 발주 차단).
                        # 부분취소분은 제외 — 원주문번호를 유지한 채 잔량만 줄어
                        # 살아남는 것이 정상 거동이라(실측 2026-07-20) 생존을
                        # "취소 미확정"으로 읽으면 같은 틱에 섞인 다른 심볼의 own
                        # 복원이 헛보류된다. 이 재확인의 목적은 재발주 전 이중
                        # 발주 차단인데 부분취소는 재발주를 동반하지 않는다.
                        full_cancels = [o for o, _s, _q in plan.cancels
                                        if o not in plan.partial_cancels]
                        try:
                            live = {_canon(p.get("order_no"))
                                    for p in (broker.pending_orders(strict=True) or [])
                                    if _pend_scope(p)}
                        except Exception as e:
                            log.warning("guard 취소 확인 조회 실패 — 이번 틱 보류: %s", e)
                            live = {_canon(o) for o in full_cancels}
                        if any(_canon(o) in live for o in full_cancels):
                            log.info("guard 취소 확정 대기 — 재발주 다음 틱으로 보류")
                            ok_all = False
                    if ok_all:
                        for iid, reason in plan.fail_intents:
                            intents.mark_failed(today.isoformat(), iid, reason)
            # 창 누적·재실행은 취소 분기 **밖**이다 — 종전엔 이 블록이 `if
            # plan.cancels or plan.fail_intents` 안에 중첩돼, 취소를 동반하지 않는
            # 재실행(목표 미달 보정)은 계획만 서고 실행되지 않았다. 브랜치 ①(own
            # 복원)은 항상 취소를 동반해 그 결함이 드러나지 않았다.
            if ok_all:
                for s in plan.restored_symbols:
                    restores[s] = restores.get(s, 0) + 1
                for gs in plan.gap_symbols:
                    converge[gs] = converge.get(gs, 0) + 1
                if plan.rerun:
                    try:
                        if window == "open":
                            run_cycle(market="KRX",
                                      instrument_class=instrument_class,
                                      trigger="auction_guard")
                        else:
                            run_close_cycle("KRX", instrument_class)
                    except Exception as e:
                        log.exception("guard 재실행 실패: %s", e)
            if ok_all and fresh_dec:
                all_decisions.extend(fresh_dec)
                for d in fresh_dec:
                    if d.get("action") in _ONCE_ACTIONS:
                        emitted.add((d.get("symbol"), d.get("action")))
                    log.warning("[가드] %s %s — %s", d.get("action"),
                                d.get("symbol"), d.get("reason"))
        except Exception as e:
            log.exception("auction guard tick 예외: %s", e)
        _time.sleep(poll_sec)

    if all_decisions:
        order_log.log_cycle(all_decisions, {
            "market": "KRX", "kind": "auction_guard", "window": window,
            "instrument_class": instrument_class, "n_cancel": n_cancel,
            "today": today.isoformat()})
    return {"status": "done", "n_cancel": n_cancel,
            "n_decisions": len(all_decisions)}
