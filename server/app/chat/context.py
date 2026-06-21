"""챗 결과 context 사이드카 (P4) — 결정적 엔진 밖에서 결과를 준실시간 시세·뉴스로 enrich.

골든 불변식 보호: 엔진(strategy_from_spec)이 결과를 낸 **뒤** 서버 엣지에서만 붙는다 —
dataset·백테스트엔 미누출(시세·뉴스는 라이브 네트워크·키 의존이라 결정성 밖). 모델 해석
요약(summarize)·웹 맥락 카드 전용. 시세=네이버 준실시간(_naver_quotes 재사용, 공개 시세
조회라 KIS 보안경계 밖)·뉴스=news_kr(단일종목 '왜 움직였나'). 실패는 graceful(context
없음) — 부가 정보가 정상 결과를 깨지 않는다(_attach_symbol_news 패턴의 다대상·다모달 일반화).
"""
from __future__ import annotations

import re

from quant_core.data.feeds import news_kr

from .. import kis_master_cache
from ..industry import _bucket, _naver_quotes

_KR_CODE = re.compile(r"^\d{6}$")


def _target_symbols(result: dict) -> list[str]:
    """결과 형상별 '대표 종목' — 준실시간 시세를 붙일 대상(KR 6자리 코드만; 네이버 국내 시세)."""
    shape = result.get("shape")
    syms: list[str] = []
    if shape == "describe_single":
        syms = [str(result.get("symbol") or "")]
    elif shape == "select":
        syms = [str(r.get("symbol") or "") for r in (result.get("results") or [])][:12]
    elif shape == "describe_portfolio":
        syms = [str(h.get("symbol") or "") for h in (result.get("holdings") or [])
                if isinstance(h, dict)]
    return [s for s in syms if _KR_CODE.match(s)]


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
            quotes = _naver_quotes(tuple(syms), _bucket())   # {code: {close, chg}} 준실시간(~90초)
            if quotes:
                ctx["quotes"] = quotes
        except Exception:   # noqa: BLE001 — 부가 시세 실패가 결과를 깨지 않게
            pass

    if result.get("shape") == "describe_single":
        try:
            name = kis_master_cache.get_name(str(result.get("symbol") or ""))
            news = news_kr.fetch_news(name, display=5) if name else []
            if news:
                ctx["news"] = news
        except Exception:   # noqa: BLE001 — 부가 뉴스 실패가 결과를 깨지 않게
            pass

    if ctx:
        ctx["source"] = "준실시간(시세·뉴스=네이버) — 분석 수치는 종가 기준"
        result["context"] = ctx
    return result
