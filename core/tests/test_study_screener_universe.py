"""스터디 러너 universe.screener 소비 — '조용한 무시' 부류 회귀 가드.

prod 실측(로컬 $0 env·conv=5 이벤트스터디): Market=KOSDAQ 스크리너가 IR에 정확히
컴파일됐는데도 실행이 전체(미국 포함) 유니버스로 돌아 동일 결과를 재생산했고,
자기서술은 '2차 스크리너: 적용'이라 표시했다(이중 조용한 오답). 원인=screener 소비가
러너별 사적 구현(engine 백테스트·run_select만)이고, 스터디 공용 choke point인
_universe_symbols(17개 호출부)가 screener를 안 봤다.

수정=_universe_symbols가 스크리너를 최신 유효 단면 1회 평가로 적용(속성 조건은 날짜
무관이라 정확). run_select는 자체 PIT as-of 마스크를 소유하므로 opt-out.
"""
import numpy as np
import pandas as pd
import pytest

from quant_core import expression_parser as ep
from quant_core.ir_engine.run import _universe_symbols, run_query
from quant_core.ir_engine.spec import StrategyIR

KR = ["100001", "100002"]      # 숫자코드 → (패치된) KOSDAQ
US = ["AAAA", "BBBB"]          # 알파티커 → (패치된) NASDAQ


def _px(n: int = 400) -> pd.DataFrame:
    """전반 완만한 하락(126d 수익 < 1.0 strict) → 후반 +1.2%/일 성장.

    126d 성장배율이 1.0을 상향 돌파(cross up)하는 이벤트가 종목당 정확히 1회 발생하도록
    결정적으로 구성(전반이 평평하면 비율이 정확히 1.0이라 strict cross가 안 나온다).
    """
    dates = pd.bdate_range("2023-01-02", periods=n)
    lvl = np.empty(n)
    lvl[0] = 100.0
    for i in range(1, n):
        lvl[i] = lvl[i - 1] * (0.9995 if i < 250 else 1.012)
    return pd.DataFrame({"Open": lvl, "High": lvl, "Low": lvl, "Close": lvl,
                         "Volume": 1_000_000.0}, index=dates)


@pytest.fixture
def dataset():
    return {s: _px() for s in KR + US}


@pytest.fixture(autouse=True)
def _market(monkeypatch):
    monkeypatch.setattr(ep, "symbol_market",
                        lambda s: "KOSDAQ" if s.split(".")[0].isdigit() else "NASDAQ")


def _kosdaq_screener() -> dict:
    # prod conv=5에서 컴파일된 것과 동형(attribute Market is_in ["KOSDAQ"]).
    return {"condition": {"op": "is_in",
                          "params": {"values": ["KOSDAQ"], "match": "contains"},
                          "inputs": {"signal": {"op": "attribute",
                                                "params": {"attr": "Market"}}}},
            "refresh": "once_at_start"}


def _event_ir(screener: dict | None) -> StrategyIR:
    u: dict = {"kind": "all"}
    if screener is not None:
        u["screener"] = screener
    ratio = {"op": "binary", "params": {"op": "/"},
             "inputs": {"a": {"op": "ts_delta", "params": {"window": 126},
                              "inputs": {"signal": {"op": "data",
                                                    "params": {"ref": "__SELF__.Close"}}}},
                        "b": {"op": "ts_delay", "params": {"window": 126},
                              "inputs": {"signal": {"op": "data",
                                                    "params": {"ref": "__SELF__.Close"}}}}}}
    return StrategyIR.model_validate({
        "name": "스크리너-스터디", "universe": u,
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "query": "relate",
        "study": {"event": {"op": "cross", "params": {"direction": "up"},
                            "inputs": {"left": ratio,
                                       "right": {"op": "const", "params": {"value": 1.0}}}},
                  "windows": [-6], "event_basis": "close"}})


def test_market_patch_routes(monkeypatch):
    """패치 지점 유효성 — get_symbol_group이 패치된 symbol_market을 경유하는지 sanity."""
    assert ep.get_symbol_group("100001", "Market") == "KOSDAQ"
    assert ep.get_symbol_group("AAAA", "Market") == "NASDAQ"


def test_universe_symbols_applies_market_screener(dataset):
    """choke point: kind=all + Market 스크리너 → KR만 반환(부류 마감의 핵심 불변식)."""
    ir = _event_ir(_kosdaq_screener())
    assert sorted(_universe_symbols(ir, dataset)) == sorted(KR)


def test_universe_symbols_opt_out_returns_unfiltered(dataset):
    """run_select용 opt-out — 자체 PIT as-of 마스크 소유 경로는 이중 적용 금지."""
    ir = _event_ir(_kosdaq_screener())
    assert sorted(_universe_symbols(ir, dataset, apply_screener=False)) == sorted(KR + US)


def test_event_study_composition_respects_market_screener(dataset):
    """신고 시나리오 E2E: 스크리너 유/무 결과가 달라야 하고, 구성은 KOSDAQ뿐이어야 한다."""
    res_all = run_query(_event_ir(None), dataset)
    res_kr = run_query(_event_ir(_kosdaq_screener()), dataset)
    assert res_all.get("success") and res_kr.get("success")
    comp_all = set((res_all.get("composition") or {}).get("by_symbol", {}))
    comp_kr = set((res_kr.get("composition") or {}).get("by_symbol", {}))
    assert comp_all & set(US), "무필터 대조군엔 US 이벤트가 있어야 실험이 유효하다"
    assert comp_kr, "필터 후에도 KR 이벤트는 남아야 한다"
    assert comp_kr <= set(KR), f"코스닥 필터 결과에 비-KOSDAQ 유입: {comp_kr - set(KR)}"
    assert res_kr.get("n_events") < res_all.get("n_events"), \
        "필터 결과가 무필터와 동일(=조용한 무시 재발)"
