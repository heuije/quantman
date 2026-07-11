"""시장(거래소) 유니버스 분리 — attribute("Market") 축.

ticker_db.json의 거래소(x: KOSPI·KOSDAQ·NASDAQ·NYSE)를 attribute Market 라벨로 노출해
is_in 스크리너로 '코스닥만/나스닥만' 백테스트·스크리닝을 가능하게 한다. 개별 주식 전용 —
ETF·지수·매크로는 주식 리스팅에 없어 폴백('기타'/'Other') 라벨로 자동 제외된다.
"""
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core import expression_parser as ep
from quant_core.expression_parser import (get_symbol_group, market_match_values,
                                          symbol_market)


# ── ticker_db 실파일 계약 (재생성 회귀 가드) ────────────────────────────────────

def test_symbol_market_real_ticker_db():
    assert symbol_market("005930") == "KOSPI"       # 삼성전자
    assert symbol_market("247540") == "KOSDAQ"      # 에코프로비엠
    assert symbol_market("005930.KS") == "KOSPI"    # 접미사 무관


def test_sp500_members_carry_real_exchange():
    """S&P500 구성 대형주의 거래소 보존 — 생성기가 S&P500(지수) 라벨로 먼저 적재하고
    거래소를 안 채우면(옛 build_db 결함) 시장 필터에서 대형주 500종이 전멸한다."""
    assert symbol_market("MMM") == "NYSE"
    assert symbol_market("AMD") == "NASDAQ"
    assert symbol_market("NVDA") == "NASDAQ"


def test_etf_and_unknown_have_no_market():
    assert symbol_market("069500") == ""    # KODEX 200 — ETF는 주식 리스팅 밖
    assert symbol_market("ZZZQX") == ""


def test_ticker_db_market_vocabulary_whitelist():
    """markets 맵 어휘는 4거래소뿐(지수 라벨 S&P500 잔재 등 오염 금지) + 4거래소 실질 커버."""
    markets = ep._ticker_meta()[1]
    assert set(markets.values()) <= {"KOSPI", "KOSDAQ", "NASDAQ", "NYSE"}
    c = Counter(markets.values())
    assert all(c[x] > 300 for x in ("KOSPI", "KOSDAQ", "NASDAQ", "NYSE"))


# ── get_symbol_group 라우팅 + 폴백 ───────────────────────────────────────────

def test_get_symbol_group_market_routing(monkeypatch):
    monkeypatch.setattr(ep, "symbol_market",
                        lambda s: {"000001": "KOSPI"}.get(s.split(".")[0], ""))
    assert get_symbol_group("000001", "Market") == "KOSPI"
    assert get_symbol_group("999999", "Market") == "기타"    # KR 숫자 폴백
    assert get_symbol_group("ZZZZ", "Market") == "Other"     # US 알파 폴백


# ── 사용자 시장어 정규화 (컴파일러 소비) ──────────────────────────────────────

def test_market_match_values_normalizes_user_words():
    assert market_match_values(["코스닥"]) == ["KOSDAQ"]
    assert market_match_values(["국장"]) == ["KOSPI", "KOSDAQ"]
    assert market_match_values(["미장"]) == ["NASDAQ", "NYSE"]
    assert market_match_values(["코스닥 시장", "나스닥"]) == ["KOSDAQ", "NASDAQ"]
    assert market_match_values(["kosdaq"]) == ["KOSDAQ"]          # canonical 대소문자 무관
    assert market_match_values(["KOSPI", "코스피"]) == ["KOSPI"]   # 중복 제거·순서 보존


def test_market_match_values_unknown_kept_raw():
    # 미상 값은 원문 유지(침묵 변조 금지 — 값이 사라져 조용히 전체/0종목이 되지 않게).
    assert market_match_values(["듣보거래소"]) == ["듣보거래소"]
    assert market_match_values([]) == []


# ── 엔진 E2E — kind=all + Market 스크리너가 simulate(백테스트)에서 실제 분리 ────

def _df(drift: float, n: int = 140) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    close = 100.0 * np.cumprod(np.full(n, 1.0 + drift))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": np.full(n, 1e6)}, index=idx)


_MKT = {"KPA": "KOSPI", "KPB": "KOSPI", "KDA": "KOSDAQ", "KDB": "KOSDAQ"}


def _market_ir(values):
    return {
        "universe": {"kind": "all", "screener": {"condition": {
            "op": "is_in", "params": {"values": values, "match": "contains"},
            "inputs": {"signal": {"op": "attribute", "params": {"attr": "Market"}}}}}},
        "signal": {"op": "ts_delta", "params": {"window": 20},
                   "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}},
        "position": {"direction": "long",
                     "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
    }


def test_simulate_screener_market_separation(monkeypatch):
    """코스닥 2종=+1%/일·코스피 2종=-1%/일로 지은 뒤 Market 스크리너로 백테스트.
    KOSDAQ 필터=상승, KOSPI 필터=하락이어야 한다 — 필터가 조용히 안 먹으면(전종목
    모멘텀 top이 그대로면) 두 실행이 같은 상승 종목을 골라 KOSPI 쪽도 양수가 된다."""
    from quant_core.ir_engine.service import strategy_from_spec
    monkeypatch.setattr(ep, "symbol_market", lambda s: _MKT.get(s.split(".")[0], ""))
    ds = {"KPA": _df(-0.01), "KPB": _df(-0.01), "KDA": _df(+0.01), "KDB": _df(+0.01)}

    res_kd = strategy_from_spec(_market_ir(["KOSDAQ"]), ds)
    assert res_kd.get("success"), res_kd.get("error")
    eq = res_kd["equity"]
    assert eq.iloc[-1] > eq.iloc[0]           # 상승 종목(코스닥)만 보유

    res_kp = strategy_from_spec(_market_ir(["KOSPI"]), ds)
    assert res_kp.get("success"), res_kp.get("error")
    eq2 = res_kp["equity"]
    assert eq2.iloc[-1] < eq2.iloc[0]         # 하락 종목(코스피)만 보유 — 혼입 없음
