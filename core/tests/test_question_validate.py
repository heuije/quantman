import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR, validate_strategy

_ATTR={"op":"attribute","params":{"attr":"Sector"}}
_SCR={"op":"is_in","params":{"values":["반도체"]},"inputs":{"signal":_ATTR}}
_PB={"op":"data","params":{"ref":"__SELF__.pb_ratio"}}
_CLOSE={"op":"data","params":{"ref":"__SELF__.Close"}}

def _errs(ir):
    return [i.rule for i in validate_strategy(StrategyIR.model_validate(ir)) if i.is_error]

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
