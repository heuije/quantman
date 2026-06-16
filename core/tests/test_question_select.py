import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR

_SIG = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}
_BASE = {"universe": {"kind": "list", "symbols": ["AAA", "BBB"]}, "signal": _SIG}


def test_select_query_and_spec_parse():
    s = StrategyIR.model_validate({**_BASE, "query": "select",
                                   "select": {"top_n": 3, "descending": False,
                                              "display": ["pb_ratio"]}})
    assert s.query == "select"
    assert s.select is not None and s.select.top_n == 3
    assert s.select.descending is False and s.select.as_of == "latest"


def test_select_defaults():
    s = StrategyIR.model_validate({**_BASE, "query": "select", "select": {"top_n": 5}})
    assert s.select.descending is True and s.select.display == [] and s.select.top_pct is None


# ── 골든 스크린 (run_select — 합성 픽스처, 결정적) ────────────────────────────

import numpy as np
import pandas as pd
from quant_core.ir_engine import run_query


def _df(pb: float, mcap: float, n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.full(n, 100.0), "High": np.full(n, 101.0),
        "Low": np.full(n, 99.0), "Close": np.full(n, 100.0),
        "Volume": np.full(n, 1e6),
        "pb_ratio": np.full(n, pb),      # 종목별 상수(결정적 랭킹)
        "market_cap": np.full(n, mcap),
    }, index=idx)


# pb: A=0.8 B=3.0 C=1.2 D=0.5 E=2.0  (저평가=낮은 pb)  market_cap: D만 소형(5e10)
_DS = {"AAA": _df(0.8, 9e11), "BBB": _df(3.0, 9e11), "CCC": _df(1.2, 9e11),
       "DDD": _df(0.5, 5e10), "EEE": _df(2.0, 9e11)}
_PB = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}


def _select_ir(**select):
    return {"universe": {"kind": "list", "symbols": list(_DS)},
            "signal": _PB, "query": "select", "select": select}


def test_run_select_ranks_lowest_pb_first():
    res = run_query(__import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"])
                    .StrategyIR.model_validate(_select_ir(top_n=3, descending=False,
                                                          display=["pb_ratio"])), _DS)
    assert res["success"] and res["query"] == "select"
    assert res["universe_size"] == 5 and res["eligible_size"] == 5
    syms = [r["symbol"] for r in res["results"]]
    assert syms == ["DDD", "AAA", "CCC"]                 # pb 0.5 < 0.8 < 1.2
    assert res["results"][0]["metrics"]["pb_ratio"] == 0.5
    assert res["results"][0]["score"] == 0.5


def test_run_select_screener_eligibility():
    # 대형주만(market_cap > 1e11) → DDD(소형) 제외 → 저pb 상위 2 = AAA, CCC
    ir = _select_ir(top_n=2, descending=False, display=["pb_ratio"])
    ir["universe"]["screener"] = {"condition": {
        "op": "compare", "params": {"op": ">"},
        "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.market_cap"}},
                   "right": {"op": "const", "params": {"value": 1e11}}}}}
    s = __import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"]).StrategyIR.model_validate(ir)
    res = run_query(s, _DS)
    assert res["success"] and res["eligible_size"] == 4   # DDD 제외
    assert [r["symbol"] for r in res["results"]] == ["AAA", "CCC"]


def test_run_select_latest_skips_trailing_all_nan_rows():
    # kind=all 유니버스에 24/7 시리즈(암호화폐 등)가 섞이면 마스터 인덱스가 주식 마지막
    # 종가일 너머(주말·당일 새벽)까지 뻗고, 그 끝 행은 score(pb_ratio)가 전부 NaN이다.
    # latest는 마지막 인덱스가 아니라 score가 하나라도 비결측인 마지막 단면을 집어야 한다.
    stock_end = _DS["AAA"].index[-1]
    btc_idx = pd.date_range("2021-01-01", stock_end + pd.Timedelta(days=3), freq="D")
    n = len(btc_idx)
    btc = pd.DataFrame({"Open": np.full(n, 50.0), "High": np.full(n, 51.0),
                        "Low": np.full(n, 49.0), "Close": np.full(n, 50.0),
                        "Volume": np.full(n, 1e6)}, index=btc_idx)   # pb_ratio 없음
    ds = {**_DS, "BTC": btc}
    s = StrategyIR.model_validate({"universe": {"kind": "all"}, "signal": _PB,
                                   "query": "select",
                                   "select": {"top_n": 3, "descending": False,
                                              "display": ["pb_ratio"]}})
    res = run_query(s, ds)
    assert res["success"]
    assert res["as_of"] == str(stock_end)[:10]               # BTC 꼬리 날짜가 아님
    assert res["eligible_size"] == 5                          # 주식 5종 전부 유효
    assert [r["symbol"] for r in res["results"]] == ["DDD", "AAA", "CCC"]


