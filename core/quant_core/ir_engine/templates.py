"""자동매매 템플릿 — 장중 신호 전략 화이트리스트의 선언·매처·스캔 파라미터 (단일 출처).

설계: docs/REDESIGN/intraday-template-redesign.md §2.2. 배경: 장중(실시간급) 신호 전략은
백테스트는 자유롭게 두되, 자동매매 연동은 라이브 배선(브로커 스캔 TR·실행창)이 사람 손으로
한 번 검증된 **템플릿 패턴**만 허용한다 — 임의 IR을 실행 계층이 몰래 번역하는 조용한
divergence를 원천 배제(보장은 검증이 아니라 설계로).

역할 분담(D3 — 이중 기재 드리프트 차단):
  · 전략 파라미터(임계·시장·상한)의 단일 출처 = **IR** → scan_params()로 추출.
  · 브로커/배포 사실(실행창·최소 앱버전·모의 지원)의 단일 출처 = **TEMPLATES 선언**.
소비자 3곳이 같은 판정을 공유한다: validate_strategy(S-template) · 서버 승격 게이트/preview ·
로컬앱 종가창 스캔 라우팅. 엔진은 template 태그를 읽지 않는다(태그 유/무 백테스트 동일).
"""

from __future__ import annotations

from typing import Any, Optional

from ..blocks.node import OP_CONST, OP_DATA, Node
from ..blocks.validate import SEV_ERROR, Issue
from ..expression_parser import market_match_values

LIMIT_UP_CLOSE = "limit_up_close_v1"

# 템플릿 선언 — 브로커·배포 사실만. paper(모의계좌)는 KIS 랭킹 TR이 모의 도메인 미지원이나
# 시세 호출 자체는 실전 도메인(quote_base)이라 가능 전망 — 실측 게이트(설계 §6 ⓐ) 통과
# 전까지 False 유지(fail-safe, 실측 후 이 상수만 갱신).
TEMPLATES: dict[str, dict[str, Any]] = {
    LIMIT_UP_CLOSE: {
        "label": "급등/상한가 마감형 오버나이트",
        "window": "krx_close_stock",       # 실행창: KRX 주식 종가창 15:25(장중·마감 동시호가)
        "min_app_version": "0.9.72",
        "threshold_pct": (20.0, 29.9),     # 등락률 임계 허용 범위(%)
        "brokers": {"kis": {"paper": False}, "ls": {"paper": False}},
    },
}

_KR_MARKETS = ("KOSPI", "KOSDAQ")


def _err(msg: str, path: str) -> Issue:
    return Issue("S-template", SEV_ERROR, msg, path)


def _threshold_of(signal: Node) -> Optional[float]:
    """정규형 신호 compare(>=|>, data(__SELF__.pct_change_1d), const(X))의 X(%). 아니면 None.

    정규형 강제가 파라미터 추출을 '패턴 추측'이 아닌 '판정'으로 만든다 — 로컬 스캔 필터와
    백테스트 신호가 같은 숫자를 읽는 근거. pct_change_1d는 % 단위 사전계산 지표(indicators.py).
    """
    if signal.op != "compare" or signal.params.get("op") not in (">=", ">"):
        return None
    left, right = signal.inputs.get("left"), signal.inputs.get("right")
    if left is None or right is None or left.op != OP_DATA or right.op != OP_CONST:
        return None
    if left.params.get("ref") != "__SELF__.pct_change_1d":
        return None
    v = right.params.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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


def template_issues(s) -> list[Issue]:
    """template 태그가 달린 StrategyIR의 패턴 정합(S-template) — validate_strategy가 호출.

    요건 = 라이브 배선이 검증된 조합 그대로: 당일 종가 매수(fill=close) · 익일 시가 청산
    (hold_days=1 + exit.fill=next_open) · 롱 · 이벤트 진입 · KRX 전 종목(Market 필터만 허용) ·
    정규형 급등 신호. 벗어나면 명시 거부 — NL repair 루프가 이 메시지로 교정한다.
    """
    t = s.template
    issues: list[Issue] = []
    if t is None:
        return issues
    spec = TEMPLATES.get(t.id)
    if spec is None:   # pydantic Literal이 먼저 막지만, 레지스트리-스키마 불일치를 방어 표면화
        issues.append(_err(f"미등록 템플릿 id: {t.id}", "template.id"))
        return issues

    if s.query != "simulate" or s.study.axis != "none":
        issues.append(_err("템플릿 전략은 단일 백테스트(query=simulate·study.axis=none) "
                           "전용입니다.", "query"))
    pos, sim, u = s.position, s.simulation, s.universe
    if pos.entry.mode != "on_signal":
        issues.append(_err("템플릿은 이벤트 진입(entry.mode=on_signal)이어야 합니다.",
                           "position.entry.mode"))
    if pos.direction != "long":
        issues.append(_err("이 템플릿은 롱 전용입니다.", "position.direction"))
    if sim.fill != "close":
        issues.append(_err("이 템플릿은 당일 종가 매수(simulation.fill=close)여야 합니다 — "
                           "종가창 스캔 진입과 백테스트가 같은 의미가 되도록.", "simulation.fill"))
    if pos.exit.hold_days != 1 or pos.exit.fill != "next_open":
        issues.append(_err("이 템플릿의 청산은 익일 시가(exit.hold_days=1 + "
                           "exit.fill=next_open)여야 합니다.", "position.exit"))
    if pos.exit.condition is not None:
        issues.append(_err("템플릿에서 매도조건(exit.condition)은 지원하지 않습니다 — "
                           "익절·손절·트레일은 허용.", "position.exit.condition"))
    if pos.sizing.mode not in ("pct_cash", "fixed_amount"):
        issues.append(_err("템플릿 사이징은 pct_cash 또는 fixed_amount만 지원합니다"
                           "(이벤트 진입 예산 경로).", "position.sizing.mode"))
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


def scan_params(s) -> dict[str, Any]:
    """매처를 통과한 템플릿 IR에서 로컬 스캔 파라미터 추출 — 단일 출처=IR.

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
