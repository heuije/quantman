"""챗 뉴스 리서치 — 모델이 판단한 쿼리·기간으로 on-demand 수집→본문→Haiku 다이제스트→증거 반환.

골든 무누출: 엔진/dataset 미진입(도구 결과로만). 결정적 수집(0토큰) + Haiku 다이제스트 1콜.
인용은 fetch한 실제 기사 메타에서 결정적 조립(LLM 미생성 → URL 환각 차단). best-effort.
"""
from __future__ import annotations

from quant_core.data.feeds import news_body, news_gdelt, news_kr

from ..config import settings

_MAX_QUERIES = 4


def _collect(queries: list[str], period: dict) -> list[dict]:
    """쿼리별 기사 목록 수집·정규화 [{title,url,date,source}]. recent→네이버, range→GDELT."""
    out: list[dict] = []
    for q in queries[:_MAX_QUERIES]:
        if period.get("kind") == "range":
            for a in news_gdelt.fetch_gdelt(q, period.get("start"), period.get("end")):
                out.append({"title": a["title"], "url": a["url"], "date": a["date"], "source": a["domain"]})
        else:
            for a in news_kr.fetch_news(q, display=10):
                out.append({"title": a["title"], "url": a["link"], "date": a.get("pub", ""),
                            "source": "naver", "desc": a.get("desc", "")})
    return out


def _dedup(arts: list[dict], cap: int) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for a in arts:
        if a["url"] and a["url"] not in seen:
            seen.add(a["url"])
            out.append(a)
        if len(out) >= cap:
            break
    return out


def _digest(arts: list[dict], queries: list[str]) -> str | None:
    """본문들 → Haiku 증거 다이제스트(기사 [n] 참조). 실패=None(상위에서 헤드라인 폴백)."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        blocks = []
        for i, a in enumerate(arts, 1):
            body = a.get("body") or a.get("desc") or ""
            blocks.append(f"[{i}] ({a['date']}·{a['source']}) {a['title']}\n{body}")
        system = ("너는 금융 뉴스 분석가다. 아래 기사들로 증거 다이제스트를 한국어로 작성: "
                  "①핵심 드라이버 3~5 ②타임라인(날짜→사건) ③기사별 1줄 요약[n] ④종합 내러티브 2~3문장. "
                  "기사를 [n]으로 참조하고 숫자·사실은 기사에서만. 추측 금지.")
        resp = client.messages.create(
            model=settings.NL_COMPILE_MODEL, max_tokens=900, system=system,
            thinking={"type": "disabled"},   # 다이제스트 요약엔 thinking 불필요(Sonnet5 기본ON→토큰·지연 낭비)
            messages=[{"role": "user", "content": f"쿼리: {', '.join(queries)}\n\n" + "\n\n".join(blocks)}])
        txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return txt or None
    except Exception:   # noqa: BLE001 — 다이제스트 실패 시 상위에서 헤드라인 폴백
        return None


def _headline_fallback(arts: list[dict]) -> str:
    lines = [f"- {a['title']} ({a['source']})" for a in arts[:8]]
    return "최근 헤드라인(본문 요약 실패 — 제목만):\n" + "\n".join(lines)


def research_news(queries: list[str], period: dict, max_articles: int = 8,
                  depth: str = "full") -> dict:
    """뉴스 리서치 도구 본체. {success, shape, digest, citations, period, n, sources}."""
    arts = _dedup(_collect(queries, period), max_articles)
    if not arts:
        return {"success": True, "shape": "news_research", "digest": "관련 뉴스를 찾지 못했습니다.",
                "citations": [], "period": period, "n": 0, "sources": []}
    if depth == "full":
        for a in arts:
            a["body"] = news_body.fetch_body(a["url"])
        digest = _digest(arts, queries) or _headline_fallback(arts)
    else:
        digest = _headline_fallback(arts)
    citations = [{"n": i, "title": a["title"], "url": a["url"], "date": a["date"], "source": a["source"]}
                 for i, a in enumerate(arts, 1)]
    return {"success": True, "shape": "news_research", "digest": digest, "citations": citations,
            "period": period, "n": len(arts), "sources": sorted({a["source"] for a in arts})}