def test_run_select_rejects_condition_signal():
    bad = {"universe": {"kind": "list", "symbols": ["AAA"]},
           "signal": {"op": "compare", "params": {"op": ">"},
                      "inputs": {"left": _PB, "right": {"op": "const", "params": {"value": 1.0}}}},
           "query": "select", "select": {"top_n": 1}}
    s = __import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"]).StrategyIR.model_validate(bad)
    res = run_query(s, _DS)
    assert res["success"] is False     # score 아님 → 거부


# ── 섹터 필터 부분일치 (sector contains — KSIC '반도체 제조업' vs 사용자어 '반도체') ──
# "저평가 반도체주 3개"의 실제 prod 실패: 분류 데이터(FDR KRX-DESC)는 반도체주를 Industry
# '반도체 제조업'으로 저장하는데 컴파일러는 is_in(attribute(Sector), ["반도체"]) 정확매칭을
# emit → "반도체 제조업"≠"반도체" → eligible 0. 부분일치 match="contains"로 근본수정.

def _df_pb(pb: float, n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.full(n, 100.0), "High": np.full(n, 101.0),
        "Low": np.full(n, 99.0), "Close": np.full(n, 100.0),
        "Volume": np.full(n, 1e6), "pb_ratio": np.full(n, pb),
    }, index=idx)


_SECTOR_ATTR = {"op": "attribute", "params": {"attr": "Sector"}}


def _sector_ir(values, match=None, **select):
    params = {"values": values}
    if match is not None:
        params["match"] = match
    cond = {"op": "is_in", "params": params, "inputs": {"signal": _SECTOR_ATTR}}
    return {"universe": {"kind": "all", "screener": {"condition": cond}},
            "signal": _PB, "query": "select", "select": dict(select)}


def test_run_select_sector_contains_matches_ksic_industry(monkeypatch):
    """'반도체' 부분일치가 KSIC '반도체 제조업'을 매칭 — 정확매칭 eligible 0 근본수정.

    + 24/7 crypto 꼬리로 마스터 인덱스가 미래로 늘어도 as_of fix가 마지막 유효일을 집어,
    두 버그(as_of tail · 섹터 정확매칭)가 함께 닫히는지 ' 저평가 반도체주 3개' end-to-end 검증.
    """
    from quant_core.data.feeds import classification
    sectors = {
        "000660": {"Industry": "반도체 제조업"},       # SK하이닉스
        "042700": {"Industry": "반도체 제조업"},       # (테스트용 반도체)
        "240810": {"Industry": "반도체 제조업"},
        "105560": {"Industry": "은행 및 저축기관"},    # 비반도체, 최저 pb(은행)
    }
    monkeypatch.setattr(classification, "load", lambda: sectors)

    stock_end = pd.date_range("2021-01-01", periods=60, freq="B")[-1]
    btc_idx = pd.date_range("2021-01-01", stock_end + pd.Timedelta(days=3), freq="D")
    n = len(btc_idx)
    btc = pd.DataFrame({"Open": np.full(n, 5.0), "High": np.full(n, 5.0),
                        "Low": np.full(n, 5.0), "Close": np.full(n, 5.0),
                        "Volume": np.full(n, 1e6)}, index=btc_idx)   # pb_ratio 없음(24/7)
    ds = {"000660": _df_pb(1.0), "042700": _df_pb(1.5), "240810": _df_pb(2.0),
          "105560": _df_pb(0.5), "BTC": btc}

    s = StrategyIR.model_validate(_sector_ir(["반도체"], match="contains",
                                             top_n=3, descending=False, display=["pb_ratio"]))
    res = run_query(s, ds)
    assert res["success"]
    assert res["as_of"] == str(stock_end)[:10]            # crypto 꼬리 아님 (as_of fix)
    assert res["eligible_size"] == 3                       # 반도체 3종 (은행 제외)
    syms = [r["symbol"] for r in res["results"]]
    assert syms == ["000660", "042700", "240810"]         # pb 1.0 < 1.5 < 2.0
    assert "105560" not in syms                            # 최저 pb이나 비반도체 → 제외


def test_run_select_sector_exact_match_is_default(monkeypatch):
    """match 미지정=정확매칭 유지 — 부분일치 안 함(버킷·국면 라벨 정확 멤버십 회귀 가드).

    표준 섹터 정규화로 의약품 제조업 → 테마 "제약·바이오". match 없는 정확매칭은
    "제약·바이오" != "바이오"라 0건(부분일치였다면 1건). exact가 기본임을 고정.
    """
    from quant_core.data.feeds import classification
    monkeypatch.setattr(classification, "load",
                        lambda: {"207940": {"Industry": "의약품 제조업"}})   # → 테마 "제약·바이오"
    ds = {"207940": _df_pb(1.0)}
    s = StrategyIR.model_validate(_sector_ir(["바이오"], top_n=3, descending=False))
    res = run_query(s, ds)
    assert res["success"] and res["eligible_size"] == 0   # "제약·바이오" != "바이오" (정확매칭)
