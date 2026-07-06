"""챗 결과 context 사이드카 (P4) — 결정적 엔진 밖에서 결과를 준실시간 시세·뉴스로 enrich.

골든 불변식 보호: 엔진(strategy_from_spec)이 결과를 낸 **뒤** 서버 엣지에서만 붙는다 —
dataset·백테스트엔 미누출(시세·뉴스는 라이브 네트워크·키 의존이라 결정성 밖). 모델 해석
요약(summarize)·웹 맥락 카드 전용. 시세=live_quote 통합(네이버 KR/US 주식·지수·환율·VIX +
Binance 크립토, 공개 조회라 KIS 보안경계 밖)·뉴스=news_kr(단일종목 '왜 움직였나'). 실패는
graceful(context 없음) — 부가 정보가 정상 결과를 깨지 않는다(_attach_symbol_news 일반화).
"""
from __future__ import annotations

from quant_core.data.feeds import estimate_kr, news_kr

from .. import kis_master_cache, live_quote


def _target_symbols(result: dict) -> list[str]:
    """결과 형상별 '대표 종목' — 준실시간 시세 대상. 분류·라우팅(KR/US/크립토)은 live_quote.quotes_for가 담당."""
    shape = result.get("shape")
    syms: list[str] = []
    if shape == "describe_single":
        syms = [str(result.get("symbol") or "")]
    elif shape == "select":
        syms = [str(r.get("symbol") or "") for r in (result.get("results") or [])][:12]
    elif shape == "describe_portfolio":
        syms = [str(h.get("symbol") or "") for h in (result.get("holdings") or [])
                if isinstance(h, dict)]
    return [s for s in syms if s]


# forward-estimate 컬럼(FnGuide 추정 스냅샷) — 저장 컬럼이 아니라 estimate_block로 on-demand 브릿지.
_FORWARD_ESTIMATE_KEYS = {"forward_pe", "eps_forward", "rev_growth", "op_growth", "ni_growth",
                          "op_margin_forward", "roe_forward"}


def _fill_forward_estimates(result: dict) -> None:
    """compare 결과 행의 요청된 forward-estimate 지표(forward_pe 등)를 estimate_block로 채운다.

    엔진이 None으로 둔(저장 컬럼 아님) 값만 채우고 실측 값은 덮지 않는다. 종목별 on-demand
    (get=load-or-fetch·캐시)·실패/무추정은 None 유지(가짜 0 금지). 이 서버측 팬아웃이 '종목마다
    describe' LLM 왕복 폭발을 비교 1콜 안으로 흡수하는 핵심(FnGuide 컨센서스 KR).
    """
    rows = result.get("results") or []
    wanted = {c.get("key") for c in (result.get("columns") or [])} & _FORWARD_ESTIMATE_KEYS
    if not rows or not wanted:
        return
    for row in rows:
        metrics = row.get("metrics")
        if not isinstance(metrics, dict) or all(metrics.get(k) is not None for k in wanted):
            continue
        code = str(row.get("code") or row.get("symbol") or "")
        if not code:
            continue
        try:
            blk = estimate_kr.estimate_block(code, last_price=row.get("last_price"))
        except Exception:   # noqa: BLE001 — 종목 하나의 추정 실패가 표 전체를 깨지 않게
            continue
        fwd = (blk or {}).get("forward") or {}
        for k in wanted:
            if metrics.get(k) is None and fwd.get(k) is not None:
                metrics[k] = fwd[k]


def attach_context(result: dict) -> dict:
    """성공한 분석 결과에 준실시간 시세·뉴스 context를 best-effort로 붙인다(엔진 밖·골든 무누출).

    describe_single/select/portfolio만 대상(나머지 형상엔 무변경). 각 모달리티는 독립적으로
    실패해도 다른 것은 붙는다 — 부가정보 실패가 결과를 깨지 않게 graceful.
    """
    if not isinstance(result, dict) or not result.get("success", True):
        return result
    ctx: dict = {}

    syms = _target_symbols(result)
    if syms:
        try:
            quotes = live_quote.quotes_for(tuple(syms))   # KR·US·크립토 자동 분류 {sym:{price,chg,change}}
            if quotes:
                ctx["quotes"] = quotes
        except Exception:   # noqa: BLE001 — 부가 시세 실패가 결과를 깨지 않게
            pass

    # 비교표(select mode=compare)에 forward 추정(P1) — 사용자가 metrics에 forward_pe 등 forward-estimate
    # 컬럼을 요청하면 엔진은 저장 컬럼이 아니라 None을 낸다. FnGuide 추정 스냅샷(estimate_block)으로
    # 종목별 채운다(서버 엣지·on-demand·N종목을 이 1콜 안에서 — LLM 왕복 폭발을 서버로 흡수). best-effort.
    if result.get("mode") == "compare":
        _fill_forward_estimates(result)

    if result.get("shape") == "describe_single":
        try:
            name = kis_master_cache.get_name(str(result.get("symbol") or ""))
            news = news_kr.fetch_news(name, display=5) if name else []
            if news:
                ctx["news"] = news
        except Exception:   # noqa: BLE001 — 부가 뉴스 실패가 결과를 깨지 않게
            pass
        try:
            est = estimate_kr.estimate_block(
                str(result.get("symbol") or ""),
                last_price=(result.get("price") or {}).get("last"))
            if est:
                ctx["estimates"] = est   # forward 추정실적(FnGuide) — 뿌리① 단일 소스
        except Exception:   # noqa: BLE001 — 부가 추정실적 실패가 결과를 깨지 않게
            pass

    # breadth("시장이 왜")·news_research(시황·"왜 움직였나")엔 거시 시장 스냅샷(KR·US 지수+VIX)
    # 부착 — "오늘 장 어때"가 뉴스만으론 지수레벨을 못 받던 배선갭(IP1)을 닫는다. 뉴스가 비어도
    # 스냅샷으로 답하게. 개별종목 뉴스에도 시장 맥락=종목특유 vs 시장전체 구분에 도움.
    if result.get("shape") in ("breadth", "news_research"):
        try:
            mkt = live_quote.market_snapshot()
            if mkt:
                ctx["market"] = mkt
        except Exception:   # noqa: BLE001 — 부가 시장 스냅샷 실패가 결과를 깨지 않게
            pass

    if ctx:
        ctx["source"] = "준실시간(시세·뉴스=네이버) — 분석 수치는 종가 기준"
        result["context"] = ctx
    return result
