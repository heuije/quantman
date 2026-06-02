"""전략요소 → 데이터 요구 의존성 (M1.2).

임의 StrategyIR을 **구조만으로** 분해해 어떤 데이터 범주가 필요한지 토큰 집합으로 산출한다.
무결성 게이트(Phase 2)가 이 토큰을 DataSpec 피드·DataManifest 실측에 대응시켜
거부/경고/보정을 판정한다. 컬럼 *가용성* 자체(이 종목에 rsi_14가 있나)는 validate 규칙0가
이미 처리하므로 여기선 **무결성 차원**(시점·조정·캘린더·생존편향·커버리지)에 필요한 범주만 본다.

요구 토큰(범주):
  price.close/open/high/low · indicator.derived · fundamental · macro:<SYM> · sector
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..blocks.node import OP_DATA

if TYPE_CHECKING:                       # 런타임 import 결합 회피 — 덕타이핑으로 접근
    from ..ir_engine.spec import StrategyIR

_OHLCV = {"open", "high", "low", "close", "volume"}
_GROUP_OPS = {"group_rank", "group_aggregate", "group_neutralize"}


def _walk(node, fn) -> None:
    if node is None:
        return
    fn(node)
    for child in node.inputs.values():
        _walk(child, fn)


def _refs(nodes) -> set[str]:
    out: set[str] = set()
    for nd in nodes:
        _walk(nd, lambda n: out.add(str(n.params.get("ref", ""))) if n.op == OP_DATA else None)
    return out


def _uses_op(nodes, ops: set[str]) -> bool:
    hit = {"v": False}
    for nd in nodes:
        _walk(nd, lambda n: hit.__setitem__("v", hit["v"] or n.op in ops))
    return hit["v"]


def required_data(strategy: "StrategyIR") -> set[str]:
    """StrategyIR이 무결성상 요구하는 데이터 범주 토큰 집합."""
    from ..indicators import BASE_INDICATOR_COLS, FUND_INDICATOR_COLS   # 지연 import

    base, fund = set(BASE_INDICATOR_COLS), set(FUND_INDICATOR_COLS)
    pos, sim, sw, u = strategy.position, strategy.simulation, strategy.sweep, strategy.universe
    nodes = [strategy.signal, pos.exit.condition, sw.label, sw.event, pos.overlays.group_label]
    # 스크리너 선별 조건(필터+횡단순위 포함)의 데이터 참조도 무결성 검사 대상에 포함.
    if u.screener:
        from ..blocks.node import Node
        if u.screener.get("condition"):
            try:
                nodes.append(Node.model_validate(u.screener["condition"]))
            except Exception:                        # noqa: BLE001 — 잘못된 트리는 validate가 처리
                pass
    req: set[str] = {"price.close"}      # 어떤 백테스트도 종가는 필요

    # 체결·청산 → 시가/고저 (fill은 이벤트드리븐 on_signal 경로에서만 의미)
    if pos.entry.mode == "on_signal":
        if sim.fill == "next_open":
            req.add("price.open")
        elif sim.fill == "typical":
            req.update({"price.high", "price.low"})
    if sw.axis == "time" and sw.event_basis == "intraday":
        req.add("price.open")
    if pos.exit.trail_atr_mult is not None or pos.exit.trail_pct is not None:
        req.add("price.high")

    # 참조된 데이터 ref 분류 (가격/지표/펀더멘털/매크로)
    for r in _refs(nodes):
        if not r:
            continue
        sym, dot, col = r.partition(".")
        if dot and sym != "__SELF__":
            req.add(f"macro:{sym}")          # 외부 브로드캐스트(시장·매크로)
            continue
        c = col if dot else r                # "__SELF__.X" 또는 "X"
        cl = c.lower()
        if cl in _OHLCV:
            req.add(f"price.{cl}")
        elif c in base:
            req.add("indicator.derived")     # OHLCV 자체산출 — 조정수준에 종속
        elif c in fund:
            req.add("fundamental")           # P3 — PIT 필요

    # 그룹 블록 → 섹터 분류
    if _uses_op(nodes, _GROUP_OPS):
        req.add("sector")
    # 통화·시장(symbol_master)·거래캘린더·ATR은 엔진·validate가 직접 강제 — 무결성 게이트가
    # 소비하지 않는 토큰은 발행하지 않는다(소비자 없는 토큰 금지; membership 정리와 동일 원칙).
    # 스크리너 유니버스는 market_cap(시총) 랭킹 기반 — 지수 멤버십 데이터 불요(KR 멤버십 무료
    # 수급 불가). 생존편향은 gate D-surv가 u.kind로 직접 경고.
    return req
