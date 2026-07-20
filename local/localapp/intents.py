"""주문 멱등성을 위한 intent 저널 (L-01).

문제: trader._submit_buy는 broker.buy_limit 호출(자금 영향) 후 self.pending에 메모리
기록만 하고, cycle 끝의 _save()에서야 디스크에 쓴다. 그 사이 크래시 시 KIS엔 주문이
있는데 디스크엔 흔적이 없어 재기동 시 같은 후보를 또 매수 → 2배 포지션, 실 자금 손실.

해법(2-phase + reconcile):
  1) Phase A: broker.buy_limit 호출 *전*에 ``submitting`` 이벤트를 ``intents.jsonl``에
     append + fsync. 디스크 도달 보장.
  2) broker.buy_limit 호출. 성공 시 Phase B로 ``submitted``(+ order_no) append+fsync.
     실패 시 ``failed`` append+fsync.
  3) Cycle의 후보 루프는 ``is_active(date, sid, sym, side)``로 멱등 게이트.
  4) 재기동 시 ``reconcile_submitting(broker, date)``으로 submitting으로 끝난 intent를
     KIS 당일 주문 조회와 매칭 → 매칭되면 submitted(중복 발주 차단), 아니면 failed.

파일 형식: 한 줄 = JSON 객체. append-only. 파일별 원자성은 단일 라인 write+fsync에
의존(POSIX/NTFS 모두 단일 라인 append는 원자적).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import INTENTS_PATH
from .state_store import append_jsonl

log = logging.getLogger("localapp.intents")

# 가격 매칭 허용오차 (5%). 사용자 PC ref_price와 KIS 접수 시 ord_unpr가 약간
# 다를 수 있으므로(시간차·tick 단위 등) 보수적 매칭.
_PRICE_TOLERANCE = 0.05


def _now_kst_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).replace(microsecond=0).isoformat()


def new_intent_id() -> str:
    """클라이언트 측 intent 식별자 (UUID4 hex)."""
    return uuid.uuid4().hex


# ── append (fsync) ────────────────────────────────────────────────────────────


def _append_fsync(rec: dict, path: Path | None = None) -> None:
    """한 줄 append + fsync — state_store 단일 경로 위임 (R5, fsync=True).

    fsync로 디스크 도달까지 보장(전원 끊김 후에도 남아 중복 발주 차단, L-01).
    이전엔 여기서 직접 os.open/fsync만 하고 ACL이 없었다 — intents.jsonl은 원시
    주문 의도(종목·수량·가격)라 owner-only가 필요하다. append_jsonl이 최초 생성 시
    1회 ACL을 적용한다.
    """
    append_jsonl(rec, path or INTENTS_PATH, fsync=True)


def begin(date_iso: str, intent_id: str, strategy_id, strategy_name: str,
          symbol: str, side: str, qty: int, ref_price: float,
          path: Path | None = None) -> None:
    """Phase A — KIS 호출 *전*에 submitting 이벤트 기록."""
    _append_fsync({
        "ts": _now_kst_iso(), "date": date_iso, "phase": "submitting",
        "intent_id": intent_id,
        "strategy_id": strategy_id, "strategy_name": strategy_name,
        "symbol": symbol, "side": side,
        "qty": int(qty), "ref_price": float(ref_price),
    }, path=path)


def mark_submitted(date_iso: str, intent_id: str, order_no: str,
                   path: Path | None = None) -> None:
    """Phase B(성공) — KIS 응답 받은 직후."""
    _append_fsync({
        "ts": _now_kst_iso(), "date": date_iso, "phase": "submitted",
        "intent_id": intent_id, "order_no": str(order_no),
    }, path=path)


def mark_failed(date_iso: str, intent_id: str, error: str,
                path: Path | None = None) -> None:
    """Phase B(실패) — KIS 호출 자체가 raise한 경우."""
    _append_fsync({
        "ts": _now_kst_iso(), "date": date_iso, "phase": "failed",
        "intent_id": intent_id, "error": error,
    }, path=path)


def mark_resolved(date_iso: str, intent_id: str, outcome: str,
                  path: Path | None = None) -> None:
    """Phase C(종결) — 이 intent의 브로커 주문이 **더 이상 살아있지 않다**.

    종전엔 저널이 intent의 *끝*을 기록하지 않았다. ``submitted``는 "브로커가
    접수했다"는 뜻인데 ``is_active``가 이를 "아직 살아있다"로 읽어, 체결이 끝난
    주문이 종일 게이트를 점유했다. 그 결과 같은 전략이 하루에 두 번 같은 방향으로
    청산해야 하면 두 번째가 ``trader._plan_exit_intents``에서 심볼 통째 hold로
    막혔다 — 2026-07-20 floo.japan1 #30에서 코스닥150선물 438계약이 실제로
    청산되지 못하고 오버나이트로 넘어갔다.

    호출 지점 = **``trader.pending``에서 주문이 사라지는 곳**(체결·취소·거부·
    익일 회수·GC·WS 전량체결). ``_apply_fill`` 내부가 아니다 — drift 교정 체결은
    거기서 조기 return하므로(원장 불변) 훅이 영영 실행되지 않는다.

    **부분체결에는 쓰지 않는다.** 잔량이 살아있는데 게이트를 풀면 §19 A1이 자기
    주문을 목표 계산에서 제외하므로(자기 워시 방지) 그 잔량이 반영되지 않은 채
    같은 방향 추가 발주가 나간다.

    누락은 fail-safe다 — 종결을 못 쓰면 종전과 똑같이 보수적으로 차단된다.
    outcome: "filled" | "cancelled" | "reclaimed" | "gc" (관측용 사유).
    """
    _append_fsync({
        "ts": _now_kst_iso(), "date": date_iso, "phase": "resolved",
        "intent_id": intent_id, "outcome": outcome,
    }, path=path)


# ── read / status ─────────────────────────────────────────────────────────────


def _read_today(date_iso: str, path: Path | None = None) -> list[dict]:
    """오늘자(date_iso) 이벤트만 시간순으로 반환."""
    target = path or INTENTS_PATH
    if not target.exists():
        return []
    out: list[dict] = []
    with open(target, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                log.warning("intents.jsonl 파싱 실패 — 라인 skip")
                continue
            if rec.get("date") == date_iso:
                out.append(rec)
    return out


def _group_by_intent(events: list[dict]) -> dict[str, list[dict]]:
    """intent_id별로 이벤트를 모음 (시간순 유지)."""
    g: dict[str, list[dict]] = {}
    for ev in events:
        iid = ev.get("intent_id")
        if not iid:
            continue
        g.setdefault(iid, []).append(ev)
    return g


def _terminal_status(events_for_intent: list[dict]) -> str:
    """주어진 intent_id의 최종 phase. 마지막 이벤트의 phase가 답."""
    return events_for_intent[-1].get("phase", "unknown") if events_for_intent else "unknown"


def is_active(date_iso: str, strategy_id, symbol: str, side: str,
              path: Path | None = None) -> bool:
    """오늘 (sid, sym, side)로 낸 주문이 **아직 살아있는가**?

    cycle의 후보 루프 멱등 게이트. 활성 = ``submitting``(주문번호 미상 — 접수
    여부를 모르므로 보수적 차단) 또는 ``submitted``(접수 확인·미종결).
    게이트를 푸는 종결은 두 종류다:
      · ``failed``  — 주문이 생성되지 않았다(거부·미접수·가드 취소 확정).
      · ``resolved`` — 주문이 생성됐다가 죽었다(체결·취소·소멸). mark_resolved 참조.

    strategy_id는 문자열로 정규화해 비교한다 — 저널은 JSON이라 int 42로 기록된
    intent를 str "42"로 조회하면 원타입 ``==``에서 miss가 나 게이트가 조용히
    통과된다(이 게이트의 실패 방향은 이중 발주라 정규화가 안전 측).
    """
    by_intent = _group_by_intent(_read_today(date_iso, path=path))
    for iid, events in by_intent.items():
        seed = events[0]
        if seed.get("phase") != "submitting":
            continue
        if (str(seed.get("strategy_id")) == str(strategy_id)
                and seed.get("symbol") == symbol
                and seed.get("side") == side):
            if _terminal_status(events) in ("submitting", "submitted"):
                return True
    return False


def list_submitting_today(date_iso: str,
                          path: Path | None = None) -> list[dict]:
    """오늘자 ``submitting``으로 끝난 (= 아직 마감 안 된) intent의 seed 레코드."""
    by_intent = _group_by_intent(_read_today(date_iso, path=path))
    out: list[dict] = []
    for iid, events in by_intent.items():
        if _terminal_status(events) == "submitting":
            out.append(events[0])
    return out


def submitted_window(date_iso: str, since_iso_ts: str,
                     path: Path | None = None) -> list[dict]:
    """이번 발주창(seed ts ≥ since) 활성(submitted) intent 목록 — 동시호가 가드(#16) 소비.

    반환 [{intent_id, strategy_id, symbol, side, qty, ref_price, order_no, accepted_ts}]
    — submitted 상태(주문번호 보유·미해소)만. strategy_id는 가드 검산식이 목표수렴 드리프트
    의도("DRIFT:*" — 체결돼도 원장 불변)를 목표변에서 제외하는 데 쓴다(G3). failed(멱등 해제됨)와 submitting(주문번호 미상 — 발주 중/ambiguous라
    가드가 판단 불가)은 제외. ts 필터로 이전 창(예: 아침) 의도가 종가 가드에 섞이지
    않는다.

    `accepted_ts` = 주문번호를 받은(브로커 접수) 시각 ISO. 가드가 "브로커 미체결에
    안 보임"을 유저 취소로 단정하기 전 **접수 후 조회 반영 지연**을 배제하는 데 쓴다
    — 접수 직후엔 t0434에 아직 안 보이는 구간이 실측된다(2026-07-20: 취소 반영이
    t+10s엔 미반영·t+20s엔 반영). 종전 docstring은 "브로커 미체결에 없으면 유저
    취소로 확정할 수 있다"고 단정했는데, 그 전제가 이 지연 구간에서 깨진다."""
    by_intent = _group_by_intent(_read_today(date_iso, path=path))
    out: list[dict] = []
    for iid, events in by_intent.items():
        seed = events[0]
        if seed.get("phase") != "submitting":
            continue
        if str(seed.get("ts", "")) < since_iso_ts:
            continue
        if _terminal_status(events) != "submitted":
            continue
        # 주문번호와 그 번호를 받은 시각을 함께 — accepted_ts가 가드의 반영지연 배제 기준.
        _acc = next((ev for ev in reversed(events) if ev.get("order_no")), None)
        out.append({"intent_id": iid, "strategy_id": str(seed.get("strategy_id") or ""),
                    "symbol": seed.get("symbol", ""),
                    "side": seed.get("side", ""), "qty": int(seed.get("qty") or 0),
                    "ref_price": float(seed.get("ref_price") or 0),
                    "order_no": str(_acc["order_no"]) if _acc else "",
                    "accepted_ts": str((_acc or seed).get("ts") or "")})
    return out


# ── reconcile ─────────────────────────────────────────────────────────────────


# CY-2 — no_fill 확정의 intent 최소 나이(초). KIS 일별주문조회의 반영 지연 창
# 안에서 "무흔적=미접수"로 확정하면 멱등 게이트 해제 → 이중 발주 위험.
_NO_FILL_MIN_AGE_SEC = 60


def _intent_age_sec(intent: dict) -> float | None:
    """seed 레코드 ts(+09:00 ISO) 기준 나이(초). 파싱 불가면 None(하한 미적용)."""
    try:
        from datetime import datetime as _dt
        ts = _dt.fromisoformat(intent["ts"])
        return (_dt.now(ts.tzinfo) - ts).total_seconds()
    except Exception:
        return None


def _row_matches(row: dict, intent: dict, is_us: bool) -> bool:
    """KIS 당일 주문 row가 intent와 매칭되는가? symbol/side/qty/price 비교.

    KR (_daily_ccld output1 행): pdno, sll_buy_dvsn_cd("01"매도/"02"매수),
        ord_qty, ord_unpr, odno, cncl_yn.
    US (_overseas_ccnl_today 행): pdno, sll_buy_dvsn_cd, ft_ord_qty,
        ft_ord_unpr3 (또는 ord_unpr3), odno.

    취소된 주문은 매칭 제외(중복 발주 시 새 주문이 들어가야 하므로).
    """
    sym = row.get("pdno", "") or ""
    if sym != intent["symbol"]:
        return False

    side_cd = (row.get("sll_buy_dvsn_cd")
               or row.get("sll_buy_dvsn", "")
               or "")
    buy_match = intent["side"] == "buy" and side_cd in ("02", "2")
    sell_match = intent["side"] == "sell" and side_cd in ("01", "1")
    if not (buy_match or sell_match):
        return False

    if is_us:
        ord_qty = int(float(row.get("ft_ord_qty", 0) or 0))
        ord_px = float(row.get("ft_ord_unpr3", 0)
                       or row.get("ord_unpr3", 0)
                       or row.get("ord_unpr", 0) or 0)
        # US엔 cncl_yn이 표준이 아니므로 prcs_stat_name으로 취소 판정
        prcs = (row.get("prcs_stat_name", "") or "").strip()
        if "취소" in prcs or "거부" in prcs:
            return False
    else:
        ord_qty = int(float(row.get("ord_qty", 0) or 0))
        ord_px = float(row.get("ord_unpr", 0) or 0)
        if (row.get("cncl_yn", "") or "").upper() == "Y":
            return False

    if ord_qty != int(intent["qty"]):
        return False

    # 가격 근접성 — ref_price 또는 ord_px 중 하나라도 0이면 가격 비교 스킵
    # (시장가 주문은 ord_unpr=0). qty + symbol + side로만 매칭.
    if intent["ref_price"] > 0 and ord_px > 0:
        if abs(ord_px / intent["ref_price"] - 1) > _PRICE_TOLERANCE:
            return False
    return True


def reconcile_submitting(broker, date_iso: str,
                         path: Path | None = None) -> dict:
    """기동/cycle 시작 시 호출. submitting으로 끝난 오늘자 intent에 대해 KIS
    당일 주문 조회로 매칭 → submitted 또는 failed로 마감.

    매칭 결과:
      - 정확히 1건: ``submitted`` 마감 (order_no 기록). 재시도 차단.
      - 0건: ``failed`` 마감. 멱등 게이트 풀려 다음 cycle에서 정상 재시도.
      - 여러 건(모호): 보수적으로 ``submitted`` 마감 — 이중 발주 절대 차단.
      - KIS 조회 실패: 그대로 두고 다음 호출에서 재시도 (게이트 유지로 안전 측).

    반환: 카운트 + intent별 outcome.
    """
    from . import market_index
    submitting = list_submitting_today(date_iso, path=path)
    result = {"matched": 0, "no_fill": 0, "ambiguous": 0,
              "kis_query_failed": 0, "details": []}
    if not submitting:
        return result

    # KR은 한 번에 전 종목 조회. US는 종목별 조회.
    # R2-③ — 공개 seam(daily_orders_today) 사용: 종전 _daily_ccld 직접 의존은
    # BrokerRouter.__getattr__의 언더스코어 차단(broker_router.py) 때문에 선물
    # 라우터 구성 전 유저에서 hasattr=False → 이 백스톱이 구조적 dead였다.
    # 공개 이름은 라우터가 stock 브로커로 자동 위임하므로 구성 무관 동작.
    kr_rows: list | None
    if not hasattr(broker, "daily_orders_today"):
        # LS 등 미지원 브로커: KR reconcile skip.
        # submitting 게이트는 보수적으로 유지 — 정산 reconcile(15:50)이 백스톱.
        log.info(
            "브로커가 daily_orders_today 미지원(LS 등) — KR intent reconcile skip; "
            "submitting 게이트는 보수적 유지, 정산 reconcile 백스톱"
        )
        kr_rows = None
    else:
        try:
            kr_rows = broker.daily_orders_today()
        except Exception as e:
            log.error("KR daily_orders_today 실패 — KR intent reconcile 보류: %s", e)
            kr_rows = None

    us_rows_cache: dict[str, list | None] = {}

    for intent in submitting:
        sym = intent["symbol"]
        is_us = market_index.is_us(sym)

        if is_us:
            if sym not in us_rows_cache:
                # R2-③ 공개 seam 우선(라우터 관통) — 없으면 종전 프라이빗(하위호환).
                _us_fn = getattr(broker, "overseas_fills_today", None) \
                    or getattr(broker, "_overseas_ccnl_today", None)
                try:
                    us_rows_cache[sym] = _us_fn(sym) if _us_fn else None
                except Exception as e:
                    log.error("US 체결조회 실패 [%s]: %s", sym, e)
                    us_rows_cache[sym] = None
            rows = us_rows_cache[sym]
        else:
            rows = kr_rows

        if rows is None:
            result["kis_query_failed"] += 1
            result["details"].append({"intent_id": intent["intent_id"],
                                       "outcome": "kis_query_failed",
                                       "symbol": sym})
            continue  # submitting 그대로 — 게이트 유지(중복 발주 차단 측면 안전)

        matches = [r for r in rows if _row_matches(r, intent, is_us=is_us)]

        if len(matches) == 1:
            order_no = matches[0].get("odno", "") or ""
            mark_submitted(date_iso, intent["intent_id"], order_no, path=path)
            result["matched"] += 1
            result["details"].append({"intent_id": intent["intent_id"],
                                       "outcome": "matched",
                                       "order_no": order_no, "symbol": sym})
        elif len(matches) == 0:
            # R2/CY-2 — 나이 하한: 방금(60초 이내) 제출된 intent는 KIS 일별 조회
            # 반영 지연일 수 있어 no_fill 확정이 이르다. 확정하면 멱등 게이트가
            # 풀려 같은 창 재실행이 재발주 → 원주문이 뒤늦게 접수돼 있으면 이중
            # 발주. 라우터 위임(R2-③)으로 이 경로가 살아나는 만큼, 하한이 같은
            # PR에 있어야 활성화가 리스크를 켜지 않는다(리뷰 부작용 연계).
            _age = _intent_age_sec(intent)
            if _age is not None and _age < _NO_FILL_MIN_AGE_SEC:
                result["details"].append({"intent_id": intent["intent_id"],
                                           "outcome": "too_fresh",
                                           "age_sec": round(_age, 1),
                                           "symbol": sym})
                continue      # submitting 유지 — 다음 호출에서 재판정
            mark_failed(date_iso, intent["intent_id"],
                        "startup_reconcile_no_fill", path=path)
            result["no_fill"] += 1
            result["details"].append({"intent_id": intent["intent_id"],
                                       "outcome": "no_fill", "symbol": sym})
        else:
            # 모호 매칭 — 보수적으로 submitted (이중 발주 절대 차단). 사용자
            # 알림은 호출부에서 details를 보고 결정.
            ords = ",".join((m.get("odno", "") or "") for m in matches)
            mark_submitted(date_iso, intent["intent_id"], ords, path=path)
            result["ambiguous"] += 1
            result["details"].append({"intent_id": intent["intent_id"],
                                       "outcome": "ambiguous",
                                       "candidates": ords, "symbol": sym,
                                       "match_count": len(matches)})
            log.warning("intent reconcile 모호 매칭(%d건) — 보수적 submitted [%s]",
                        len(matches), sym)

    return result
