"""IR 백테스트 요청 처리 — HTTP/DB와 분리된 순수 핵심 (서버 라우터가 감쌈).

명세 §11(처리절차)·§5·§6. 요청 spec(dict) → Node 파싱 → 메타규칙+무결성 검증 →
기본값 주입 → run_backtest_ir → 결과(+경고). 서버/웹 모두 이 함수를 통한다.

순수 함수라 헤드리스 단위테스트 가능(test_engine_service.py).
"""

from __future__ import annotations

import pandas as pd

from ..blocks import (
    DatasetMeta, Node, apply_defaults, available_refs, integrity_issues, validate,
)
from ..blocks.validate import prioritize
from .backtest import run_backtest_ir
from .run import run_query
from .spec import StrategyIR, validate_strategy

# run_backtest_ir로 그대로 전달할 청산/체결/기간 파라미터 (None이면 drop → 엔진 기본값).
_EXIT_KW = (
    "hold_days", "take_profit", "stop_loss", "trail_atr_mult", "trail_pct",
    "sell_amount_pct", "rule_sell_pcts", "fill", "commission", "slippage",
    "sell_tax", "initial_capital", "start", "end",
)


def _issue_dict(i) -> dict:
    return {"rule": i.rule, "severity": i.severity, "message": i.message, "path": i.path}


def _futures_capital_warning(s: StrategyIR, dataset: dict, res: dict) -> dict | None:
    """선물 1계약 증거금 > 가용예산이면 진입이 0건이 되는데, 엔진이 조용히 0거래로 보여
    '왜 0인지' 오해를 부른다 — 명시 경고로 표면화한다. 엔진 증거금 사이징(계약수=int(예산/
    (가격×승수×개시증거금률)), 라이브 패리티)은 정상이고, 이건 자본 설정이 계약 명목 대비
    과소하다는 안내일 뿐(사이징 로직 무관·골든 무영향 — 거래>0이면 발동 안 함).

    조건(거짓이면 None): query=simulate · 거래 0건 · 유니버스의 선물 1계약 증거금 > 예산.
    예산 = 초기자본 × futures_margin_pct/100 (엔진 _budget과 동일 식)."""
    if s.query != "simulate":
        return None
    metrics = res.get("metrics") or {}
    if metrics.get("n_trades"):              # 거래가 있었으면 자본은 충분했음 → 무경고
        return None
    from ..exec_defaults import instrument_spec, is_futures
    cap = float(s.simulation.initial_capital)
    fmp = float(s.position.sizing.futures_margin_pct) / 100.0
    budget = cap * fmp
    for sym in s.universe.symbols:
        if not is_futures(sym):
            continue
        df = dataset.get(sym)
        if df is None or "Close" not in getattr(df, "columns", []):
            continue
        px = df["Close"].dropna()
        if px.empty:
            continue
        ispec = instrument_spec(sym)
        margin = float(px.iloc[-1]) * ispec.multiplier * ispec.init_margin_rate
        if budget < margin:
            need = margin / fmp if fmp > 0 else margin
            return {"rule": "capital", "severity": 25, "path": "simulation.initial_capital",
                    "message": (f"초기자본 {cap:,.0f}원으로는 {sym} 1계약 증거금"
                                f"(약 {margin:,.0f}원)을 충당하지 못해 진입이 0건입니다. "
                                f"초기자본을 약 {need:,.0f}원 이상으로 올리거나 사이징의 "
                                f"futures_margin_pct를 높이세요.")}
    return None


def _parse(spec: dict, key: str):
    raw = spec.get(key)
    if raw is None:
        return None, None
    try:
        return Node.model_validate(raw), None
    except Exception as e:  # noqa: BLE001 — 사용자 입력 파싱 실패는 메시지로 반환
        return None, f"{key} 파싱 오류: {e}"


def backtest_from_spec(
    spec: dict,
    dataset: dict[str, pd.DataFrame],
    *,
    valid_refs: set[str] | None = None,
    meta: DatasetMeta | None = None,
) -> dict:
    """IR 전략 spec을 검증·실행. 실패 시 {success:False, error, issues}.

    spec: {trade_symbol, buy(Node dict), sell?(Node dict), 청산/체결 파라미터...}
    valid_refs: 규칙0 데이터 가용성 검사용 (없으면 dataset에서 자동 생성).
    meta: 무결성 검사용 DatasetMeta (없으면 기본값 — delay=1).
    """
    trade_symbol = spec.get("trade_symbol")
    if not trade_symbol:
        return {"success": False, "error": "trade_symbol이 필요합니다."}
    if "buy" not in spec or spec.get("buy") is None:
        return {"success": False, "error": "매수 신호(buy)가 필요합니다."}

    buy, err = _parse(spec, "buy")
    if err:
        return {"success": False, "error": err}
    sell, err = _parse(spec, "sell")
    if err:
        return {"success": False, "error": err}

    if valid_refs is None:
        valid_refs = available_refs(dataset)
    if meta is None:
        meta = DatasetMeta()

    # 규칙0·1·2·3 (메타규칙) + 규칙4 (무결성)
    issues = list(validate(buy, valid_refs)) + list(integrity_issues(buy, meta))
    if sell is not None:
        issues += list(validate(sell, valid_refs)) + list(integrity_issues(sell, meta))
    issues = prioritize(issues)
    errors = [i for i in issues if i.is_error]
    if errors:
        return {"success": False, "error": errors[0].message,
                "issues": [_issue_dict(i) for i in issues]}

    # 규칙5 — 빈칸 기본값 주입
    buy = apply_defaults(buy)
    sell = apply_defaults(sell) if sell is not None else None

    exit_kw = {k: spec[k] for k in _EXIT_KW if k in spec and spec[k] is not None}
    res = run_backtest_ir(dataset, trade_symbol, buy, sell_node=sell, **exit_kw)
    # 비-error 무결성 경고(예: 펀더멘털 PIT 미태깅)를 결과에 동봉
    res["warnings"] = list(res.get("warnings") or []) + [_issue_dict(i) for i in issues]
    return res


