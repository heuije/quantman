"""전략 조합 — 저장된 전략의 자산곡선을 합성 심볼(strat:<id>)로 dataset에 주입.

조합 폐쇄성(compositional closure): 엔진의 출력(equity)을 입력(가격 시리즈) 공간에 넣는다.
그 순간 자산에 동작하던 모든 원자 — universe·rank·sizing·오버레이·분석(IC·국면) — 가 전략에도
그대로 들어올림(lift)된다. 새 트리 구문 0: 기존 크로스에셋 데이터-참조 원자(SYM.Close)를
재사용한다. 'strat:value_ls.Close' = 그 전략의 자산곡선.

  - F3 팩터모멘텀 = 합성 팩터-심볼들에 대한 평범한 로테이션 전략(top_n=1 by 추세).
  - 전략 간 리스크패리티 = vol_inverse over [strat:A, strat:B, ...].
  - 전략 상관 = ts_corr(strat:A, strat:B) — 내 알파들의 동조화 측정.

core 순수성: 저장 전략 조회는 server(Postgres) 소유 → resolver 콜백 주입(core가 server를
import 하지 않는다). 자식 equity는 인과적(자식 백테스트가 t 이하 데이터만 사용)이라 부모가
strat:X.Close[t]를 t에 쓰는 것은 look-ahead가 아니다.
"""

from __future__ import annotations

import pandas as pd

from ..blocks import referenced_symbols
from ..blocks.node import Node
from .run import run_strategy_ir
from .spec import StrategyIR

STRAT_PREFIX = "strat:"


def _equity_to_frame(equity: pd.Series) -> pd.DataFrame:
    """자산곡선 → 합성 OHLCV(Close=equity). 일중 정보 없으니 O=H=L=C, Volume=0."""
    c = pd.Series(equity, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 0.0},
                        index=c.index)


def collect_strat_refs(ir: StrategyIR) -> set[str]:
    """전략이 참조하는 strat:<id> 심볼 — universe.symbols + 모든 노드의 'strat:X.field' ref."""
    ids: set[str] = set()
    for s in (ir.universe.symbols or []):
        if isinstance(s, str) and s.startswith(STRAT_PREFIX):
            ids.add(s)
    nodes = [ir.signal, ir.position.exit.condition, ir.position.overlays.group_label,
             ir.study.label, ir.study.event, ir.study.target_node]
    sc = (ir.universe.screener or {}).get("condition")
    if sc is not None:
        try:
            nodes.append(Node.model_validate(sc))
        except Exception:  # noqa: BLE001 — 잘못된 트리는 validate에서 별도 보고
            pass
    for nd in nodes:
        if nd is None:
            continue
        ids |= {sym for sym in referenced_symbols(nd) if sym.startswith(STRAT_PREFIX)}
    return ids


def has_strat_refs(ir: StrategyIR) -> bool:
    return bool(collect_strat_refs(ir))


def materialize_strategy_assets(ir: StrategyIR, dataset: dict, resolver,
                                *, _seen: frozenset = frozenset()) -> dict:
    """strat:<id> 참조를 자식 전략 equity로 dataset에 주입(깊이우선 재귀·순환탐지).

    resolver(token) -> 자식 전략 spec(dict). token = 'strat:' 접두 제거(버전 포함, 예 'value_ls@v3').
    dataset를 제자리 변형(호출자가 공유 캐시를 보호하려면 사본을 넘길 것). strat: 참조가 있는데
    resolver=None이면 ValueError. 자식은 단일 백테스트(run_strategy_ir)로 실행 — 자식 spec의
    study(펼침/기간분할)는 무시하고 base equity만 자산으로 쓴다.
    """
    refs = collect_strat_refs(ir)
    if not refs:
        return dataset
    if resolver is None:
        raise ValueError("strat: 참조에는 저장 전략 resolver가 필요합니다.")
    for ref in refs:
        if ref in dataset:
            continue                                   # 이미 물질화됨(세션 내 캐시)
        token = ref[len(STRAT_PREFIX):]
        if token in _seen:
            raise ValueError(f"전략 조합 순환참조: {token}")
        child_spec = resolver(token)
        if child_spec is None:
            raise ValueError(f"전략을 찾을 수 없습니다: {token}")
        try:
            child = StrategyIR.model_validate(child_spec)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"자식 전략 파싱 오류({token}): {e}") from e
        materialize_strategy_assets(child, dataset, resolver, _seen=_seen | {token})
        res = run_strategy_ir(child, dataset)
        if not res.get("success") or res.get("equity") is None:
            raise ValueError(f"자식 전략 실행 실패({token}): {res.get('error')}")
        dataset[ref] = _equity_to_frame(res["equity"])
    return dataset
