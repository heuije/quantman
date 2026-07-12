"""자동매매 템플릿 — 장중 신호 전략 화이트리스트의 선언·매처·스캔 파라미터 (단일 출처).

설계: docs/REDESIGN/intraday-template-redesign.md §2.2·§4. 배경: 장중(실시간급) 신호 전략은
백테스트는 자유롭게 두되, 자동매매 연동은 라이브 배선(브로커 스캔/감시·실행창)이 사람 손으로
한 번 검증된 **템플릿 패턴**만 허용한다 — 임의 IR을 실행 계층이 몰래 번역하는 조용한
divergence를 원천 배제(보장은 검증이 아니라 설계로).

역할 분담(D3 — 이중 기재 드리프트 차단):
  · 전략 파라미터(임계·시장/종목·상한)의 단일 출처 = **IR** → scan_params/watchlist_params로 추출.
  · 브로커/배포 사실(실행창·최소 앱버전·모의 지원)의 단일 출처 = **TEMPLATES 선언**.
소비자 3곳이 같은 판정을 공유한다: validate_strategy(S-template) · 서버 승격 게이트/preview ·
로컬앱(종가창 스캔 라우팅·장중 트리거 매니저). 엔진은 template 태그를 읽지 않는다(태그 유/무
백테스트 동일). 단 fill="trigger"의 정규형 신호 추출기(trigger_threshold_of)는 엔진도 공유 —
태그가 아니라 **신호 형태**가 체결 의미를 결정한다(D2 유지).
"""

from __future__ import annotations

from typing import Any, Optional

from ..blocks.node import OP_CONST, OP_DATA, Node
from ..blocks.validate import SEV_ERROR, Issue
from ..expression_parser import market_match_values

LIMIT_UP_CLOSE = "limit_up_close_v1"
WATCHLIST_TRIGGER = "watchlist_trigger_v1"

# 템플릿 선언 — 브로커·배포 사실만. paper(모의계좌)는 브로커별 스캔/시세 TR의 모의 가용성
# 실측 게이트(설계 §6 ⓐ) 통과 전까지 False 유지(fail-safe, 실측 후 이 상수만 갱신).
TEMPLATES: dict[str, dict[str, Any]] = {
    LIMIT_UP_CLOSE: {
        "label": "급등/상한가 마감형 오버나이트",
        "window": "krx_close_stock",       # 실행창: KRX 주식 종가창 15:25(장중·마감 동시호가)
        "min_app_version": "0.9.72",
        "threshold_pct": (20.0, 29.9),     # 등락률 임계 허용 범위(%)
        "brokers": {"kis": {"paper": False}, "ls": {"paper": False}},
    },
    WATCHLIST_TRIGGER: {
        "label": "워치리스트 장중 돌파 진입",
        "window": "krx_intraday",          # 실행창: KRX 장중 loop(09:00~15:30 감시·즉시 진입)
        "min_app_version": "0.9.73",
        "threshold_pct": (2.0, 29.9),      # 전일 종가 대비 돌파 임계(%)
        "max_symbols": 20,                 # 전략 1개의 워치리스트 상한
        # 유저(계정) 합산 감시 예산 — 서버 연동 게이트의 admission control 항(설계 §4):
        # KIS WS 등록 한도 41 − 보유 여유 11(청산 감시 우선 degrade). 초과 상태를 런타임에
        # 막는 게 아니라 연동 시점에 만들 수 없게 한다.
        "watch_budget": 30,
        "brokers": {"kis": {"paper": False}, "ls": {"paper": False}},
    },
}

_KR_MARKETS = ("KOSPI", "KOSDAQ")


def _err(msg: str, path: str) -> Issue:
    return Issue("S-template", SEV_ERROR, msg, path)


def _const_threshold(signal: Node, ref: str) -> Optional[float]:
    """정규형 compare(>=|>, data(ref), const(X))의 X. 형태가 다르면 None.

    정규형 강제가 파라미터 추출을 '패턴 추측'이 아닌 '판정'으로 만든다 — 라이브 필터와
    백테스트 신호가 같은 숫자를 읽는 근거(임계의 단일 출처 = 신호 const 하나).
    """
    if signal.op != "compare" or signal.params.get("op") not in (">=", ">"):
        return None
    left, right = signal.inputs.get("left"), signal.inputs.get("right")
    if left is None or right is None or left.op != OP_DATA or right.op != OP_CONST:
        return None
    if left.params.get("ref") != ref:
        return None
    v = right.params.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _threshold_of(signal: Node) -> Optional[float]:
    """마감형 정규형 — 종가 등락률(pct_change_1d, % 단위) 임계."""
    return _const_threshold(signal, "__SELF__.pct_change_1d")


