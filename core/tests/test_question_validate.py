import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR, validate_strategy

_ATTR={"op":"attribute","params":{"attr":"Sector"}}
_SCR={"op":"is_in","params":{"values":["반도체"]},"inputs":{"signal":_ATTR}}
_PB={"op":"data","params":{"ref":"__SELF__.pb_ratio"}}
_CLOSE={"op":"data","params":{"ref":"__SELF__.Close"}}

def _errs(ir):
    return [i.rule for i in validate_strategy(StrategyIR.model_validate(ir)) if i.is_error]

def _err_msgs(ir):
    return [i.message for i in validate_strategy(StrategyIR.model_validate(ir)) if i.is_error]


# ── M1: method-fit 리다이렉트 (#9) ───────────────────────────────────────────
# 단일종목 예측력을 횡단 method(IC/회귀)로 오매핑하면, 게이트는 이미 거부하나(종목 2+)
# 메시지가 막다른 제약이라 repair가 종목 추가(의도 왜곡)로 잘못 수렴한다. 메시지가
# 올바른 대안(이벤트 스터디)을 안내해야 모델이 단일종목 예측을 이벤트스터디로 재컴파일한다.

def test_single_symbol_ic_redirects_to_event_study():
    msgs=_err_msgs({"query":"relate","universe":{"kind":"single","symbols":["005930"]},
                    "signal":_CLOSE,
                    "study":{"relation_kind":"ic","target_node":_CLOSE,"windows":[5,20]}})
    assert any("이벤트" in m for m in msgs), f"단일종목 IC 에러는 이벤트스터디로 안내해야: {msgs}"

def test_single_symbol_regression_redirects_to_event_study():
    msgs=_err_msgs({"query":"relate","universe":{"kind":"single","symbols":["005930"]},
                    "signal":_CLOSE,
                    "study":{"relation_kind":"regression","factors":[_CLOSE],"windows":[5,20]}})
    assert any("이벤트" in m for m in msgs), f"단일종목 회귀 에러는 이벤트스터디로 안내해야: {msgs}"

def test_cross_sectional_ic_two_symbols_validates_clean():
    # 횡단 IC(종목 2+)는 정당 — 단일종목 가드가 걸리면 안 된다(회귀 가드).
    errs=_errs({"query":"relate","universe":{"kind":"list","symbols":["005930","000660"]},
                "signal":_CLOSE,
                "study":{"relation_kind":"ic","target_node":_CLOSE,"windows":[5,20]}})
    assert errs==[], f"횡단 IC(2종목) 통과해야: {errs}"

def test_single_symbol_event_study_validates_clean():
    # 단일종목 예측의 올바른 method=이벤트스터디 — 단일종목이어도 통과해야(M1 redirect 목적지).
    event={"op":"compare","params":{"op":">"},
           "inputs":{"left":_CLOSE,"right":{"op":"const","params":{"value":100}}}}
    errs=_errs({"query":"relate","universe":{"kind":"single","symbols":["005930"]},
                "signal":_CLOSE,
                "study":{"event":event,"windows":[5,20]}})
    assert errs==[], f"단일종목 이벤트스터디 통과해야: {errs}"

def test_select_all_plus_sector_screener_validates_clean():
    # idiom #7 레시피 — 이전엔 S-entry·S-univn 3개 에러로 거짓거부됐다.
    errs=_errs({"query":"select","universe":{"kind":"all","screener":{"condition":_SCR,"refresh":"each_rebalance"}},
                "signal":_PB,"select":{"top_n":3,"descending":False,"display":["pb_ratio"]}})
    assert errs==[], f"select IR이 깨끗이 통과해야: {errs}"

def test_describe_single_score_signal_validates_clean():
    errs=_errs({"query":"describe","universe":{"kind":"single","symbols":["005930"]},"signal":_CLOSE})
    assert "S-entry" not in errs and errs==[], f"describe IR 통과해야: {errs}"

def test_simulate_position_rules_still_apply():
    # 회귀 가드 — simulate는 여전히 S-entry(on_signal+score) 거부.
    errs=_errs({"query":"simulate","universe":{"kind":"single","symbols":["005930"]},"signal":_PB,
                "position":{"entry":{"mode":"on_signal"}}})
    assert "S-entry" in errs, "simulate는 position 규칙 유지해야"

def test_simulate_all_screener_now_allowed():
    # kind=all+스크리너는 simulate에서도 정당(scheduled).
    errs=_errs({"query":"simulate","universe":{"kind":"all","screener":{"condition":_SCR,"refresh":"each_rebalance"}},
                "signal":_PB,"position":{"entry":{"mode":"scheduled","top_n":10}}})
    assert "S-univ" not in errs, f"kind=all+스크리너 허용돼야: {errs}"
