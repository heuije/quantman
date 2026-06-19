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
    """오늘 (sid, sym, side) intent가 submitting 또는 submitted 상태로 존재?

    cycle의 후보 루프 멱등 게이트. failed로 끝난 intent는 무시(재시도 허용).
    """
    by_intent = _group_by_intent(_read_today(date_iso, path=path))
    for iid, events in by_intent.items():
        seed = events[0]
        if seed.get("phase") != "submitting":
            continue
        if (seed.get("strategy_id") == strategy_id
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


# ── reconcile ─────────────────────────────────────────────────────────────────


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
    kr_rows: list | None
    if not hasattr(broker, "_daily_ccld"):
        # LS 등 _daily_ccld 미지원 브로커: KR reconcile skip.
        # submitting 게이트는 보수적으로 유지 — 정산 reconcile(15:50)이 백스톱.
        log.info(
            "브로커가 _daily_ccld 미지원(LS 등) — KR intent reconcile skip; "
            "submitting 게이트는 보수적 유지, 정산 reconcile 백스톱"
        )
        kr_rows = None
    else:
        try:
            body = broker._daily_ccld()  # noqa: SLF001 — reconcile은 raw row가 필요
            kr_rows = body.get("output1", []) or []
        except Exception as e:
            log.error("KR _daily_ccld 실패 — KR intent reconcile 보류: %s", e)
            kr_rows = None

    us_rows_cache: dict[str, list | None] = {}

    for intent in submitting:
        sym = intent["symbol"]
        is_us = market_index.is_us(sym)

        if is_us:
            if sym not in us_rows_cache:
                try:
                    us_rows_cache[sym] = broker._overseas_ccnl_today(sym)  # noqa: SLF001
                except Exception as e:
                    log.error("US _overseas_ccnl_today 실패 [%s]: %s", sym, e)
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