def _field_coverage_summary(manifest, symbols: list) -> dict:
    """관련 종목들의 비가격 필드 '부분 커버리지'를 요약 — {field: {"covered","total"}}.

    다종목 쿼리에서 일부 종목만 가진 필드(시총·펀더·수급)는 랭킹·집계를 편향시킨다. 완전
    (covered==total)·전무(covered==0)는 생략하고 *부분만* 노출 → 챗봇이 결손을 0으로 오해하지
    않게(null≠0). 단일 종목은 '부분' 개념이 없어 빈 dict(게이트·describe 리포트가 담당).
    """
    if len(symbols) < 2:
        return {}
    counts: dict = {}
    for sym in symbols:
        sm = manifest.symbol(sym)
        if sm is None:
            continue
        for f in sm.field_coverage:
            counts[f] = counts.get(f, 0) + 1
    total = len(symbols)
    return {f: {"covered": c, "total": total}
            for f, c in sorted(counts.items()) if 0 < c < total}


def strategy_from_spec(
    spec: dict,
    dataset: dict[str, pd.DataFrame],
    *,
    valid_refs: set[str] | None = None,
    meta: DatasetMeta | None = None,
    manifest=None,
    strict: bool = False,
    strategy_resolver=None,
) -> dict:
    """완전한 StrategyIR(dict)을 검증·실행. 단일/팩터/포트폴리오/펼침 모두 처리.

    query/study가 단일·펼침(resultset)·분석·기간분할 경로를 결정(run_query 디스패치).
    manifest 제공 시 데이터 무결성 4액션 게이트(생존편향·조정·PIT·가용성·캘린더)를 함께 적용.
    strict=True면 편향형 경고를 거부로 승격(실전 자금 투입 前 게이트).
    strategy_resolver(token)->자식 spec: strat:<id> 합성 자산을 물질화(전략 조합 G3). server가 주입.
    """
    try:
        s = StrategyIR.model_validate(spec)
    except Exception as e:  # noqa: BLE001 — 사용자 입력 파싱 실패
        return {"success": False, "error": f"전략 파싱 오류: {e}"}

    # 전략 조합 — strat:<id> 참조를 자식 equity로 물질화. 공유 캐시 보호 위해 사본에 주입.
    from .compose import has_strat_refs, materialize_strategy_assets
    if has_strat_refs(s):
        try:
            dataset = materialize_strategy_assets(s, dict(dataset), strategy_resolver)
        except ValueError as e:
            return {"success": False, "error": f"전략 조합 오류: {e}"}
        valid_refs = None   # 합성 심볼 포함해 재계산

    if valid_refs is None:
        valid_refs = available_refs(dataset)
    # manifest 제공 시 그 무결성 플래그로 meta 도출(PIT는 게이트가 strict-인지로 소유 → has_pit=True 위임차단)
    if manifest is not None and meta is None:
        meta = DatasetMeta(delay=manifest.delay, has_pit=True,
                           has_membership_history=manifest.has_membership_history)
    issues = list(validate_strategy(s, valid_refs, meta))
    if manifest is not None:
        from ..data import evaluate_data_soundness  # 지연 import — 선택적 데이터 계층 의존
        issues = prioritize(issues + list(evaluate_data_soundness(s, manifest, strict=strict)))
    errors = [i for i in issues if i.is_error]
    if errors:
        return {"success": False, "error": errors[0].message,
                "issues": [_issue_dict(i) for i in issues]}

    # 최상위 디스패치 — query(동사) + study(펼침)로 단일/펼침/분석/기간분할 경로 선택.
    res = run_query(s, dataset)
    if res.get("success"):
        warns = list(res.get("warnings") or []) + [_issue_dict(i) for i in issues]   # run_query(무거래 등) 보존 후 병합
        from .data_quality import assess_data_quality   # Phase 0.5 — 실행 전 데이터 품질 불변식
        from .run import relevant_symbols                # 평가를 전략 관련 심볼로 한정(매크로 노이즈 제거)
        rel = relevant_symbols(s, dataset)
        warns += assess_data_quality(dataset, start=getattr(s.simulation, "start", None),
                                     end=getattr(s.simulation, "end", None), relevant=rel)
        try:
            cap_warn = _futures_capital_warning(s, dataset, res)
        except Exception:   # noqa: BLE001 — 부가 경고 계산 실패가 정상 백테스트 결과를 깨지 않게(보조 정보)
            cap_warn = None
        if cap_warn:
            warns.append(cap_warn)
        res["warnings"] = warns
        if manifest is not None:                # 필드 부분커버리지 → 결과계약 노출(null≠0·편향가드)
            fc = _field_coverage_summary(manifest, rel)
            if fc:
                res.setdefault("diagnostics", {})["field_coverage"] = fc
    return res
