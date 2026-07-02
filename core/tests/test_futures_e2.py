"""E2 — SimSpec 롤/조정이 백테스트에 반영되는지(패널서 재-stitch) + explain 정직성.

_apply_futures_roll: 롤/조정 지정 시 만기물 패널에서 연속물 재구성해 dataset 교체.
미지정=기본 서빙뷰 그대로(패널 로드조차 안 함), 패널 미수집=기본 유지(honest boundary).
"""
import pandas as pd

from quant_core import data_fetcher as _df
from quant_core.blocks import data
from quant_core.data.futures_roll import build_continuous
from quant_core.ir_engine import Entry, PositionSpec, SimSpec, StrategyIR, Universe, explain_ir
from quant_core.ir_engine.run import _apply_futures_roll

D = pd.bdate_range("2020-01-01", periods=6)


def _panel():
    def bars(c, dates, closes, vols, ois):
        return pd.DataFrame({"contract": c, "Open": closes, "High": [x + 1 for x in closes],
                             "Low": [x - 1 for x in closes], "Close": closes, "Settle": closes,
                             "Volume": vols, "OI": ois}, index=dates)
    c1 = bars("202001", D[:4], [100, 101, 102, 103], [1000, 900, 800, 700], [5000, 4000, 3000, 0])
    c2 = bars("202002", D[:6], [110, 111, 112, 113, 114, 115],
              [100, 200, 750, 800, 900, 900], [1000, 2000, 3500, 4000, 5000, 5000])
    return pd.concat([c1, c2]).sort_index()


def _strat(**sim):
    return StrategyIR(signal=data("momentum_12_1m"),
                      universe=Universe(kind="single", symbols=["코스피200선물"]),
                      position=PositionSpec(entry=Entry(mode="always")),
                      simulation=SimSpec(**sim))


def _default_view():
    return build_continuous(_panel(), "at_expiry", "none")   # 기본 서빙뷰 흉내


def test_apply_roll_restitches_from_panel(monkeypatch):
    monkeypatch.setattr(_df, "load_futures_panel", lambda s: _panel())
    ds = {"코스피200선물": _default_view()}
    out = _apply_futures_roll(_strat(roll_method="oi_cross"), ds)
    pd.testing.assert_frame_equal(out["코스피200선물"], build_continuous(_panel(), "oi_cross", "none"))
    pd.testing.assert_frame_equal(ds["코스피200선물"], _default_view())   # 원본 dataset 불변


def test_apply_roll_noop_when_unset(monkeypatch):
    called = []
    monkeypatch.setattr(_df, "load_futures_panel", lambda s: called.append(s) or _panel())
    ds = {"코스피200선물": _default_view()}
    out = _apply_futures_roll(_strat(), ds)                  # 롤/조정 미지정
    assert out is ds and not called                          # 패널 로드조차 안 함(기본 서빙뷰)


def test_apply_roll_keeps_default_when_no_panel(monkeypatch):
    monkeypatch.setattr(_df, "load_futures_panel", lambda s: pd.DataFrame())
    ds = {"코스피200선물": _default_view()}
    out = _apply_futures_roll(_strat(series_adjust="ratio"), ds)
    pd.testing.assert_frame_equal(out["코스피200선물"], _default_view())  # 패널 없음 → 기본 유지


def test_explain_roll_applied_for_paneled(monkeypatch):
    monkeypatch.setattr(_df, "has_futures_panel", lambda s: True)
    doc = explain_ir(_strat(roll_method="oi_cross"))
    env = next(b for b in doc["buckets"] if b["key"] == "environment")
    txt = " ".join(it["value"] for it in env["items"])
    assert "반영" in txt and "oi_cross" in txt                # 적용됨 표면화(미적용 아님)
