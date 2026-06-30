"""DataManifest — 적재된 데이터셋의 **실측 메타** (DataSpec 요구에 대응하는 '실제 보유' 기록).

무결성 게이트가 *요구(DataSpec) vs 실측(Manifest)*를 비교해 거부/경고/보정을 결정한다.
cron이 수급 시 채우고(`core/data/_manifest.json` 사이드카), 코어가 읽는다 — **서버 DB 비의존**.

3계층:
  - 전역      : 데이터셋 버전·생성시각·무결성 플래그(delay·PIT·멤버십 이력)
  - per-feed  : 피드별 출처·수급시각·실측 조정수준·상태(ok/partial/failed/absent)
  - per-symbol: 종목별 시장·통화·캘린더·섹터·커버리지(first/last/n_rows)·상장폐지일

스키마 자체는 pydantic만 의존(pandas 비의존). build_manifest만 지연 import로 DataFrame을 읽는다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .spec import Adjustment

FeedStatus = Literal["ok", "partial", "failed", "absent"]


class FeedManifest(BaseModel):
    """한 피드(수급 단위)의 실측 상태 — cron이 수급 직후 기록."""
    key: str                                  # DataSpec 피드 키 (예: "ohlcv.kr")
    source: Optional[str] = None              # 실제 사용 소스
    fetched_at: Optional[str] = None          # 마지막 수급 시각(ISO)
    status: FeedStatus = "ok"
    adjustment: Adjustment = "not_applicable"  # 실측 조정 수준(요구와 비교됨)
    has_as_of: bool = False                   # PIT as_of 병기 여부
    n_symbols: int = 0
    error: Optional[str] = None               # status!=ok 시 사유


class SymbolManifest(BaseModel):
    """한 종목의 실측 메타. 가격 유도값(날짜·행수)은 build_manifest가, 나머지는 cron이 채운다."""
    symbol: str
    feed: Optional[str] = None                # 이 종목의 가격 피드 키
    market: Optional[str] = None
    currency: Optional[str] = None
    calendar: Optional[str] = None
    sector: Optional[str] = None
    adjustment: Optional[Adjustment] = None
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    n_rows: int = 0
    listing_date: Optional[str] = None
    delisting_date: Optional[str] = None
    # 비가격 필드(펀더/플로우/컨센서스)의 실측 가용성 {field: {"first","last","n"}}.
    # sparse 커버리지를 종목×필드×기간으로 노출 → 챗봇이 결손을 0으로 오해 못 하게(null≠0).
    field_coverage: dict[str, dict] = Field(default_factory=dict)


class DataManifest(BaseModel):
    """데이터셋 1개의 전체 실측 메타."""
    version: int = 0
    built_at: Optional[str] = None
    # 무결성 전역 플래그 (기존 DatasetMeta 흡수)
    delay: int = 1
    has_pit: bool = False
    has_membership_history: bool = False
    feeds: dict[str, FeedManifest] = Field(default_factory=dict)
    symbols: dict[str, SymbolManifest] = Field(default_factory=dict)

    # ── 조회 헬퍼 (게이트가 사용) ──
    def feed(self, key: str) -> Optional[FeedManifest]:
        return self.feeds.get(key)

    def feed_status(self, key: str) -> FeedStatus:
        f = self.feeds.get(key)
        return f.status if f is not None else "absent"

    def symbol(self, sym: str) -> Optional[SymbolManifest]:
        return self.symbols.get(sym)

    def calendars(self, syms: list[str]) -> set[str]:
        """주어진 종목들의 캘린더 집합 — 혼합 캘린더(달력 희소화) 탐지용."""
        return {self.symbols[s].calendar for s in syms
                if s in self.symbols and self.symbols[s].calendar}


# ── 빌드 (가격 DataFrame에서 유도 가능한 것 + 외부 메타 머지) ──────────────────

def _dstr(x) -> str:
    """DatetimeIndex 값 → 'YYYY-MM-DD'(date 속성 있으면) 또는 str."""
    return str(x.date()) if hasattr(x, "date") else str(x)


def build_manifest(dataset: dict, *, version: int = 0,
                   feeds: Optional[dict] = None,
                   symbol_meta: Optional[dict] = None,
                   track_fields: Optional[list] = None, **flags) -> DataManifest:
    """dataset(dict[sym]->DataFrame)에서 per-symbol 커버리지(날짜·행수)를 유도해 매니페스트 생성.

    가격 DataFrame만으로는 통화·시장·섹터·출처·조정수준을 알 수 없다(정직) — symbol_meta·feeds로
    외부에서 주입(cron이 KIS master·소스 정보로 채움). 주입 안 된 필드는 None(미상)으로 둔다.

    track_fields: 비가격 필드(펀더/플로우/컨센서스 컬럼) 목록 — 주면 종목별 비결측 first/last/n을
    field_coverage에 기록한다(null≠0 노출용). None이면 가격 커버리지만 기록(기존 동작·하위호환).
    """
    from datetime import datetime, timezone   # 지연 import — 스키마 경량 유지

    symbol_meta = symbol_meta or {}
    syms: dict[str, SymbolManifest] = {}
    for s, df in dataset.items():
        if df is None or len(df) == 0:
            sm = SymbolManifest(symbol=s, n_rows=0)
        else:
            idx = df.index
            lo, hi = idx.min(), idx.max()
            fc: dict[str, dict] = {}
            if track_fields:
                for f in track_fields:
                    if f in df.columns:
                        nn = df[f].notna()
                        n = int(nn.sum())
                        if n:                 # 비결측이 하나도 없으면 '미보유'로 둔다(키 자체 생략)
                            present = df.index[nn.to_numpy()]
                            fc[f] = {"first": _dstr(present.min()),
                                     "last": _dstr(present.max()), "n": n}
            sm = SymbolManifest(
                symbol=s, n_rows=int(len(df)),
                first_date=_dstr(lo), last_date=_dstr(hi),
                field_coverage=fc)
        if s in symbol_meta:                  # 외부 메타 머지(통화·시장·섹터·feed 등)
            sm = sm.model_copy(update=symbol_meta[s])
        syms[s] = sm

    feed_objs = {k: (v if isinstance(v, FeedManifest) else FeedManifest(**{"key": k, **v}))
                 for k, v in (feeds or {}).items()}
    return DataManifest(version=version,
                        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        feeds=feed_objs, symbols=syms, **flags)


def coverage_report(manifest: "DataManifest") -> dict:
    """전 종목 매니페스트 → 필드별 글로벌 커버리지 {field: {covered, total, pct, first, last}}.

    비가격 필드(펀더·플로우·컨센서스)의 유니버스 커버리지를 집계 — 백필 진행률·무결손 SLA의
    측정 기준선(P2/P3). field_coverage(build_manifest track_fields)가 채워진 매니페스트가 입력.
    """
    total = len(manifest.symbols)
    agg: dict = {}
    for sm in manifest.symbols.values():
        for f, cov in sm.field_coverage.items():
            a = agg.get(f)
            if a is None:
                agg[f] = {"covered": 1, "first": cov["first"], "last": cov["last"]}
            else:
                a["covered"] += 1
                a["first"] = min(a["first"], cov["first"])
                a["last"] = max(a["last"], cov["last"])
    for a in agg.values():
        a["total"] = total
        a["pct"] = round(100 * a["covered"] / total, 1) if total else 0.0
    return dict(sorted(agg.items()))


# ── 사이드카 IO ───────────────────────────────────────────────────────────────

def default_manifest_path() -> Path:
    """매니페스트 사이드카 경로 — 가격 parquet과 동일 디렉터리(QP_CORE_DATA_DIR 또는 core/data)."""
    base = os.environ.get("QP_CORE_DATA_DIR")
    root = Path(base) if base else Path(__file__).resolve().parents[2] / "data"
    return root / "_manifest.json"


def save_manifest(m: DataManifest, path: Optional[Path] = None) -> Path:
    path = Path(path) if path is not None else default_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(m.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_manifest(path: Optional[Path] = None) -> Optional[DataManifest]:
    path = Path(path) if path is not None else default_manifest_path()
    if not path.exists():
        return None
    return DataManifest.model_validate_json(path.read_text(encoding="utf-8"))
