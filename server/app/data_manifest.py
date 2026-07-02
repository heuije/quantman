"""데이터셋 → DataManifest 빌드 (Phase 3) — 실측 메타를 채워 무결성 게이트를 가동한다.

cron이 parquet을 갱신하면 data_cache.invalidate() → 다음 로드 시 이 빌더가 매니페스트를
재생성한다(수급↔스키마 정합). 피드별 source/adjustment **정책은 단일 출처**인
quant_core.data.feeds(PRICE_FEED_POLICY)에서 읽는다 — 코드 곳곳에 흩뿌리지 않는다.
통화·시장은 KIS master(권위 메타)에서, 없으면 코드 휴리스틱으로 채운다.
"""

from __future__ import annotations

from quant_core import data_fetcher as _df
from quant_core.data import DataManifest, build_manifest, coverage_report
from quant_core.data import spec as _spec
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
    # track_fields — sourced sparse 필드의 종목별 가용성 측정(null≠0 노출용). _FIELD_GROUPS에서
    # 파생(TRACK_FIELDS) = 단일 출처: 새 필드군은 _FIELD_GROUPS 한 곳에 등록하면 매니페스트 추적과
    # 챗 인벤토리가 함께 배선된다(드리프트 가드가 spec와의 정합을 CI에서 강제).
    # 가격파생 BASE 지표는 가격 있는 곳엔 항상 재계산 가능하므로 추적 제외(편향 무관).
    return build_manifest(dataset, version=version, symbol_meta=sym_meta, feeds=feeds,
                          track_fields=list(TRACK_FIELDS),
                          has_membership_history=False, has_pit=fund_has_asof, delay=1)


# ── 커버리지 인벤토리 (챗 LLM 화이트리스트) ────────────────────────────────────
# 검증 매니페스트(실측) → 데이터 유형별 커버리지 리스트. 챗 프롬프트에 주입돼
# "여기 없으면 미지원"의 화이트리스트가 된다. 하드코딩 문자열이 아니라 엔진 실측 뎁스.

def _range_over(manifest: DataManifest, symbols: list[str]) -> tuple[str | None, str | None, int]:
    """주어진 심볼들의 매니페스트 커버리지 = min(first)~max(last) + 데이터 있는 심볼 수."""
    firsts, lasts, n = [], [], 0
    for s in symbols:
        sm = manifest.symbols.get(s)
        if sm is not None and sm.n_rows and sm.first_date and sm.last_date:
            firsts.append(sm.first_date)
            lasts.append(sm.last_date)
            n += 1
    if not firsts:
        return None, None, 0
    return min(firsts), max(lasts), n


def _spec_meta(key: str) -> tuple[str, str | None]:
    """유형키 → (label, source). data_spec REGISTRY가 정본. 미등록이면 키 자체를 라벨로."""
    s = _spec.get(key)
    if s is None:
        return key, None
    return s.label, s.source


def _spec_floor(key: str) -> str | None:
    """유형키 → 정책 목표 floor(ISO) — spec.floor. 실측 first와 비교해 백필 진행을 정직 노출."""
    s = _spec.get(key)
    return getattr(s, "floor", None) if s is not None else None


# 매크로 유형 중 주요 심볼 개별 뎁스를 상세 노출할 유형(챗이 크로스에셋 참조 시 실측 확인).
_MACRO_DETAIL_KEYS = {"macro.krx"}

# 필드 커버리지(sparse) → 유형키·라벨. coverage_report의 필드는 펀더/플로우/컨센서스 컬럼.
from quant_core.indicators import (  # noqa: E402
    FUND_INDICATOR_COLS as _FUND, FLOW_INDICATOR_COLS as _FLOW,
    CONSENSUS_INDICATOR_COLS as _CONS,
)
_FIELD_GROUPS: list[tuple[str, str, set]] = [
    ("fundamental.equity", "펀더멘털(밸류·재무)", set(_FUND)),
    ("flow.kr_investor", "수급(기관·외국인 순매수)", set(_FLOW)),
    ("estimate.consensus", "애널 컨센서스(목표가·투자의견)", set(_CONS)),
]

# 매니페스트 field_coverage 추적 대상 — _FIELD_GROUPS 유래(단일 출처, 드리프트 가드 대상).
TRACK_FIELDS: list[str] = [c for _, _, cols in _FIELD_GROUPS for c in sorted(cols)]


