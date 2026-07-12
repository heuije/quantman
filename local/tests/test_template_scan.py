# -*- coding: utf-8 -*-
"""템플릿 종가창 후보 합성(scan_template_candidates) — 필터·상한·단일 스캔·방어선.

계약(장중 템플릿 설계 §2.5): 브로커 스캔은 전략 수와 무관하게 1회(최소 임계),
전략별 재필터 = 잠김 필수 + 임계 + 시장(ticker_db) + max_daily_entries(등락률 내림차순).
미지 템플릿·결함 IR은 그 전략만 제외(경고) — 나머지 진입을 막지 않는다.
"""
from __future__ import annotations

import quant_core.expression_parser as ep

from localapp.template_scan import scan_template_candidates

_MKT = {"123450": "KOSDAQ", "067310": "KOSDAQ", "035720": "KOSDAQ",
        "005930": "KOSPI"}

_ROWS = [
    {"symbol": "123450", "name": "A", "price": 1300, "change_pct": 30.0,
     "is_limit_up": True, "ask_rem": 0},
    {"symbol": "005930", "name": "B", "price": 70000, "change_pct": 29.6,
     "is_limit_up": True, "ask_rem": 0},
    {"symbol": "067310", "name": "C", "price": 1290, "change_pct": 29.55,
     "is_limit_up": False, "ask_rem": 4000},          # 준상한 — 잠김 아님
    {"symbol": "035720", "name": "D", "price": 50000, "change_pct": 25.0,
     "is_limit_up": True, "ask_rem": 0},
    {"symbol": "900001", "name": "E", "price": 100, "change_pct": 21.0,
     "is_limit_up": True, "ask_rem": 0},              # 거래소 미상('') — 시장 필터 제외
]


class _FakeBroker:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[float] = []

    def scan_close_surge(self, min_change_pct: float):
        self.calls.append(min_change_pct)
        return self.rows


def _tpl_def(thr=29.5, markets=None, max_entries=3):
    d = {
        "name": "t", "query": "simulate",
        "universe": {"kind": "all"},
        "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.pct_change_1d"}},
            "right": {"op": "const", "params": {"value": thr}}}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 1, "fill": "next_open"}},
        "simulation": {"fill": "close"},
        "template": {"id": "limit_up_close_v1", "max_daily_entries": max_entries},
    }
    if markets:
        d["universe"]["screener"] = {"condition": {
            "op": "is_in", "params": {"values": markets, "match": "contains"},
            "inputs": {"signal": {"op": "attribute", "params": {"attr": "Market"}}}}}
    return d


def test_synthesis_filters_and_caps(monkeypatch):
    monkeypatch.setattr(ep, "symbol_market", lambda s: _MKT.get(s, ""))
    broker = _FakeBroker(_ROWS)
    strategies = [
        {"id": 11, "definition": _tpl_def(thr=29.5, markets=["코스닥"], max_entries=2)},
        {"id": 22, "definition": _tpl_def(thr=20.0, max_entries=3)},
        {"id": 33, "definition": {"template": {"id": "future_tpl_v9"}}},   # 미지 템플릿 — 제외
        {"id": 44, "definition": _tpl_def()["position"] and {"universe": {"kind": "single"}}},  # 일반 전략 — 무관
        {"id": 55, "definition": {"template": {"id": "limit_up_close_v1"}}},  # 결함 IR — 제외
    ]
    out = scan_template_candidates(broker, strategies)

    # 스캔은 1회 — 두 전략의 최소 임계(20.0)로.
    assert broker.calls == [20.0]

    by_sid = {e["strategy_id"]: [c["symbol"] for c in e["candidates"]] for e in out}
    # s11: 잠김+KOSDAQ+≥29.5 → A만 (B=KOSPI, C=잠김아님, D=임계미달).
    assert by_sid[11] == ["123450"]
    # s22: 잠김+국내 전체+≥20 → A,B,D 등락률 내림차순 (E=거래소 미상 제외), max 3.
    assert by_sid[22] == ["123450", "005930", "035720"]
    assert set(by_sid) == {11, 22}
    assert all(c["direction"] == "long" for e in out for c in e["candidates"])


def test_no_template_strategies_no_scan():
    broker = _FakeBroker(_ROWS)
    out = scan_template_candidates(broker, [{"id": 1, "definition": {"universe": {}}}])
    assert out == [] and broker.calls == []


def test_max_entries_cap(monkeypatch):
    monkeypatch.setattr(ep, "symbol_market", lambda s: _MKT.get(s, ""))
    broker = _FakeBroker(_ROWS)
    out = scan_template_candidates(
        broker, [{"id": 9, "definition": _tpl_def(thr=20.0, max_entries=1)}])
    assert [c["symbol"] for c in out[0]["candidates"]] == ["123450"]   # 등락률 1위만
