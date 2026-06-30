"""데이터셋 → DataManifest 빌드 (Phase 3) — 실측 메타를 채워 무결성 게이트를 가동한다.

cron이 parquet을 갱신하면 data_cache.invalidate() → 다음 로드 시 이 빌더가 매니페스트를
재생성한다(수급↔스키마 정합). 피드별 source/adjustment **정책은 단일 출처**인
quant_core.data.feeds(PRICE_FEED_POLICY)에서 읽는다 — 코드 곳곳에 흩뿌리지 않는다.
통화·시장은 KIS master(권위 메타)에서, 없으면 코드 휴리스틱으로 채운다.
"""

from __future__ import annotations

from quant_core import data_fetcher as _df
from quant_core.data import DataManifest, build_manifest
from quant_core.exec_defaults import instrument_spec as _instrument_spec
from quant_core.data.feeds import (
    PRICE_FEED_POLICY,
    classification as _cls,
    classify_price_feed,
    listing as _lst,
)

from . import kis_master_cache


def build_dataset_manifest(dataset: dict, *, version: int = 0) -> DataManifest:
    macro = set(_df.MACRO_SYMBOLS)
    fdr_assets, yf_assets = set(_df.FDR_SYMBOLS), set(_df.YFINANCE_SYMBOLS)
    try:
        master = {m["symbol"]: m for m in kis_master_cache.get_master_list()}
    except Exception:                            # noqa: BLE001 — 마스터 미갱신 시 휴리스틱 폴백
        master = {}

    cls_map = _cls.load()                        # static.classification 사이드카(섹터·업종)
    lst_map = _lst.load()                        # static.listing 사이드카(상장·폐지일)

    sym_meta: dict = {}
    feed_count: dict = {}
    for sym in dataset:
        feed = classify_price_feed(sym, macro=macro, fdr_assets=fdr_assets, yf_assets=yf_assets)
        meta: dict = {}
        mm = master.get(sym)
        if mm:                                   # KIS master 권위 통화·시장
            if mm.get("currency"):
                meta["currency"] = mm["currency"]
            if mm.get("market"):
                meta["market"] = mm["market"]
        if feed:
            policy = PRICE_FEED_POLICY[feed]
            meta["feed"] = feed
            meta["adjustment"] = policy.adjustment
            meta["calendar"] = policy.calendar
            # 통화 SSOT — instrument_spec(국내선물=KRW·해외선물=USD·주식은 숫자코드 휴리스틱).
            meta.setdefault("currency", _instrument_spec(sym).currency)
            feed_count[feed] = feed_count.get(feed, 0) + 1
        crec = cls_map.get(sym)                   # 섹터(업종 우선) — 그룹 블록·게이트 참조
        if crec:
            meta["sector"] = crec.get("Industry") or crec.get("Sector")
        lrec = lst_map.get(sym)                   # 상장·폐지일 — 생존편향·워밍업 충분성
        if lrec:
            if lrec.get("listing_date"):
                meta["listing_date"] = lrec["listing_date"]
            if lrec.get("delisting_date"):
                meta["delisting_date"] = lrec["delisting_date"]
        if meta:
            sym_meta[sym] = meta

    feeds = {k: {"source": PRICE_FEED_POLICY[k].source, "adjustment": PRICE_FEED_POLICY[k].adjustment,
                 "status": "ok", "n_symbols": n} for k, n in feed_count.items()}

    # 펀더멘털 피드 — 종목 df에 펀더멘털 컬럼이 붙어 있으면 수급된 것(SEC US·OpenDART KR).
    import pandas as pd
    from quant_core.indicators import (FUND_INDICATOR_COLS, FLOW_INDICATOR_COLS,
                                       CONSENSUS_INDICATOR_COLS)
    fund_syms = [s for s, df in dataset.items()
                 if df is not None and any(c in df.columns for c in FUND_INDICATOR_COLS)]
    # has_as_of 실측 — 펀더멘털 parquet이 실제 as_of(제출일) 인덱스를 갖는지 확인(단언 아님).
    # compute_fundamentals가 as_of 인덱스를 보장하지만, 향후 비-PIT 소스 유입 시 정직히 False가
    # 되도록 측정한다(D-pit가 실 저하를 감지할 수 있게). 첫 유효 종목에서 조기 종료.
    fund_has_asof = False
    for s in fund_syms:
        fdf = _df.load_stock_fundamentals(s)
        if not fdf.empty and isinstance(fdf.index, pd.DatetimeIndex) and len(fdf.index):
            fund_has_asof = True
            break
    if fund_syms:
        feeds["fundamental.equity"] = {"source": "SEC Company Facts(US) + OpenDART(KR)",
                                       "status": "ok", "has_as_of": fund_has_asof,
                                       "n_symbols": len(fund_syms)}

    # has_membership_history=False — 멤버십 이력 미연동(정직). has_pit은 펀더멘털 as_of 실측으로 판정.
    # track_fields — sourced sparse 필드(펀더/플로우/컨센서스)의 종목별 가용성 측정(null≠0 노출용).
    # 가격파생 BASE 지표는 가격 있는 곳엔 항상 재계산 가능하므로 추적 제외(편향 무관).
    return build_manifest(dataset, version=version, symbol_meta=sym_meta, feeds=feeds,
                          track_fields=(list(FUND_INDICATOR_COLS) + list(FLOW_INDICATOR_COLS)
                                        + list(CONSENSUS_INDICATOR_COLS)),
                          has_membership_history=False, has_pit=fund_has_asof, delay=1)
