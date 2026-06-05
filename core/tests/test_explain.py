"""explain_ir — StrategyIR를 MECE 버킷별 사람이 읽는 설명으로 분해하는지 검증.

핵심 가치: silent 기본 가정(비용·자본·체결)을 [기본]으로 표면화, 유저가 정한 값은
[지정]으로 구분. 단위(분수 vs 퍼센트)가 엔진과 일치하는지도 잠근다.

    cd platform/core && python -m pytest tests/test_explain.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.blocks import data
from quant_core.ir_engine import (Entry, Exit, PositionSpec, SimSpec, Sizing,
                                   StrategyIR, Universe)
from quant_core.ir_engine.explain import explain_ir

_ALWAYS = ["universe", "signal", "entry", "sizing", "exit", "overlays",
           "execution", "environment"]


def _bucket(doc, key):
    for b in doc["buckets"]:
        if b["key"] == key:
            return b
    raise AssertionError(f"버킷 '{key}' 없음: {[b['key'] for b in doc['buckets']]}")


def _item(bucket, label_sub):
    for it in bucket["items"]:
        if label_sub in it["label"]:
            return it
    raise AssertionError(f"항목 '{label_sub}' 없음: {[i['label'] for i in bucket['items']]}")


def _make_ir():
    return StrategyIR(
        signal=data("momentum_12_1m"),
        universe=Universe(kind="list", symbols=["005930", "000660"]),
        position=PositionSpec(
            direction="short",
            sizing=Sizing(mode="equal_weight"),
            entry=Entry(mode="scheduled", rebalance="monthly", top_n=2),
            exit=Exit(take_profit=0.1, stop_loss=-0.05),
        ),
        simulation=SimSpec(leverage=2.0),   # commission/capital 미지정 → 기본
    )


def test_all_eight_buckets_present():
    doc = explain_ir(_make_ir())
    keys = [b["key"] for b in doc["buckets"]]
    for k in _ALWAYS:
        assert k in keys, f"{k} 버킷 누락"


def test_silent_cost_defaults_surfaced_as_default():
    """유저가 비용을 안 적어도 엔진 기본값이 [기본]으로 드러나야 — 이 기능의 핵심."""
    ex = _bucket(explain_ir(_make_ir()), "execution")
    comm = _item(ex, "수수료")
    assert comm["provenance"] == "기본"
    assert "0.03%" in comm["value"]
    assert "0.1%" in _item(ex, "슬리피지")["value"]
    assert "0.23%" in _item(ex, "매도세")["value"]
    # 체결 시점도 기본(다음 거래일 시가)
    assert "시가" in _item(ex, "체결 시점")["value"]


def test_user_set_fields_marked_specified():
    doc = explain_ir(_make_ir())
    direction = _item(_bucket(doc, "entry"), "방향")
    assert direction["provenance"] == "지정"
    assert "숏" in direction["value"] or "공매도" in direction["value"]
    lev = _item(_bucket(doc, "sizing"), "레버리지")
    assert lev["provenance"] == "지정"
    assert "2" in lev["value"]


def test_exit_units_match_engine_fractions():
    """take_profit=0.1 은 분수(=10%), stop_loss=-0.05 는 -5% 로 표시돼야(엔진 단위 일치)."""
    ex = _bucket(explain_ir(_make_ir()), "exit")
    assert "10%" in _item(ex, "익절")["value"]
    assert "5%" in _item(ex, "손절")["value"]
    assert _item(ex, "익절")["provenance"] == "지정"


def test_signal_bucket_shows_type_and_indicators():
    sig = _bucket(explain_ir(_make_ir()), "signal")
    typ = _item(sig, "종류")
    assert "팩터" in typ["value"]
    inds = _item(sig, "지표")
    assert "momentum_12_1m" in inds["value"]


def test_default_capital_is_default():
    cap = _item(_bucket(explain_ir(_make_ir()), "environment"), "초기 자본")
    assert cap["provenance"] == "기본"
    assert "10,000,000" in cap["value"]


def test_always_mode_marks_exit_not_applicable():
    ir = _make_ir()
    ir.position.entry.mode = "always"
    ir.position.exit = Exit()
    ex = _bucket(explain_ir(ir), "exit")
    assert "always" in _item(ex, "청산")["value"]


def test_sweep_bucket_only_when_axis_set():
    base = explain_ir(_make_ir())
    assert all(b["key"] != "analysis" for b in base["buckets"])  # axis=none → 없음


def test_summary_is_readable_prose():
    doc = explain_ir(_make_ir())
    s = doc["summary"]
    assert isinstance(s, str) and len(s) > 30
    assert "공매도" in s or "숏" in s            # direction=short 반영
    assert "리밸런싱" in s                       # scheduled 반영
    assert "0.03%" in s                          # 비용 기본가정이 산문에도 명시
    assert "10,000,000원" in s                   # 자본 기본값
