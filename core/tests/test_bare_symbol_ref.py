"""bare 심볼 ref 정규화(StrategyIR._normalize_bare_symbol_refs) — COT R0 부류 마감 가드.

NL 컴파일러가 매크로 크로스참조를 bare("금선물투기순포지션")로 낼 때 4계층 정합이 깨지던
부류(로더 미수집→R0 / 로드돼도 평가기가 컬럼 해석→NaN)를 파싱 경계 정규화로 닫는다.
"""

from __future__ import annotations

from quant_core.ir_engine.spec import StrategyIR, needed_symbols


def _ir(signal, universe=None, **kw):
    return {"universe": universe or {"kind": "single", "symbols": ["금선물"]},
            "signal": signal, "query": "simulate", **kw}


def _data(ref):
    return {"op": "data", "params": {"ref": ref}}


def _gt(lhs, value=0):
    return {"op": "compare", "params": {"cmp": "gt"},
            "inputs": {"lhs": lhs, "rhs": {"op": "const", "params": {"value": value}}}}


def test_bare_macro_ref_normalized_to_dotted():
    s = StrategyIR.model_validate(_ir(_gt(_data("금선물투기순포지션"))))
    assert s.signal.inputs["lhs"].params["ref"] == "금선물투기순포지션.Close"
    assert "금선물투기순포지션" in needed_symbols(s)      # 로더가 이제 수집한다(R0 마감)


def test_dotted_and_column_refs_untouched():
    """dotted는 그대로, 컬럼/미지 bare ref는 비변경(오정규화 없음·기존 R0 진단 보존)."""
    s = StrategyIR.model_validate(_ir(_gt(_data("VIX.Close"))))
    assert s.signal.inputs["lhs"].params["ref"] == "VIX.Close"
    s2 = StrategyIR.model_validate(_ir(_gt(_data("rsi_14"), 50)))
    assert s2.signal.inputs["lhs"].params["ref"] == "rsi_14"
    s3 = StrategyIR.model_validate(_ir(_gt(_data("없는심볼오타"))))
    assert s3.signal.inputs["lhs"].params["ref"] == "없는심볼오타"


def test_universe_own_symbol_bare_ref_normalized():
    """유니버스 자신의 심볼을 bare로 참조해도 정규화(같은 부류 — 종목코드 포함)."""
    s = StrategyIR.model_validate(_ir(_gt(_data("005930")),
                                      universe={"kind": "single", "symbols": ["005930"]}))
    assert s.signal.inputs["lhs"].params["ref"] == "005930.Close"


def test_nested_and_study_nodes_normalized():
    """중첩 노드(ts_delta 안)와 study.event 트리도 걷는다 — needed_symbols와 같은 표면."""
    nested = _gt({"op": "ts_delta", "params": {"window": 20},
                  "inputs": {"src": _data("옵션풋콜비율")}})
    s = StrategyIR.model_validate(_ir(nested, study={"event": _data("금선물투기순포지션")}))
    assert s.signal.inputs["lhs"].inputs["src"].params["ref"] == "옵션풋콜비율.Close"
    assert s.study.event.params["ref"] == "금선물투기순포지션.Close"
    needed = needed_symbols(s)
    assert {"옵션풋콜비율", "금선물투기순포지션"} <= needed


def test_screener_condition_dict_normalized():
    s = StrategyIR.model_validate(_ir(
        _gt(_data("Close"), 0),
        universe={"kind": "list", "symbols": ["005930", "000660"],
                  "screener": {"condition": _gt(_data("코스피200변동성지수"), 25)}}))
    cond = s.universe.screener["condition"]
    assert cond["inputs"]["lhs"]["params"]["ref"] == "코스피200변동성지수.Close"


def test_idempotent_revalidation():
    """정규화된 IR을 재검증(동결 원장 재파싱 시나리오)해도 불변 — 멱등."""
    s = StrategyIR.model_validate(_ir(_gt(_data("금선물투기순포지션"))))
    s2 = StrategyIR.model_validate(s.model_dump())
    assert s2.signal.inputs["lhs"].params["ref"] == "금선물투기순포지션.Close"