def trigger_threshold_of(signal: Node) -> Optional[float]:
    """트리거 정규형 — 장중 도달(high_change_1d = 당일 High/전일 Close−1, %) 임계.

    fill="trigger"의 체결 경계가가 여기서 나온다(엔진 run_unified가 공유) — 종가 후퇴와
    무관하게 "장중 임계 도달"을 일봉으로 정직 판정하는 신호 형태.
    """
    return _const_threshold(signal, "__SELF__.high_change_1d")


def _market_values(screener: Optional[dict]) -> Optional[list[str]]:
    """universe.screener → 거래소 라벨 목록. 없으면 KRX 전체, Market is_in 형태면 정규화 값,
    그 외 스크리너 형태는 None(템플릿 비허용 — 임의 스크리너는 스캔으로 재현 불가)."""
    if not screener:
        return list(_KR_MARKETS)
    cond = screener.get("condition") if isinstance(screener, dict) else None
    if not isinstance(cond, dict) or cond.get("op") != "is_in":
        return None
    sig = (cond.get("inputs") or {}).get("signal") or {}
    if sig.get("op") != "attribute" or (sig.get("params") or {}).get("attr") != "Market":
        return None
    return market_match_values((cond.get("params") or {}).get("values") or [])


def _common_issues(s) -> list[Issue]:
    """전 템플릿 공통 요건 — 단일 백테스트·이벤트 진입·롱·이벤트 예산 사이징·매도조건 금지."""
    issues: list[Issue] = []
    if s.query != "simulate" or s.study.axis != "none":
        issues.append(_err("템플릿 전략은 단일 백테스트(query=simulate·study.axis=none) "
                           "전용입니다.", "query"))
    pos = s.position
    if pos.entry.mode != "on_signal":
        issues.append(_err("템플릿은 이벤트 진입(entry.mode=on_signal)이어야 합니다.",
                           "position.entry.mode"))
    if pos.direction != "long":
        issues.append(_err("이 템플릿은 롱 전용입니다.", "position.direction"))
    if pos.exit.condition is not None:
        issues.append(_err("템플릿에서 매도조건(exit.condition)은 지원하지 않습니다 — "
                           "익절·손절·트레일은 허용.", "position.exit.condition"))
    if pos.sizing.mode not in ("pct_cash", "fixed_amount"):
        issues.append(_err("템플릿 사이징은 pct_cash 또는 fixed_amount만 지원합니다"
                           "(이벤트 진입 예산 경로).", "position.sizing.mode"))
    return issues


def _limit_up_issues(s, spec: dict) -> list[Issue]:
    """마감형 요건 — 당일 종가 매수·익일 시가 청산·KRX 전 종목(Market 필터만)·정규형 등락률."""
    issues: list[Issue] = []
    pos, sim, u = s.position, s.simulation, s.universe
    if sim.fill != "close":
        issues.append(_err("이 템플릿은 당일 종가 매수(simulation.fill=close)여야 합니다 — "
                           "종가창 스캔 진입과 백테스트가 같은 의미가 되도록.", "simulation.fill"))
    if pos.exit.hold_days != 1 or pos.exit.fill != "next_open":
        issues.append(_err("이 템플릿의 청산은 익일 시가(exit.hold_days=1 + "
                           "exit.fill=next_open)여야 합니다.", "position.exit"))
    if u.kind != "all" or u.symbols:
        issues.append(_err("템플릿 유니버스는 kind=all(전 종목 스캔)이어야 합니다.", "universe"))
    else:
        mkts = _market_values(u.screener)
        if mkts is None:
            issues.append(_err("템플릿 스크리너는 시장(Market) 필터만 허용합니다.",
                               "universe.screener"))
        elif not mkts or not set(mkts) <= set(_KR_MARKETS):
            issues.append(_err("이 템플릿은 KRX(KOSPI·KOSDAQ) 전용입니다.",
                               "universe.screener"))
    thr = _threshold_of(s.signal)
    lo, hi = spec["threshold_pct"]
    if thr is None:
        issues.append(_err("템플릿 신호는 정규형이어야 합니다: "
                           "compare(>=, data(__SELF__.pct_change_1d), const(임계%)).", "signal"))
    elif not (lo <= thr <= hi):
        issues.append(_err(f"등락률 임계는 {lo}~{hi}% 범위여야 합니다(현재 {thr:g}%).", "signal"))
    return issues


