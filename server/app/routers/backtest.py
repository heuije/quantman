"""종목 목록 라우터 — 전략 빌더(IR 연구소)용 종목 union 제공.

(레거시 operand /backtest/run·/analysis/run·/backtest/runs 엔드포인트는 IR 단일 체제
전환으로 제거됨. IR 백테스트는 /ir/* 라우터가 담당.)
"""

from __future__ import annotations

import quant_core as qc
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from .. import data_cache, kis_master_cache
from ..deps import get_current_user
from ..models import User

router = APIRouter(tags=["symbols"])


# /symbols 응답 캐시 — (dataset 버전, 마스터 갱신시각) 키로 1회 빌드·직렬화 후 재사용.
# 데이터가 실제로 바뀔 때(=키 변동)만 재빌드되므로 하루 몇 번 갱신돼도 항상 최신.
# 큰 페이로드라 dict가 아닌 인코딩된 bytes를 캐시해 재직렬화 비용까지 없앤다.
_symbols_cache: tuple[tuple[int, int], bytes] | None = None


def _symbols_version_key() -> tuple[int, int]:
    return (data_cache.get_version(), kis_master_cache.get_fetched_epoch())


def _build_symbols_payload() -> dict:
    """빌더용 종목 union을 만든다. 경량 심볼 인덱스(parquet 메타)로 빌드 — 전체
    지표계산(load_dataset, 22k×compute ≈ 8.5분) 없이 종목 목록만 구성한다.

    1) KIS 마스터의 매수 가능 종목 (tradable)
    2) 데이터 보유 종목 (인덱스의 has_ohlc)

    지표 메타는 종목마다 동일(전역)하므로 per-symbol 배열 대신 indicator_catalog로
    1회만 보낸다(이전엔 22k× 중복 직렬화 = 43.5MB). 빌더는 이 카탈로그로 지표 드롭다운을
    구성하고, 종목별 실제 보유 여부는 백테스트/검증 시점에 확정된다.
    """
    index = data_cache.get_symbol_index()      # {sym: {rows, has_ohlc}} — 지표계산 없음

    master_list = kis_master_cache.get_master_list()
    has_master = len(master_list) > 0
    master_by_code = {m["symbol"]: m for m in master_list}

    out = []
    seen: set[str] = set()

    def _category(market: str, kind: str) -> str:
        """카테고리 라벨 — 시장 + 유형 결합."""
        kind_label = {"stock": "주식", "etf_etn": "ETF/ETN",
                       "reits": "REITs"}.get(kind, "주식")
        region = {
            "KOSPI": "국내", "KOSDAQ": "국내",
            "NAS": "미국 NASDAQ", "NYS": "미국 NYSE", "AMS": "미국 AMEX",
            "TSE": "일본", "HKS": "홍콩",
        }.get(market, "")
        if market in ("KOSPI", "KOSDAQ"):
            return f"국내{kind_label} ({market})"
        return f"{region} {kind_label}".strip()

    # 1) 데이터 보유 종목 (인덱스). 마스터에도 있으면 tradable.
    for sym in sorted(index):
        has_ohlc = index[sym]["has_ohlc"]
        # 클래스주 심볼로지: dataset은 대시(BRK-B), 마스터는 슬래시(BRK/B) →
        # 정규화 조회해야 매칭(안 하면 Berkshire 등이 tradable=False가 됨).
        meta = master_by_code.get(sym) or master_by_code.get(sym.replace("-", "/")) or {}
        in_master = bool(meta)
        kind = meta.get("kind", "stock")
        out.append({
            "symbol": sym,
            "name": meta.get("name", ""),
            "category": (_category(meta.get("market", ""), kind) if in_master
                          else qc.symbol_category(sym)),
            "tradable": in_master and has_ohlc,
            "has_backtest_data": has_ohlc,
            "asset_class": "futures" if qc.is_futures(sym) else "equity",
            "kind": kind if in_master else None,
            "rows": index[sym]["rows"],
        })
        seen.add(sym)

    # 2) 마스터에는 있지만 데이터 없는 종목 — 라이브 매매만 가능
    for code, meta in master_by_code.items():
        if code in seen:
            continue
        # §4.8: 미국은 데이터 보유분만 selectable로 노출(데이터 없는 ~1만+ 미국 종목 제외).
        if meta.get("market") in ("NAS", "NYS", "AMS"):
            continue
        kind = meta.get("kind", "stock")
        out.append({
            "symbol": code,
            "name": meta.get("name", ""),
            "category": _category(meta.get("market", ""), kind),
            "tradable": True,
            "has_backtest_data": False,
            "asset_class": "equity",        # KIS 주식 마스터 — 선물 아님
            "kind": kind,
            "rows": 0,
        })

    # 전역 지표 카탈로그 — 컬럼별 메타(종목 무관). 빌더 지표 드롭다운용, 1회 전송.
    indicator_catalog = [{
        "key": c,
        "label": qc.get_indicator_label(c),
        "group": qc.get_indicator_group(c),
        "unit": qc.get_indicator_unit(c),
        "compare_group": qc.get_indicator_compare_group(c),
    } for c in qc.get_all_indicator_columns()]

    return {"symbols": out, "indicator_catalog": indicator_catalog,
            "has_master": has_master, "master_status": kis_master_cache.get_status()}


@router.get("/symbols")
def list_symbols(request: Request, user: User = Depends(get_current_user)):
    """전략 빌더용 종목 목록. 데이터 변경 시점에만 재빌드되는 캐시 + ETag.

    같은 데이터에 대해선 서버가 재계산·재직렬화를 건너뛰고, 브라우저는
    If-None-Match가 일치하면 304(본문 없음)로 받아 전송 비용도 사라진다.
    """
    global _symbols_cache
    key = _symbols_version_key()
    if _symbols_cache is None or _symbols_cache[0] != key:
        body = JSONResponse(_build_symbols_payload()).body
        _symbols_cache = (key, body)
    body = _symbols_cache[1]

    etag = f'W/"symbols-{key[0]}-{key[1]}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
