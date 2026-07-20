"""자동매매 건강 모니터 — per-user · end-to-end 관측.

mwmw CON.parquet 인시던트(자동매매 설치 이래 발주 0이 며칠간 미탐지)의 근본은
자동매매 건강 텔레메트리가 `SyncSnapshot.payload`에 갇혀 운영자에게 안 보인 것이다.
이 모듈은 그 payload(+heartbeat·라이브 전략 수)를 읽어 유저별 MECE 건강 조건을
평가한다 — 침묵하는 총체적 실패(0발주·데이터결손·앱다운·동기화실패)를 신호등으로.

`admin_metrics`/`chat_analytics` 패턴 미러 — evaluate_health는 순수함수(입력→dict)라
hermetic 테스트가 가능하다(특히 mwmw 과거 스냅샷 replay). 보안 불변식: payload에서
읽는 키는 전부 안전정보(플래그·카운트·심볼·타임스탬프)뿐 — 자격증명·계좌번호·원시주문
미접근.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, func, select

from .models import (HealthAlertState, HeartbeatEvent, Strategy, SyncSnapshot,
                     User)

# 상태 등급 (신호등)
GREEN = "green"      # 정상
AMBER = "amber"      # 주의(대시보드 표시, 자동 페이징 안 함)
RED = "red"          # 이상(운영자 페이징 대상 — 단 alertable인 조건만)
UNKNOWN = "unknown"  # 신호 부재(판정 불가)

# 임계 상수 — 초기값. 운영 관측으로 튜닝(설계문서 §4).
STALE_MIN = 30         # heartbeat가 이 분(min) 넘게 끊기면 앱 다운 의심
PUSH_GAP_HR = 6        # heartbeat 정상인데 스냅샷 push가 이 시간(hr) 넘게 없으면 동기화 실패
LOAD_RATIO_RED = 0.5   # 데이터 로드율(loaded/needed)이 이 아래면 데이터 결손
_CONTENT_SCAN_DEPTH = 8  # 최신이 content-빈약(reconcile)일 때 직전 cycle을 찾아 훑는 최근 push 수
REF_DIVERGENCE_RED_BPS = 200    # 원장 진입가↔브로커 실평균 괴리 이 이상이면 참조가 오염(장부)
REF_DIVERGENCE_AMBER_BPS = 50   # 이 이상이면 주의(대시보드 표시)

# "실제 매매 사이클"로 간주하는 kind — 여기서 0발주+데이터결손이면 dead-man's-switch.
# state_sync·settlement·reconcile 등은 진입 사이클이 아니라 제외(정상 0발주).
_TRADING_CYCLE_KINDS = frozenset({"cycle", "day_trade", "day_trade_close"})


def _as_utc(ts: datetime | None) -> datetime | None:
    """naive는 UTC로 간주(_now가 UTC 저장), tz-aware는 그대로."""
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _cond(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail}


def evaluate_health(
    payload: dict | None,
    *,
    heartbeat_at: datetime | None,
    snapshot_at: datetime | None,
    live_strategies: int,
    now: datetime,
    content_payload: dict | None = None,
    prior: dict | None = None,
) -> dict:
    """유저 1명의 최신 스냅샷을 MECE 건강 조건으로 평가한다(순수함수).

    payload: SyncSnapshot.payload (최신 1건 = latest overall). heartbeat_at: 최신
    HeartbeatEvent.at. snapshot_at: 최신 SyncSnapshot.received_at. live_strategies:
    라이브 전략 수. now: 평가 시각(UTC). prior: 직전 평가 결과(전이 판정용, 선택).

    content_payload: **content 조건 전용** 페이로드 — diagnostics·cycle_summary·decisions·
    health 같은 '클라 매매 사이클이 실어 보내는' 신호의 출처. 최신 push가 reconcile/settlement/
    state_sync면 그 신호가 비어(=UNKNOWN degrade), 실제로는 직전 cycle 스냅샷에 건강한 값이
    있는데도 진단자가 못 보던 사각(2026-07-13 CON 오진의 관측성 근본)을 닫는다. compute_user_
    health가 'diagnostics 실린 최신 스냅샷'을 넘긴다. None이면 payload를 그대로 써 **기존과 동일**
    (하위호환). next_day_preview(C4)·reconciliation(C9)은 latest overall(=payload)에서 최신이라
    content_payload를 쓰지 않는다.

    반환: {conditions:{키:{status,detail}}, overall, alert_reasons:[{code,message}]}.
    alert_reasons = 캘린더 무관·고신뢰 RED만(운영자 페이징 대상). C1/C2(캘린더 민감)·
    C9(양성 잦음)는 대시보드엔 뜨되 자동 페이징 안 함.
    """
    p = payload or {}
    # content 조건은 cp(직전 cycle)에서, 시간·preview·reconcile은 p(latest overall)에서.
    cp = content_payload if content_payload is not None else p
    now = _as_utc(now)
    cs = cp.get("cycle_summary") or {}
    diag = cp.get("diagnostics") or {}
    conditions: dict[str, dict] = {}
    alerts: list[dict] = []

    has_live = live_strategies >= 1

    # ── C1 런타임 (앱 실행·heartbeat) ──
    hb = _as_utc(heartbeat_at)
    if not has_live:
        conditions["runtime"] = _cond(GREEN, "라이브 전략 없음 — 실행 대상 아님")
    elif hb is None:
        conditions["runtime"] = _cond(AMBER, "heartbeat 기록 없음")
    else:
        age_min = (now - hb).total_seconds() / 60
        if age_min > STALE_MIN:
            conditions["runtime"] = _cond(
                AMBER, f"heartbeat {age_min:.0f}분 전 — 앱/PC 꺼짐 가능")
        else:
            conditions["runtime"] = _cond(GREEN, f"heartbeat {age_min:.0f}분 전")

    # ── C2 연결 (스냅샷 push) ──
    sa = _as_utc(snapshot_at)
    if sa is None:
        conditions["connectivity"] = _cond(AMBER, "스냅샷 수신 이력 없음")
    else:
        gap_hr = (now - sa).total_seconds() / 3600
        hb_fresh = hb is not None and (now - hb).total_seconds() / 60 <= STALE_MIN
        if gap_hr > PUSH_GAP_HR and hb_fresh:
            conditions["connectivity"] = _cond(
                AMBER, f"push {gap_hr:.1f}시간 공백(heartbeat 정상) — 동기화 실패 가능")
        else:
            conditions["connectivity"] = _cond(GREEN, f"최근 push {gap_hr:.1f}시간 전")

    # ── C3 데이터 신선도 (번들 추출·로드율) ──
    bundle = diag.get("bundle") or {}
    dataset = diag.get("dataset") or {}
    data_red = False
    if not diag:
        conditions["data_freshness"] = _cond(
            UNKNOWN, "진단 미탑재(구버전 클라 또는 풀사이클 前)")
    elif bundle.get("result") == "failed":
        data_red = True
        conditions["data_freshness"] = _cond(
            RED, f"번들 추출 실패: {bundle.get('error_type') or bundle.get('error') or '?'}")
        alerts.append({"code": "bundle_failed", "message": conditions["data_freshness"]["detail"]})
    elif dataset:
        needed = int(dataset.get("needed") or 0)
        loaded = int(dataset.get("loaded") or 0)
        ratio = (loaded / needed) if needed else 1.0
        if needed > 0 and ratio < LOAD_RATIO_RED:
            data_red = True
            conditions["data_freshness"] = _cond(
                RED, f"데이터 로드율 {loaded}/{needed} ({ratio:.0%}) — 추출/신선도 결손")
        else:
            conditions["data_freshness"] = _cond(GREEN, f"데이터 로드 {loaded}/{needed}")
    else:
        conditions["data_freshness"] = _cond(GREEN, "번들 정상")

    # ── C7 판단 산출 (skip_no_data) ──
    # cycle_summary.n_skip_no_data 집계 우선(P2 승격 — decisions 리스트 없어도 판정 가능),
    # 없으면 decisions 스캔(구버전 하위호환).
    decisions = cp.get("decisions") or []
    cs_n_skip = cs.get("n_skip_no_data")
    skip_no_data_n = cs_n_skip if cs_n_skip is not None else len(
        [d for d in decisions if d.get("action") == "skip_no_data"])
    had_orders = (cs.get("n_bought") or 0) > 0 or any(
        d.get("action") in ("buy", "sell", "enter", "exit") for d in decisions)
    decision_red = False
    if skip_no_data_n and not had_orders:
        decision_red = True
        conditions["decision_output"] = _cond(
            RED, f"{skip_no_data_n}종목 데이터결손으로 skip_no_data(발주 판단 불가)")
    elif decisions or had_orders:
        conditions["decision_output"] = _cond(GREEN, "판단 정상 산출")
    else:
        conditions["decision_output"] = _cond(UNKNOWN, "결정 이력 없음(무후보일 가능)")

    # ── C6 사이클 실행 ──
    # error는 **latest-fresh 신호**다 — 정산/reconcile 잔고조회 실패가 최신 push(diagnostics 없어
    # content로 안 잡히는 kind)에 error를 실어 보내는데(runner.py Phase0), content(직전 cycle)만
    # 보면 그 error를 마스킹해 cycle_error 운영자 페이지가 소실된다. 그래서 latest(p)·content(cp)
    # **양쪽의 error**를 본다(어느 쪽 실패든 놓치지 않게). 완료 kind는 content(직전 실 cycle)가 유용.
    cyc_error = (p.get("cycle_summary") or {}).get("error") or cs.get("error")
    if cyc_error:
        conditions["cycle_execution"] = _cond(RED, f"사이클 에러: {cyc_error}")
        alerts.append({"code": "cycle_error", "message": conditions["cycle_execution"]["detail"]})
    elif cs.get("kind"):
        conditions["cycle_execution"] = _cond(GREEN, f"사이클 완료(kind={cs.get('kind')})")
    else:
        conditions["cycle_execution"] = _cond(UNKNOWN, "사이클 요약 없음")

    # ── C8 발주 + dead-man's-switch (핵심 복합 신호) ──
    kind = cs.get("kind")
    cycle_ran = kind in _TRADING_CYCLE_KINDS
    n_bought = cs.get("n_bought")
    no_orders = (n_bought or 0) == 0
    if has_live and cycle_ran and no_orders and (data_red or decision_red):
        conditions["order_placement"] = _cond(
            RED, "데이터/발주 실패 의심 — 매매 사이클 실행됐으나 데이터 결손으로 0발주")
        alerts.append({
            "code": "no_orders_data_starved",
            "message": "라이브 전략 있는데 매매 사이클이 데이터 결손으로 0발주 "
                       "(설치 이래 무발주 사고 시그니처)"})
    elif cs.get("n_rejected"):
        # R6 — 사유 라벨 첨부(저널 rejection_reasons top1): "거부 N건"만으론
        # 운영자가 원인(증거금·장마감·가격제한)을 원격 식별 못 하던 가시성 보강.
        _top = ((cp.get("rejection_reasons") or {}).get("reasons") or [{}])[0]
        _why = f" — {_top['label']}" if _top.get("label") else ""
        conditions["order_placement"] = _cond(
            AMBER, f"주문 거부 {cs.get('n_rejected')}건{_why}")
    elif (n_bought or 0) > 0:
        conditions["order_placement"] = _cond(GREEN, f"발주 {n_bought}건 체결")
    else:
        conditions["order_placement"] = _cond(GREEN, "발주 없음(정상 무후보 또는 진입창 밖)")

    # ── C12 발주 추적 정체 (pending 좀비 — R6) ──
    # 브로커가 거부/만료시켰는데 status 어휘 갭(R2)으로 종결 분류가 안 되면 pending이
    # submitted로 표류한다(mwmw 510 = 7일 좀비 실측). n_rejected(분류 성공 전제)와
    # 독립인 유일한 신호가 이 나이 — 24h 넘으면 AMBER, 72h 넘으면 RED(대시보드
    # 표시·자동 페이징 없음: C9와 같은 정책. 다음 GC·수동 정리 유도가 목적).
    _pend = p.get("pending_local") or []
    _now_epoch = now.timestamp()
    _ages_hr = [(_now_epoch - float(e.get("submitted_ts") or _now_epoch)) / 3600
                for e in _pend if isinstance(e, dict)]
    _stale_pend = [a for a in _ages_hr if a > 24]
    if not _pend:
        conditions["order_tracking"] = _cond(GREEN, "추적 중 미체결 없음")
    elif not _stale_pend:
        conditions["order_tracking"] = _cond(
            GREEN, f"추적 중 {len(_pend)}건 (전부 24h 이내)")
    else:
        _oldest_d = max(_stale_pend) / 24
        _st = RED if max(_stale_pend) > 72 else AMBER
        conditions["order_tracking"] = _cond(
            _st, f"미종결 발주 추적 {len(_stale_pend)}건 — 최고령 {_oldest_d:.1f}일 "
                 "(거부·만료가 미분류된 좀비 의심)")

    # ── C13 저널 기록 (orders.jsonl — R6) ──
    # append 실패·침묵의 무증상 삼킴 방지(07-14~17 mwmw 3일 공백 부류). 실패 누계는
    # 클라 재시작 시 리셋되는 세션 카운터 — 0이 아니면 지금 세션에서 실패가 실제
    # 발생 중이라는 뜻이라 그 자체로 신호.
    _journal = diag.get("journal") or {}
    _jf = int(_journal.get("append_failures") or 0)
    if _jf > 0:
        conditions["journal"] = _cond(
            AMBER, f"주문 저널 기록 실패 {_jf}건 — 마지막: "
                   f"{_journal.get('last_error') or '?'} (기록 유실 위험)")
    elif _journal:
        _last_ev = _journal.get("orders_last_event_ts")
        _traded = (n_bought or 0) + (cs.get("n_sold") or 0)
        if _traded > 0 and not _last_ev:
            conditions["journal"] = _cond(
                AMBER, "사이클 매매가 있는데 주문 저널이 비어 있음 — 동결 의심")
        else:
            conditions["journal"] = _cond(GREEN, "저널 기록 정상")
    # diag.journal 자체가 없으면(구버전 클라) 조건 미생성 — UNKNOWN 강요 대신 생략.

    # ── C5 브로커/계좌 준비 ──
    # P2 emit: health.broker_ready(bool)·active_broker + 기존 account_handles + 토큰경고(warnings).
    # 토큰 만료 판정은 클라가 자기 tz로 계산한 verdict(health.warnings)를 그대로 소비한다
    # — 서버가 naive-local 타임스탬프를 UTC로 재해석하면 어긋나므로(클라가 tz의 진실).
    health = cp.get("health") or {}
    broker_ready = health.get("broker_ready")
    handles = health.get("account_handles")
    token_warn = [w for w in (health.get("warnings") or []) if "토큰" in str(w)]
    if has_live and broker_ready is False:
        conditions["broker_ready"] = _cond(RED, "라이브 전략 있는데 브로커 미설정/자격증명 실패")
        alerts.append({"code": "broker_missing", "message": conditions["broker_ready"]["detail"]})
    elif has_live and broker_ready is None and handles is not None and len(handles) == 0:
        conditions["broker_ready"] = _cond(RED, "라이브 전략 있는데 브로커 계좌 미등록")
        alerts.append({"code": "broker_missing", "message": conditions["broker_ready"]["detail"]})
    elif broker_ready or handles:
        if token_warn:
            conditions["broker_ready"] = _cond(AMBER, f"브로커 준비됨 · {token_warn[0]}")
        else:
            conditions["broker_ready"] = _cond(GREEN, "브로커 준비됨")
    else:
        conditions["broker_ready"] = _cond(UNKNOWN, "브로커 상태 신호 없음(구버전 클라)")

    # ── C4 전략 유효성 (서버 preview 기반) ──
    # preview.skipped는 **보수적 투영**(실 발주 결정은 클라)이라 데이터 신선도(stale) 스킵도 포함한다.
    # 데이터 stale은 전략 무효가 아니다 — 특히 선물 연속물은 T+1 아침(08:10) 재구성이라 **저녁엔 항상
    # stale로 보이지만 08:55 사이클 전 해소**된다(main.py by-design). 진짜 데이터 문제는 클라
    # authoritative 신호(C7 skip_no_data·C8 0발주·dead-man's-switch)가 잡으므로, C4는 **구조적 전략
    # 무효**(유니버스 공집합·IR 파싱·참조 미해소)만 판정하고 stale-only 스킵은 무시한다 — 2026-07-13
    # mwmw 저녁 선물 stale이 '전략 무효 AMBER'로 오표시되던 false-positive 근본수정.
    prev = p.get("next_day_preview") or {}
    by_strat = prev.get("by_strategy") or []

    def _has_structural_skip(strat: dict) -> bool:
        """이 전략의 스킵 중 **데이터 신선도가 아닌**(=구조적 무효) 사유가 있나."""
        for sk in (strat.get("skipped") or []):
            reason = str(sk.get("reason", ""))
            if "stale" not in reason and "지연" not in reason:
                return True
        return False

    if has_live and by_strat:
        invalid = [s for s in by_strat if _has_structural_skip(s)]
        stale_only = [s for s in by_strat
                      if s.get("skipped") and not _has_structural_skip(s)]
        if invalid:
            conditions["strategy_validity"] = _cond(
                AMBER, f"라이브 전략 {len(invalid)}개 구조적 무효(유니버스·IR·참조) — preview skip")
        elif stale_only:
            # 전부 데이터 신선도 대기 — 전략 자체는 유효(선물은 아침 재구성 전 저녁 정상 상태).
            conditions["strategy_validity"] = _cond(
                GREEN, f"전략 {len(by_strat)}개 유효 (데이터 신선도 대기 — 전략 무효 아님)")
        else:
            conditions["strategy_validity"] = _cond(GREEN, f"전략 {len(by_strat)}개 preview 유효")
    else:
        conditions["strategy_validity"] = _cond(UNKNOWN, "preview 이력 없음")

    # ── C9 사후 정합 (reconcile drift — 대시보드 표시, 자동 페이징 안 함) ──
    # "확인했더니 정합"과 "확인한 적 없음"을 같은 GREEN으로 접으면 안 된다(2026-07-20):
    # 정합 신호는 15:50 정산 push에만 실려 실측 40스냅샷 중 9건뿐인데, 나머지 31건의
    # GREEN이 "원장↔브로커 정합"으로 읽혀 진단 근거로 인용됐다. 더 나쁜 건 실패 경로 —
    # 로컬이 잔고 조회 실패 시 `{"error": ...}`만 push하는데 그것도 GREEN이 됐다.
    # `checked_at`(analytics.reconcile_ledger가 항상 채움)의 부재가 "점검 미완료"의
    # 판별자다. UNKNOWN은 종합 등급·페이징을 건드리지 않아 장중 노이즈가 되지 않는다.
    rec = p.get("reconciliation") or {}
    extras = rec.get("external_extras") or []
    blocked = rec.get("reconcile_blocked") or {}
    if rec.get("error"):
        conditions["reconciliation"] = _cond(
            UNKNOWN, f"정합 점검 실패 — {rec['error']}")
    elif not rec.get("checked_at"):
        conditions["reconciliation"] = _cond(
            UNKNOWN, "정합 미점검 — 점검은 15:50 정산에 수행(장중 미점검이 정상)")
    elif blocked:
        # 로컬이 이 경우 has_drift도 True로 강제한다(웹훅 알림이 그 플래그에 의존).
        # 종전엔 아래 drift 분기에 걸려 "포지션 정합 drift(외부/미등록 0건)"로 렌더돼
        # 운영자가 원인을 오독했다 — 이건 drift가 아니라 **점검 자체가 불가**한 상태다.
        _why = ", ".join(filter(None, [
            f"잔고 조회 실패 {blocked.get('fetch_failed')}" if blocked.get("fetch_failed") else "",
            f"계약코드 미인식 {blocked.get('unmapped_codes')}" if blocked.get("unmapped_codes") else ""]))
        conditions["reconciliation"] = _cond(
            AMBER, f"정합 점검 불가 — 선물 신원계층 파손({_why})")
    elif rec.get("has_drift") or extras:
        conditions["reconciliation"] = _cond(
            AMBER, f"포지션 정합 drift(외부/미등록 {len(extras)}건)")
    else:
        conditions["reconciliation"] = _cond(
            GREEN, f"원장↔브로커 정합 (점검 {rec['checked_at']})")

    # ── C10 참조가 오염 (원장 진입가 ↔ 브로커 실평균 괴리) ──
    # stale 번들 봉을 참조가로 쓰면(trader freshness 가드 부재) 원장 entry_price가 브로커
    # 실체결 평균(avg_price)에서 벗어난다 — 2026-07-14 mwmw: 원장진입가 1144.03 vs 실평균
    # 1090.48(491bps)·stale peak 1210.5. 실주문은 시장가라 실체결은 정상이나 장부(진입가·
    # 실현손익·청산손익)가 오염된다. 기존 positions만으로 파생(로컬 emit 불요)·최신 overall(p)
    # 이 현재 포지션이라 p에서 읽는다(C9와 동일). 정밀한 사이클시점 ref↔live 경보는 별도(로컬 emit).
    worst = None  # (bps, symbol, entry, avg, peak)
    for pos in (p.get("positions") or []):
        entry = pos.get("entry_price")
        avg = pos.get("avg_price")
        if not entry or not avg or avg <= 0:
            continue                     # 한쪽 없거나 0 — 판정 불가(오탐·나눗셈 방지)
        bps = abs(entry - avg) / avg * 10000
        if worst is None or bps > worst[0]:
            worst = (bps, pos.get("symbol"), entry, avg, pos.get("peak_price"))
    if worst is None:
        conditions["ref_price_integrity"] = _cond(UNKNOWN, "포지션 참조가 신호 없음")
    else:
        bps, sym, entry, avg, peak = worst
        detail = (f"{sym} 원장진입가 {entry:g} vs 브로커실평균 {avg:g} = {bps:.0f}bps 괴리"
                  + (f"·peak {peak:g}" if peak else ""))
        if bps > REF_DIVERGENCE_RED_BPS:
            conditions["ref_price_integrity"] = _cond(RED, detail + " — 참조가 오염(장부)")
            alerts.append({"code": "ref_price_divergence",
                           "message": conditions["ref_price_integrity"]["detail"]})
        elif bps > REF_DIVERGENCE_AMBER_BPS:
            conditions["ref_price_integrity"] = _cond(AMBER, detail)
        else:
            conditions["ref_price_integrity"] = _cond(
                GREEN, f"{sym} 진입가↔실평균 정합({bps:.0f}bps)")

    # ── C11 목표 수렴 (target reconciliation — 대시보드 표시, 자동 페이징 안 함) ──
    # 목표모델(kr-target-reconciliation.md)의 새 신호를 운영자에게 노출한다 — payload에만
    # 갇히면 목표모델이 프로덕션에서 어떻게 도는지(수동매매 흡수·수렴 보류) 관측 불가
    # (2026-07-13 CON 오진과 같은 F1~F3 관측성 함정). drift 교정은 수동 개입 시 정상이라
    # 자동 페이징은 안 하되(C9 정합과 동일 정책), 규모·보류를 신호등으로 보이게 한다.
    #  · drift_deferred = 교정이 발주 못 됨(비-최근월물·현재가 부재) → 다음 사이클/수동 점검
    #  · skip_indeterminate/drift_eval_skipped = 판정불가로 수렴 보류(§13 hold)
    #  · n_drift = 이번 사이클 교정 계약수(수동매매/외부보유 정리 규모)
    n_drift = int(cs.get("n_drift") or 0)
    drift_deferred = [d for d in decisions if d.get("action") == "drift_deferred"]
    held = [d for d in decisions
            if d.get("action") in ("skip_indeterminate", "drift_eval_skipped")]
    if drift_deferred:
        conditions["target_convergence"] = _cond(
            AMBER, f"목표수렴 교정 보류 {len(drift_deferred)}건 — 비-최근월물/현재가 부재"
                   "(다음 사이클 재시도·수동 점검)")
    elif held:
        conditions["target_convergence"] = _cond(
            AMBER, f"수렴 보류 {len(held)}건 — stale/부분잔고/정규화실패로 hold(전량청산 아님)")
    elif n_drift > 0:
        conditions["target_convergence"] = _cond(
            AMBER, f"목표수렴 drift 교정 {n_drift}계약 — 수동매매/외부보유 정리(원장 불변)")
    elif cs.get("kind"):
        conditions["target_convergence"] = _cond(GREEN, "목표 상태 수렴 정상(drift 0)")
    else:
        conditions["target_convergence"] = _cond(UNKNOWN, "수렴 신호 없음(구버전 클라)")

    # ── 종합 ──
    statuses = [c["status"] for c in conditions.values()]
    if RED in statuses:
        overall = RED
    elif AMBER in statuses:
        overall = AMBER
    else:
        overall = GREEN

    return {"conditions": conditions, "overall": overall, "alert_reasons": alerts}


def compute_user_health(session: Session, *, now: datetime | None = None) -> list[dict]:
    """유저별 자동매매 건강 1회 스냅샷 — /admin/health · dead-man's-switch cron의 소스.

    라이브 전략 有 유저 ∪ 스냅샷 有 유저를 대상으로, **유저당 최신 스냅샷 1건만** 로드해
    egress를 억제한다(30일 전체 스캔 금지 — Neon egress 인시던트 교훈). 라이브 전략이
    있는데 스냅샷이 아예 없는 유저(설치했으나 한 번도 push 안 함)도 payload={}로 평가한다.
    """
    now = _as_utc(now or datetime.now(timezone.utc))

    # 라이브 전략 수 (유저별)
    live_counts = {
        uid: n for uid, n in session.exec(
            select(Strategy.user_id, func.count())
            .where(Strategy.run_mode == "live").group_by(Strategy.user_id)).all()
        if uid is not None}

    # 최신 heartbeat (유저별)
    hb: dict[int, datetime] = {}
    for uid, ts in session.exec(
            select(HeartbeatEvent.user_id, func.max(HeartbeatEvent.at))
            .group_by(HeartbeatEvent.user_id)).all():
        if uid is not None and ts is not None:
            hb[uid] = _as_utc(ts)

    # 최신 스냅샷 (유저당 최신 id 1건 — id는 삽입순 단조증가라 latest)
    latest_ids = [mx for _, mx in session.exec(
        select(SyncSnapshot.user_id, func.max(SyncSnapshot.id))
        .group_by(SyncSnapshot.user_id)).all() if mx is not None]
    snaps = {sn.user_id: sn for sn in session.exec(
        select(SyncSnapshot).where(SyncSnapshot.id.in_(latest_ids))).all()} if latest_ids else {}

    # content 조건(데이터·판단·발주·브로커)은 클라 매매 사이클이 실어 보내는 diagnostics/decisions/
    # health에서 온다. 최신 push가 reconcile/settlement/state_sync면 그 신호가 비어 UNKNOWN으로
    # degrade되는데, 실제로는 직전 cycle 스냅샷에 건강한 값이 있다(2026-07-13 CON 오진: 22:35
    # reconcile이 최신이라 data=UNK·CON 에러 비가시 → 진단자가 사고 증거를 못 봄). 그래서 최신이
    # diagnostics를 안 실었으면 'diagnostics 실린 최신 스냅샷'을 유저별로 따로 로드해(egress: 최신이
    # 이미 cycle이면 재로드 안 함) content 전용 페이로드로 넘긴다.
    # diagnostics 유무는 payload(JSON) 내부라 SQL 필터가 방언 의존적(SQLite/Postgres 상이)이다.
    # 방언 함정을 피하려 유저당 최근 _CONTENT_SCAN_DEPTH건만 로드해 파이썬에서 판별한다 —
    # cycle은 잦아 직전 cycle이 보통 몇 push 내에 있고, 없으면 content=None(=기존 UNKNOWN)로 안전
    # degrade. 최신이 이미 cycle인 유저는 재로드 안 함(egress 제한). cron/admin/CLI 냉경로라 무해.
    content_snaps: dict[int, SyncSnapshot] = {}
    _lacks_diag = [uid for uid, sn in snaps.items()
                   if not (sn.payload or {}).get("diagnostics")]
    for uid in _lacks_diag:
        for snx in session.exec(
                select(SyncSnapshot).where(SyncSnapshot.user_id == uid)
                .order_by(SyncSnapshot.id.desc()).limit(_CONTENT_SCAN_DEPTH)).all():
            if (snx.payload or {}).get("diagnostics"):
                content_snaps[uid] = snx
                break

    uids = set(live_counts) | set(snaps)
    emails = {u.id: u.email for u in session.exec(
        select(User).where(User.id.in_(uids))).all()} if uids else {}

    rows = []
    for uid in uids:
        sn = snaps.get(uid)
        # content 스냅샷 = 최신이 diagnostics를 가지면 그것, 아니면 diagnostics 실린 직전 스냅샷.
        csn = sn if (sn and (sn.payload or {}).get("diagnostics")) else content_snaps.get(uid)
        res = evaluate_health(
            sn.payload if sn else {},
            heartbeat_at=hb.get(uid),
            snapshot_at=_as_utc(sn.received_at) if sn else None,
            live_strategies=live_counts.get(uid, 0),
            now=now,
            content_payload=csn.payload if csn else None)
        cdiag = ((csn.payload or {}).get("diagnostics") or {}) if csn else {}
        rows.append({
            "user_id": uid,
            "email": emails.get(uid),
            "live_strategies": live_counts.get(uid, 0),
            "snapshot_at": _as_utc(sn.received_at).isoformat() if sn else None,
            # content_at = content 조건이 읽은 스냅샷 시각(최신과 다르면 '직전 cycle'). CLI/대시가
            # '데이터는 Nh 전 사이클 기준'을 표시해 stale 여부를 투명화(F3).
            "content_at": _as_utc(csn.received_at).isoformat() if csn else None,
            "heartbeat_at": hb[uid].isoformat() if uid in hb else None,
            "overall": res["overall"],
            "conditions": res["conditions"],
            "alert_reasons": res["alert_reasons"],
            # 원시 진단 노출(F1) — 진단자가 신호등만 보고 놓치던 raw 에러(예: CON.parquet WinError)를
            # 직접 본다. recent_errors는 클라가 emit측에서 scrub(경로·유저명)한 안전 요약이다.
            "recent_errors": cdiag.get("recent_errors") or [],
            "bundle": cdiag.get("bundle") or {},
            "dataset": cdiag.get("dataset") or {},
        })
    # RED 먼저, 그 다음 AMBER — 운영자가 문제 유저를 위에서 본다.
    _rank = {RED: 0, AMBER: 1, UNKNOWN: 2, GREEN: 3}
    rows.sort(key=lambda r: (_rank.get(r["overall"], 4), -(r["live_strategies"])))
    return rows


def _format_operator_alert(row: dict) -> str:
    who = row.get("email") or f"user {row['user_id']}"
    reasons = "\n".join(f"  · {a['message']}" for a in row["alert_reasons"])
    return (f"🚨 [Quant 운영] 자동매매 건강 이상 — {who}\n"
            f"  라이브 전략 {row['live_strategies']}개 · 상태 {row['overall'].upper()}\n"
            f"{reasons}\n"
            f"  → /admin 건강 패널에서 확인")


def scan_and_alert(session: Session, *, now: datetime | None = None,
                   send=None) -> dict:
    """전 유저 건강 평가 → 페이징 대상(alert_reasons)의 **전이 시에만** 운영자 알림.

    dead-man's-switch cron(main.py)이 주기 호출한다. on-ingest로는 못 잡는 "push 없는
    침묵 유저"까지 cron이 잡는다. send: callable(str)->bool(운영자 webhook 발송). None이면
    상태만 갱신(발송 안 함 — 테스트·webhook 미설정). 같은 alert 집합 반복은 재발송 안 함(스팸 방지).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    rows = compute_user_health(session, now=now)
    sent = 0
    for r in rows:
        codes = ",".join(sorted(a["code"] for a in r["alert_reasons"]))
        st = session.get(HealthAlertState, r["user_id"])
        prev = st.alert_codes if st else ""
        if codes and codes != prev:
            # 신규 또는 변경된 이상 → 발송(1회)
            if send is not None and send(_format_operator_alert(r)):
                sent += 1
            if st is None:
                st = HealthAlertState(user_id=r["user_id"])
            st.alert_codes = codes
            st.since = now
            st.last_alerted_at = now
            st.recovered_at = None
            session.add(st)
        elif not codes and st and st.alert_codes:
            # 회복 → 상태 클리어(회복은 조용히, 재이상 시 다시 발송되도록)
            st.alert_codes = ""
            st.since = None
            st.recovered_at = now
            session.add(st)
    session.commit()
    return {"evaluated": len(rows), "alerts_sent": sent}
