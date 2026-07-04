"""데이터 무결성 4액션 게이트 (Phase 2).

required_data(전략) 요구 토큰을 **DataSpec(요구) × DataManifest(실측)**에 대조해
소n드니스 가이드라인의 4액션으로 판정한다:
  REJECT-HARD(SEV_INTEGRITY=40) · REJECT(SEV_ERROR=30) · WARN(SEV_INTEGRITY_WARN=25) · REPAIR/INFO(SEV_INFO=10)

편향형(생존편향·PIT·조정수준)은 기본 WARN, strict=True(실전 투입 前)면 REJECT-HARD로 승격.
REPAIR(유니버스 스코핑·결손종목 제외)는 INFO로 **반드시 기록**(silent 0).

분담: delay·비-causal 등 노드/시뮬 수준은 validate/integrity가, 컬럼 가용성(rsi_14 존재?)은
validate 규칙0가 본다. 이 게이트는 **매니페스트가 알려주는 데이터 차원**(시점·조정·생존편향·
가용성·캘린더)만 담당해 중복을 피한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..blocks.validate import (
    SEV_ERROR, SEV_INFO, SEV_INTEGRITY, SEV_INTEGRITY_WARN, Issue,
)
from . import spec as _spec
from .deps import required_data

if TYPE_CHECKING:
    from ..ir_engine.spec import StrategyIR
    from .manifest import DataManifest

_ADJ_REQUIRED = {"split_adjusted", "total_return"}

# 생존편향(D-surv)이 정의상 없는 현재-스냅샷 쿼리 — "오늘의 유니버스"만 보므로 상폐 종목 누락이
# 결과를 왜곡하지 않는다. 역사적 백테스트/관계분석(simulate·relate·prescribe)만 경고 대상.
_SNAPSHOT_QUERIES = {"breadth", "select", "describe"}


def _price_feeds(strategy, manifest) -> set[str]:
    """유니버스가 의존하는 가격 피드 키 집합 (조정수준 검사 대상)."""
    u = strategy.universe
    if u.kind in ("single", "list"):
        out = set()
        for s in u.symbols:
            sm = manifest.symbol(s)
            if sm is not None and sm.feed:
                out.add(sm.feed)
        return out
    return {k for k in manifest.feeds if k.startswith("ohlcv.")}


def evaluate_data_soundness(strategy: "StrategyIR", manifest: "DataManifest",
                            *, strict: bool = False) -> list[Issue]:
    """전략 × 매니페스트 → 데이터 무결성 이슈 목록(4액션)."""
    out: list[Issue] = []
    req = required_data(strategy)
    u = strategy.universe
    bias = SEV_INTEGRITY if strict else SEV_INTEGRITY_WARN   # 편향 게이트 승격

    # 1) 참조 외부 심볼(매크로·시장) 가용성 — 없으면 신호 평가 불가(거부)
    for tok in req:
        if tok.startswith("macro:"):
            sym = tok.split(":", 1)[1]
            if manifest.symbol(sym) is None:
                out.append(Issue("D-ref", SEV_ERROR,
                                 f"참조 데이터 '{sym}' 미수급 — 신호 평가 불가.", "signal"))

    # 2) 펀더멘털 — 가용성(거부) + PIT(편향)
    if "fundamental" in req:
        st = manifest.feed_status("fundamental.equity")
        if st in ("absent", "failed"):
            out.append(Issue("D-avail", SEV_ERROR,
                             "펀더멘털 데이터 미수급 — 펀더멘털 신호 사용 불가.", "signal"))
        else:
            f = manifest.feed("fundamental.equity")
            if f is not None and not f.has_as_of:
                out.append(Issue("D-pit", bias,
                                 "펀더멘털 PIT(as_of) 미태깅 — 미래 실적 누출 위험.", "signal"))

    # 3) 섹터 분류 — 그룹 블록 선행조건(거부)
    if "sector" in req:
        st = manifest.feed_status("static.classification")
        has_sector = any(sm.sector for sm in manifest.symbols.values())
        if st in ("absent", "failed") and not has_sector:
            out.append(Issue("D-avail", SEV_ERROR,
                             "섹터 분류 미수급 — 그룹 블록 실행 불가.", "signal"))

    # 4) 생존편향 — 전체/스크리너 유니버스 + 멤버십 이력 없음(편향).
    #    역사적 백테스트가 변화하는 유니버스를 소급 평가할 때만 왜곡된다 — 현재-스냅샷 쿼리
    #    (breadth 장세·select 스크리닝·describe 지표)는 "오늘의 유니버스"만 보므로 생존편향이
    #    정의상 없어 경고를 억제한다(과잉발화 방지: KOSPI 장세 질문마다 뜨던 노이즈 근본 제거).
    #    종목별 delisting_date는 manifest에 수집돼 있으나, 완전한 생존편향 해소는 상장폐지 종목의
    #    OHLCV를 유니버스에 편입해야 가능 — 후속 백로그. 현재는 u.kind로 정직히 경고.
    if (u.kind == "all" and not manifest.has_membership_history
            and strategy.query not in _SNAPSHOT_QUERIES):
        out.append(Issue("D-surv", bias,
                         "지수 구성 이력 없음 — 생존편향 가능(상장폐지 종목 누락).", "universe"))

    # 5) 가격 조정수준 — 요구(DataSpec) vs 실측(Manifest)
    if any(t.startswith("price.") for t in req) or "indicator.derived" in req:
        for k in _price_feeds(strategy, manifest):
            sp = _spec.get(k)
            if sp is None or sp.adjustment not in _ADJ_REQUIRED:
                continue
            fm = manifest.feed(k)
            actual = fm.adjustment if fm is not None else None
            if actual in (None, "not_applicable", "raw"):
                out.append(Issue("D-adj", bias,
                                 f"{k}: 조정수준 불일치(요구 {sp.adjustment}, 실측 "
                                 f"{actual or '미상'}) — 분할 가짜갭·총수익 왜곡 가능.", "data"))

    # 6) 유니버스 — 결손종목 제외(REPAIR) · 혼합 캘린더(REPAIR)
    if u.kind == "list":
        present = [s for s in u.symbols
                   if manifest.symbol(s) is not None and manifest.symbol(s).n_rows > 0]
        missing = [s for s in u.symbols if s not in present]
        if missing:
            out.append(Issue("D-repair", SEV_INFO,
                             f"데이터 없는 종목 제외하고 진행(REPAIR): {', '.join(missing)}", "universe"))
        if not present:
            out.append(Issue("D-avail", SEV_ERROR,
                             "리스트 유니버스에 데이터 보유 종목이 없습니다.", "universe"))
    if u.kind in ("list", "all"):
        syms = u.symbols if u.kind == "list" else list(manifest.symbols.keys())
        if len(manifest.calendars(syms)) > 1:
            out.append(Issue("D-repair", SEV_INFO,
                             "혼합 캘린더 — 유니버스 달력으로 스코핑 적용(REPAIR).", "universe"))

    # 7) 워밍업 충분성 — 상장일이 백테스트 시작 이후인 종목은 초기 구간 지표가 불완전(REPAIR/INFO).
    start = strategy.simulation.start
    if start:
        syms = u.symbols if u.kind in ("single", "list") else list(manifest.symbols.keys())
        late = []
        for s in syms:
            sm = manifest.symbol(s)
            if sm is not None and sm.listing_date and sm.listing_date > start:
                late.append(s)
        if late:
            shown = ", ".join(late[:5]) + ("…" if len(late) > 5 else "")
            out.append(Issue("D-repair", SEV_INFO,
                             f"상장이 백테스트 시작 이후 — 초기 구간 워밍업 부족({len(late)}종목): {shown}",
                             "universe"))

    return out
