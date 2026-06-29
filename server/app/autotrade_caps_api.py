"""서버측 자동매매 capability 진입점 — core SSOT를 게이트·웹·카탈로그에 연결.

asset_class_for_symbol: 심볼→자산군(미지원 시 None). 일본/홍콩 방어 차단 포함.
capability_matrix: 전 셀을 JSON 직렬화(웹 소비 — 라벨·AccountPicker 필터).
"""
from __future__ import annotations

from typing import Any

import quant_core as qc
from quant_core.autotrade_capability import (
    autotrade_capability, BROKERS, MODES, ASSET_CLASSES,
)

# KIS/LS 라우팅 가능한 주식 시장코드(일본 TSE·홍콩 HKS는 dead branch → 제외).
_KR_MARKETS = {"KOSPI", "KOSDAQ"}
_US_MARKETS = {"NAS", "NYS", "AMS"}


def asset_class_for_symbol(symbol: str, market: str) -> str | None:
    """심볼→자산군('kr_equity'|'kr_futures'|'us_equity'|'us_futures') 또는 None(미지원).

    선물은 core instrument_category가 권위(한글 상품명). 주식은 market으로 판별 —
    KR/US 지원 시장만 통과, 일본/홍콩 등은 None(자동매매 미지원)."""
    if qc.is_futures(symbol):
        return qc.instrument_category(symbol)        # kr_futures | us_futures
    if market in _KR_MARKETS:
        return "kr_equity"
    if market in _US_MARKETS:
        return "us_equity"
    return None


def capability_matrix() -> dict[str, Any]:
    """전 셀(broker→mode→asset_class→Capability dict). 웹이 1회 페치해 소비."""
    out: dict[str, Any] = {}
    for b in BROKERS:
        out[b] = {}
        for m in MODES:
            out[b][m] = {}
            for ac in ASSET_CLASSES:
                cap = autotrade_capability(b, m, ac)
                out[b][m][ac] = {"status": cap.status, "verified": cap.verified,
                                 "reason": cap.reason, "setup_hint": cap.setup_hint}
    return out