def _entry(key: str, label: str, depth: str, n_symbols: int,
           source: str | None, pct: float | None = None, detail: list | None = None,
           first: str | None = None) -> dict:
    e = {"key": key, "label": label, "depth": depth, "n_symbols": n_symbols, "source": source}
    if pct is not None:
        e["pct"] = pct
    if detail:
        e["detail"] = detail
    # 목표 floor 미달(실측 first > spec.floor)이면 백필 진행중 — 챗이 깊이를 과신하지 않게 노출.
    floor = _spec_floor(key)
    if floor and first and first > floor:
        e["target_floor"] = floor
    return e


def coverage_inventory(manifest: DataManifest | None) -> list[dict]:
    """검증 매니페스트 → 데이터 유형별 실측 커버리지 리스트(챗 화이트리스트).

    각 항목: {key, label, depth("first~last"), n_symbols, pct?, source, detail?}.
    - 가격·매크로 유형: data_type_symbols() 심볼들의 매니페스트 min(first)~max(last) + count.
    - 주식: KR(숫자코드)/US(그 외 가격피드 종목)로 per-symbol 집계.
    - sparse 필드(펀더/플로우/컨센서스): coverage_report의 pct + 글로벌 range.
    매니페스트 None(콜드스타트)이면 [] — 프롬프트가 인벤토리 섹션을 graceful 생략.
    """
    if manifest is None:
        return []

    out: list[dict] = []
    type_syms = _df.data_type_symbols()

    # ① 가격·매크로 유형 (per-symbol first/last 집계) — data_spec 순서 근사(P1→P4).
    for key in ("ohlcv.crypto", "ohlcv.futures",
                "macro.market", "macro.fred", "macro.fred_lagged", "macro.krx"):
        syms = type_syms.get(key, [])
        first, last, n = _range_over(manifest, syms)
        if n == 0:
            continue
        label, source = _spec_meta(key)
        detail = None
        if key in _MACRO_DETAIL_KEYS:                # 매크로는 주요 심볼 개별 뎁스까지 노출
            detail = []
            for s in syms:
                sm = manifest.symbols.get(s)
                if sm is not None and sm.n_rows and sm.first_date and sm.last_date:
                    detail.append({"symbol": s, "depth": f"{sm.first_date}~{sm.last_date}"})
        out.append(_entry(key, label, f"{first}~{last}", n, source, detail=detail, first=first))

    # ② 주식 (동적 유니버스) — 가격 피드가 있는 종목을 KR(숫자6자)/US로 집계.
    kr_syms = [s for s, sm in manifest.symbols.items()
               if sm.feed == "ohlcv.kr" and sm.n_rows and sm.first_date]
    us_syms = [s for s, sm in manifest.symbols.items()
               if sm.feed == "ohlcv.us" and sm.n_rows and sm.first_date]
    for key, syms in (("ohlcv.kr", kr_syms), ("ohlcv.us", us_syms)):
        first, last, n = _range_over(manifest, syms)
        if n == 0:
            continue
        label, source = _spec_meta(key)
        out.append(_entry(key, label, f"{first}~{last}", n, source, first=first))

    # ③ sparse 필드(펀더/플로우/컨센서스) — coverage_report의 pct + 글로벌 range.
    report = coverage_report(manifest)              # {field: {covered, total, pct, first, last}}
    for key, label, cols in _FIELD_GROUPS:
        covered = {f: report[f] for f in cols if f in report}
        if not covered:
            continue
        firsts = [c["first"] for c in covered.values()]
        lasts = [c["last"] for c in covered.values()]
        # 필드 그룹 커버리지 = 유형 내 필드 중 최대 pct(가장 잘 채워진 필드 기준)·글로벌 range.
        pct = max(c["pct"] for c in covered.values())
        n_sym = max(c["covered"] for c in covered.values())
        _, source = _spec_meta(key)
        out.append(_entry(key, label, f"{min(firsts)}~{max(lasts)}", n_sym, source, pct=pct,
                          first=min(firsts)))

    return out


def inventory_for_prompt(inventory: list[dict]) -> str:
    """coverage_inventory → 챗 시스템 프롬프트용 compact 렌더(유형별 1줄)."""
    lines = []
    for e in inventory:
        line = f"- {e['label']}: {e['depth']} · {e['n_symbols']}종목"
        if e.get("pct") is not None:
            line += f"(커버리지 {e['pct']}%)"
        if e.get("target_floor"):
            line += f" (목표 {e['target_floor']}~ 백필 진행중)"
        if e.get("source"):
            line += f" [{e['source']}]"
        lines.append(line)
        for d in (e.get("detail") or []):
            lines.append(f"    · {d['symbol']}: {d['depth']}")
    return "\n".join(lines)
