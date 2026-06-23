"""뿌리② select.group_by(섹터별 top-N) + 뿌리③ name/code 부착 — run_select 계약 회귀."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import quant_core.expression_parser as ep  # noqa: E402
from quant_core.ir_engine import StrategyIR, run_query  # noqa: E402

_SEC = {"S1": "반도체", "S2": "반도체", "S3": "반도체",
        "S4": "배터리", "S5": "배터리", "S6": "배터리"}
_PB = {"S1": 0.5, "S2": 0.8, "S3": 1.2, "S4": 0.6, "S5": 0.9, "S6": 1.5}


def _ohlc(pb: float) -> pd.DataFrame:
    idx = pd.date_range("2022-01-03", periods=60, freq="B")
    close = np.full(60, 100.0)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e6, "pb_ratio": pb}, index=idx)


def _ds() -> dict:
    return {s: _ohlc(pb) for s, pb in _PB.items()}


def _ir(**sel) -> dict:
    return {"universe": {"kind": "list", "symbols": list(_SEC)},
            "signal": {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}},
            "query": "select", "select": {"descending": False, **sel}}


def test_group_by_per_sector_top_n(monkeypatch):
    monkeypatch.setattr(ep, "get_symbol_group", lambda sym, gt="Industry": _SEC.get(sym, "기타"))
    res = run_query(StrategyIR.model_validate(_ir(top_n=2, group_by="Sector")), _ds())
    assert res.get("success"), res.get("error")
    assert res["group_by"] == "Sector"
    groups = {g["group"]: g["results"] for g in res["groups"]}
    assert set(groups) == {"반도체", "배터리"}                      # 멀티섹터 둘 다(반도체만 아님)
    assert len(groups["반도체"]) == 2 and len(groups["배터리"]) == 2  # 섹터별 top-2
    assert [r["code"] for r in groups["반도체"]] == ["S1", "S2"]     # 저평가(낮은 pb) 우선
    assert [r["code"] for r in groups["배터리"]] == ["S4", "S5"]


def test_results_have_name_and_code(monkeypatch):
    monkeypatch.setattr(ep, "get_symbol_group", lambda sym, gt="Industry": _SEC.get(sym, "기타"))
    res = run_query(StrategyIR.model_validate(_ir(top_n=3)), _ds())
    assert res.get("success"), res.get("error")
    r0 = res["results"][0]
    assert r0["code"] == r0["symbol"]              # 코드
    assert "name" in r0                            # 이름(자기서술)


def test_symbol_name_real_ticker():
    from quant_core.expression_parser import symbol_name
    assert symbol_name("005930") == "삼성전자"      # ticker_db.json(코어 metadata)
    assert symbol_name("999999") == "999999"       # 미수급 → 코드 폴백


def test_symbol_name_reads_bundled_db_ignoring_runtime_dir(monkeypatch, tmp_path):
    """프로덕션 회귀: QP_CORE_DATA_DIR가 런타임 데이터 볼륨(prod=/srv/data, ticker_db 없음)을
    가리켜도 종목명은 번들 metadata(core/data)에서 읽어야 한다. 안 그러면 lookup이 빈 맵→전
    종목이 코드로 폴백돼 '종목명=코드'로 뜬다(실제 프로덕션 버그였음)."""
    import quant_core.expression_parser as ep
    monkeypatch.setenv("QP_CORE_DATA_DIR", str(tmp_path))   # ticker_db 없는 빈 디렉터리
    monkeypatch.setattr(ep, "_NAME_CACHE", None)            # 캐시 리셋(teardown이 restore)
    assert ep.symbol_name("005930") == "삼성전자"           # env 무시·번들에서 이름 해석


_PER = {"S1": 8, "S2": 20, "S3": 30, "S4": 10, "S5": 25, "S6": 35}


def _ds2() -> dict:
    out = {}
    idx = pd.date_range("2022-01-03", periods=60, freq="B")
    close = np.full(60, 100.0)
    for s in _SEC:
        out[s] = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                               "Close": close, "Volume": 1e6,
                               "pb_ratio": _PB[s], "trailing_pe": _PER[s]}, index=idx)
    return out


def test_composite_score_runs(monkeypatch):
    """저평가 composite(백분위 합)이 run_select에서 평가·랭킹되는지 — assemble_ir이 만드는 신호 구조."""
    monkeypatch.setattr(ep, "get_symbol_group", lambda sym, gt="Industry": _SEC.get(sym, "기타"))

    def _rank(ref):
        return {"op": "rank", "params": {"unit": "pct", "descending": False},
                "inputs": {"signal": {"op": "data", "params": {"ref": ref}}}}
    sig = {"op": "binary", "params": {"op": "+"},
           "inputs": {"a": _rank("__SELF__.pb_ratio"), "b": _rank("__SELF__.trailing_pe")}}
    ir = {"universe": {"kind": "list", "symbols": list(_SEC)},
          "signal": sig, "query": "select", "select": {"descending": False, "top_n": 2}}
    res = run_query(StrategyIR.model_validate(ir), _ds2())
    assert res.get("success"), res.get("error")
    # S1(pb 0.5·per 8 = 둘 다 최저분위) → composite 최저 → 저평가 1위
    assert res["results"][0]["code"] == "S1"
    assert res["results"][0]["score"] is not None              # composite 점수 산출됨


def test_result_contract_columns_scoring(monkeypatch):
    """뿌리③ — 결과에 columns(메타)·scoring(산식) 부착."""
    monkeypatch.setattr(ep, "get_symbol_group", lambda sym, gt="Industry": _SEC.get(sym, "기타"))
    res = run_query(StrategyIR.model_validate(_ir(top_n=3, display=["pb_ratio"])), _ds())
    assert res.get("success"), res.get("error")
    cols = {c["key"]: c for c in res["columns"]}
    assert {"name", "code", "score", "pb_ratio"} <= set(cols)       # identity+점수+메트릭
    assert cols["pb_ratio"]["label"] == "PBR" and cols["pb_ratio"]["unit"] == "배"
    assert res["scoring"]["recipe"]                                 # 산식 문자열(투명)
    assert res["scoring"]["direction"] == "low_better"             # descending=False


def test_column_meta_units():
    from quant_core.ir_engine.columns import column_meta
    assert column_meta("market_cap")["unit"] == "백만원" and column_meta("market_cap")["scale"] == 1e6
    assert column_meta("ev_ebitda")["label"] == "EV/EBITDA"
    assert column_meta("unknown_x")["label"] == "unknown_x"         # 미등록 폴백