def _watchlist_issues(s, spec: dict) -> list[Issue]:
    """워치리스트형 요건 — trigger 체결·지정 KR 종목 1~N·보유일 1~10·정규형 장중 도달 임계."""
    issues: list[Issue] = []
    pos, sim, u = s.position, s.simulation, s.universe
    if sim.fill != "trigger":
        issues.append(_err("이 템플릿은 장중 돌파 체결(simulation.fill=trigger)이어야 합니다 — "
                           "장중 감시 진입과 백테스트가 같은 의미가 되도록.", "simulation.fill"))
    max_n = int(spec["max_symbols"])
    if u.kind != "list" or not (1 <= len(u.symbols) <= max_n):
        issues.append(_err(f"워치리스트는 지정 종목 1~{max_n}개(kind=list)여야 합니다 — "
                           "브로커 감시 예산(WS 슬롯)의 정적 보장 조건.", "universe"))
    elif not all(x.isdigit() and len(x) == 6 for x in u.symbols):
        issues.append(_err("워치리스트는 KRX 개별 주식(6자리 코드)만 지원합니다.",
                           "universe.symbols"))
    if u.screener:
        issues.append(_err("워치리스트 템플릿은 스크리너를 지원하지 않습니다(종목 직접 지정).",
                           "universe.screener"))
    hd = pos.exit.hold_days
    if hd is None or not (1 <= hd <= 10):
        issues.append(_err("이 템플릿은 보유일수 1~10일이 필요합니다(익절·손절·트레일은 "
                           "추가 허용 — 시간 상한이 안전 기본).", "position.exit.hold_days"))
    thr = trigger_threshold_of(s.signal)
    lo, hi = spec["threshold_pct"]
    if thr is None:
        issues.append(_err("템플릿 신호는 정규형이어야 합니다: "
                           "compare(>=, data(__SELF__.high_change_1d), const(임계%)).", "signal"))
    elif not (lo <= thr <= hi):
        issues.append(_err(f"돌파 임계는 {lo}~{hi}% 범위여야 합니다(현재 {thr:g}%).", "signal"))
    return issues


def template_issues(s) -> list[Issue]:
    """template 태그가 달린 StrategyIR의 패턴 정합(S-template) — validate_strategy가 호출.

    요건 = 라이브 배선이 검증된 조합 그대로. 벗어나면 명시 거부 — NL repair 루프가
    이 메시지로 교정한다(조용한 divergence 금지).
    """
    t = s.template
    if t is None:
        return []
    spec = TEMPLATES.get(t.id)
    if spec is None:   # pydantic Literal이 먼저 막지만, 레지스트리-스키마 불일치를 방어 표면화
        return [_err(f"미등록 템플릿 id: {t.id}", "template.id")]
    issues = _common_issues(s)
    if t.id == LIMIT_UP_CLOSE:
        issues += _limit_up_issues(s, spec)
    elif t.id == WATCHLIST_TRIGGER:
        issues += _watchlist_issues(s, spec)
    return issues


def scan_params(s) -> dict[str, Any]:
    """마감형(limit_up_close_v1) 로컬 스캔 파라미터 — 단일 출처=IR.

    반환: {"threshold_pct": float, "markets": [KOSPI|KOSDAQ,…], "max_entries": int}.
    정규형이 아니면 ValueError — validate_strategy(S-template) 통과가 선행 계약이므로
    여기 도달하면 프로그래밍 오류다(조용한 기본값 대체 금지).
    """
    thr = _threshold_of(s.signal)
    if thr is None or s.template is None:
        raise ValueError("템플릿 정규형 IR이 아닙니다 — validate_strategy(S-template) 선행 필요")
    mkts = _market_values(s.universe.screener)
    if not mkts:
        raise ValueError("템플릿 시장 필터가 유효하지 않습니다 — S-template 선행 필요")
    return {"threshold_pct": thr, "markets": mkts,
            "max_entries": s.template.max_daily_entries}


def watchlist_params(s) -> dict[str, Any]:
    """워치리스트형(watchlist_trigger_v1) 로컬 감시 파라미터 — 단일 출처=IR.

    반환: {"threshold_pct": float, "symbols": [6자리 코드,…], "max_entries": int}.
    """
    thr = trigger_threshold_of(s.signal)
    if thr is None or s.template is None or not s.universe.symbols:
        raise ValueError("템플릿 정규형 IR이 아닙니다 — validate_strategy(S-template) 선행 필요")
    return {"threshold_pct": thr, "symbols": list(s.universe.symbols),
            "max_entries": s.template.max_daily_entries}
