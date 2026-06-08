"""P1-6(server) — /ir 라우터 핸들러 헤드리스 검증.

명세 P1-6. 요청모델(IrBacktestIn) → backtest_from_spec → serialize_backtest 전 경로를
HTTP/DB 없이 핸들러 직접 호출로 고정. get_dataset만 합성 데이터로 monkeypatch.

    cd platform && pytest tests/test_server_ir_route.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "server"))

ir = pytest.importorskip("app.routers.ir")  # 서버 의존성 미설치 환경이면 skip
from app.routers.ir import IrBacktestIn, ir_backtest, ir_catalog  # noqa: E402

SYM = "005930"


def _fake_dataset():
    idx = pd.date_range("2020-01-01", periods=250, freq="B")
    r = np.random.default_rng(11)
    close = np.maximum(100 + np.cumsum(r.normal(0.05, 1.2, 250)), 5.0)
    return {SYM: pd.DataFrame({
        "Open": np.concatenate([[close[0]], close[:-1]]),
        "High": close * 1.01, "Low": close * 0.99, "Close": close,
        "Volume": r.uniform(1e5, 1e6, 250),
        "ma_dev_20d": r.uniform(-5, 5, 250),
    }, index=idx)}


@pytest.fixture(autouse=True)
def _patch_dataset(monkeypatch):
    # ir_backtest는 qc.load_dataset_for(needed)로 부분집합 로드한다(get_dataset 아님).
    # ir_strategy의 strat: 폴백 경로만 get_dataset 사용 — 둘 다 합성 데이터로 패치.
    monkeypatch.setattr(ir, "get_dataset", _fake_dataset)
    monkeypatch.setattr(ir.qc, "load_dataset_for", lambda needed: _fake_dataset())


def _buy(indicator, op, value):
    return {"op": "compare", "params": {"op": op},
            "inputs": {"left": {"op": "data", "params": {"ref": f"__SELF__.{indicator}"}},
                       "right": {"op": "const", "params": {"value": value}}}}


def test_catalog_endpoint():
    res = ir_catalog(user=None)
    blocks = res["blocks"]
    assert len(blocks) > 30
    ops = {b["op"] for b in blocks}
    assert {"compare", "rank", "ts_corr", "logic", "data"} <= ops


def test_backtest_endpoint_success():
    body = IrBacktestIn(trade_symbol=SYM, buy=_buy("ma_dev_20d", ">", 0.0),
                        hold_days=10, initial_capital=1e7)
    res = ir_backtest(body, user=None)
    assert res["success"]
    assert "metrics" in res and res["metrics"]["n_trades"] >= 1
    assert isinstance(res["equity"], list) and len(res["equity"]) > 0
    assert "warnings" in res


def test_backtest_endpoint_rejects_bad_type():
    bad = {"op": "logic", "params": {"logic": "AND"},
           "inputs": {"0": {"op": "data", "params": {"ref": "__SELF__.ma_dev_20d"}}}}
    body = IrBacktestIn(trade_symbol=SYM, buy=bad)
    res = ir_backtest(body, user=None)
    assert res["success"] is False
    assert any(i["rule"] == "R1" for i in res["issues"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
