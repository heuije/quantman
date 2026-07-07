"""param_manifest — IR의 '조정 가능한 변수' 매니페스트 (실시간 변수조정용).

챗봇/빌더가 분석 결과를 줄 때 이 매니페스트를 함께 실어, 유저가 채팅창에서 핵심 변수값을
실시간으로 바꾸면(엑셀 템플릿의 독립변수처럼) 웹이 IR을 그 값으로 재구성→`/ir/strategy`
재실행(LLM 없음·토큰 0)→차트 갱신한다. 매번 챗 재요청(토큰 낭비) 대신.

이 함수는 spec(SSOT)의 *현재 IR에 의미 있는* 수치/enum 노브만 골라 {path·label·type·value·
범위}로 노출한다(없는 30개 필드 나열 금지 — over-engineering 회피). path는 점경로라 웹이
setPath로 IR을 재구성한다.
"""
from __future__ import annotations

from typing import Any

from ..backtest import _DEFAULT_COMMISSION, _DEFAULT_SLIPPAGE
from .spec import StrategyIR


def param_manifest(ir: StrategyIR | dict) -> list[dict[str, Any]]:
    """조정 가능한 변수 목록. 각 항목: {path, label, type(number|select|bool), value, ...범위}.

    범위 키 — number: min·max·step / select: options. 빈 리스트면 웹이 패널을 안 띄운다.
    """
    d = ir.model_dump() if hasattr(ir, "model_dump") else dict(ir)
    out: list[dict[str, Any]] = []

    def add(path: str, label: str, typ: str, value: Any, **extra: Any) -> None:
        out.append({"path": path, "label": label, "type": typ, "value": value, **extra})

    q = d.get("query", "simulate")
    pos = d.get("position") or {}
    ent = pos.get("entry") or {}
    ex = pos.get("exit") or {}
    sz = pos.get("sizing") or {}
    sim = d.get("simulation") or {}
    study = d.get("study") or {}

    if q == "simulate":
        # 진입 — 스케줄 리밸런싱 노브
        if ent.get("mode") == "scheduled":
            if ent.get("top_n") is not None:
                add("position.entry.top_n", "보유 종목 수 (top_n)", "number", ent["top_n"], min=1, max=50, step=1)
            add("position.entry.rebalance", "리밸런스 주기", "select", ent.get("rebalance", "weekly"),
                options=["daily", "weekly", "monthly", "quarterly", "annual"])
        if ent.get("threshold") is not None:
            add("position.entry.threshold", "임계값 (threshold)", "number", ent["threshold"], step=0.1)
        # 사이징 — 종목당 예산(%) (이벤트 진입)
        if sz.get("mode") in ("pct_cash", "fixed_amount") and sz.get("amount_pct") is not None:
            add("position.sizing.amount_pct", "종목당 투입 (%)", "number", sz["amount_pct"], min=1, max=100, step=1)
        # 청산 규칙 — 설정된 것만
        for f, lbl, mn, mx, st in (("hold_days", "보유기간 (일)", 0, 250, 1),
                                   ("take_profit", "익절 (%)", 0.5, 100, 0.5),
                                   ("stop_loss", "손절 (%)", -100, -0.5, 0.5),
                                   ("trail_pct", "트레일링 스톱 (%)", 0.5, 100, 0.5)):
            if ex.get(f) is not None:
                add(f"position.exit.{f}", lbl, "number", ex[f], min=mn, max=mx, step=st)
        # 비용·자본 — 엑셀 노란셀 격(항상 노출). None이면 엔진 기본값을 시작값으로(실효값 표기).
        add("simulation.commission", "수수료 (분수·0.0003=0.03%)", "number",
            sim.get("commission") if sim.get("commission") is not None else _DEFAULT_COMMISSION,
            min=0, max=0.01, step=0.0001)
        add("simulation.slippage", "슬리피지 (분수)", "number",
            sim.get("slippage") if sim.get("slippage") is not None else _DEFAULT_SLIPPAGE,
            min=0, max=0.01, step=0.0001)
        add("simulation.initial_capital", "초기 자본 (₩)", "number",
            sim.get("initial_capital") or 100_000_000.0, min=1_000_000, max=10_000_000_000, step=1_000_000)
        if sim.get("leverage") and sim["leverage"] != 1.0:
            add("simulation.leverage", "레버리지", "number", sim["leverage"], min=1, max=10, step=0.5)
        # 기간분할 구간 수(달력 주기 split_period면 데이터가 정하므로 노출 안 함)
        if study.get("axis") == "time_fold" and not study.get("split_period"):
            add("study.folds", "기간 구간 수 (folds)", "number", study.get("folds", 4), min=2, max=12, step=1)

    elif q == "select":
        sel = d.get("select") or {}
        if sel.get("top_n") is not None:
            add("select.top_n", "상위 N 종목", "number", sel["top_n"], min=1, max=100, step=1)
        add("select.descending", "내림차순 (큰 값 우선)", "bool", bool(sel.get("descending", True)))

    elif q == "relate":
        # 이벤트 스터디·코호트(#C P2·D5): 이벤트 조건의 const 임계를 값 조정 노브로 — 이전엔 relate가
        # 브랜치 없어 adjustable=[]였다(프로덕션 실측: adjust_analysis 'study.window.end' → '가능:[]'라
        # 재컴파일 강제·비결정 발산). 임계만 바꿔 재실행(LLM·재컴파일 없음)하면 결정적이다.
        for i, t in enumerate(_collect_event_thresholds(study.get("event")), 1):
            lbl = f"이벤트 임계: {t['ref']}" if t["ref"] else f"이벤트 임계 {i}"
            add(t["path"], f"{lbl}", "number", t["value"], step=0.5)

    return out


def _collect_event_thresholds(node: Any, path: str = "study.event") -> list[dict[str, Any]]:
    """study.event 트리에서 compare의 상수 임계(const)를 수집 → [{path, value, ref}].

    이벤트 조건은 임의 블록트리라, compare 노드의 const 쪽을 조정 대상으로 노출한다(반대쪽 data ref로
    라벨). 웹 setPath가 이 점경로로 IR을 재구성해 재실행한다(재컴파일 없음). 단일·다조건(AND/OR) 모두 처리.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return out
    if node.get("op") == "compare":
        inp = node.get("inputs") or {}
        for side, other_key in (("right", "left"), ("left", "right")):
            c = inp.get(side) or {}
            if c.get("op") == "const":
                v = (c.get("params") or {}).get("value")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    other = inp.get(other_key) or {}
                    ref = (other.get("params") or {}).get("ref", "") if other.get("op") == "data" else ""
                    out.append({"path": f"{path}.inputs.{side}.params.value", "value": v,
                                "ref": ref.split(".")[-1] if ref else ""})
                    break                          # compare당 임계 1개(양쪽 const는 비정형이라 첫쪽만)
    for k, v in (node.get("inputs") or {}).items():
        out += _collect_event_thresholds(v, f"{path}.inputs.{k}")
    return out
