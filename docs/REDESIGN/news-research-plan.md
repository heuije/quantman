# 뉴스 리서치 (`research_news`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 오케스트레이터가 질문에 답하기 위해 *필요한 뉴스(엔티티+관련 매크로)·기간*을 판단해 on-demand로 수집·본문까지 읽고 답하는 능동 `research_news` 도구를 추가한다(가벼운 describe 헤드라인 사이드카는 유지).

**Architecture:** 결정적 수집(네이버 최근 / GDELT 과거 / 본문 추출, 0토큰) → 저비용 Haiku 다이제스트 1콜(증거 다이제스트·인용 결정적) → 오케스트레이터 답변. 뉴스는 엔진/dataset 미진입(골든 무누출), 새 도구 결과 형상 `news_research` → 웹 NewsDigest 렌더러(Phase3 레지스트리).

**Tech Stack:** Python(requests, anthropic SDK, 선택 trafilatura) · FastAPI · React/Recharts · pytest. 정본 = `C:\Users\USER\Desktop\창업\_wt-diag`(origin/main 체크아웃). 테스트는 `PYTHONPATH=core`(core) / `PYTHONPATH=../core`(server). spec = `docs/REDESIGN/news-research-redesign.md`.

---

## File Structure

- Create `core/quant_core/data/feeds/news_gdelt.py` — GDELT 과거/글로벌 기사 목록(쿼리+날짜범위·throttle). 0토큰.
- Create `core/quant_core/data/feeds/news_body.py` — 기사 URL→본문 추출(trafilatura+폴백·URL캐시). 0토큰.
- Create `server/app/chat/news_research.py` — 오케스트레이션(라우팅·dedup·상한·본문·Haiku 다이제스트·인용). Haiku 1콜.
- Modify `server/app/chat/tools.py` — `research_news` 도구 스키마 + `run_research_news` dispatch.
- Modify `core/quant_core/ir_engine/summarize.py` — `result_shape`/`summarize_result`에 `news_research`(digest 반환).
- Modify `core/quant_core/ir_engine/capabilities.py` — capability_spec에 research_news.
- Modify `server/app/chat/prompt.py` + `server/app/ir_compiler.py` — 도구 안내(프롬프트엔 라우팅, 컴파일러는 무관 — prompt.py만).
- Modify `web/src/components/ResultCharts.tsx` — `NewsDigest` 컴포넌트.
- Modify `web/src/components/ChatResultView.tsx` — `RENDERERS["news_research"]`.
- Modify `web/src/types.ts` — `NewsDigestResult` 타입.
- Create tests: `core/tests/test_news_gdelt.py`, `core/tests/test_news_body.py`, `server/tests/test_news_research.py`.

**빌드 순서**: Task1·2(결정적 fetcher, 독립) → Task3(오케스트레이션, 1·2 의존) → Task4(도구·요약 배선) → Task5(웹).

---

## Task 1: GDELT 과거/글로벌 fetcher (`news_gdelt.py`)

**Files:**
- Create: `core/quant_core/data/feeds/news_gdelt.py`
- Test: `core/tests/test_news_gdelt.py`

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_news_gdelt.py
"""GDELT 과거/글로벌 뉴스 fetcher — 파싱·날짜범위·rate-limit graceful (HTTP mock)."""
from __future__ import annotations
import sys
from pathlib import Path
_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
from quant_core.data.feeds import news_gdelt  # noqa: E402


class _Resp:
    def __init__(self, text): self.text = text
    def json(self):
        import json
        return json.loads(self.text)


def test_fetch_gdelt_parses_articles(monkeypatch):
    payload = ('{"articles":[{"title":"Samsung earnings beat","url":"http://x.com/a",'
               '"domain":"x.com","seendate":"20260510T120000Z","language":"Korean"}]}')
    monkeypatch.setattr(news_gdelt, "_throttle", lambda: None)
    monkeypatch.setattr(news_gdelt.requests, "get", lambda *a, **k: _Resp(payload))
    out = news_gdelt.fetch_gdelt("Samsung", start="2026-05-01", end="2026-05-31")
    assert out[0]["url"] == "http://x.com/a"
    assert out[0]["title"] == "Samsung earnings beat"
    assert out[0]["date"] == "20260510T120000Z"


def test_fetch_gdelt_graceful_on_ratelimit_text(monkeypatch):
    monkeypatch.setattr(news_gdelt, "_throttle", lambda: None)
    monkeypatch.setattr(news_gdelt.requests, "get",
                        lambda *a, **k: _Resp("Please limit requests to one every 5 seconds"))
    assert news_gdelt.fetch_gdelt("KOSPI") == []   # 비-JSON 안내문 → 빈 리스트


def test_fetch_gdelt_date_params(monkeypatch):
    seen = {}
    monkeypatch.setattr(news_gdelt, "_throttle", lambda: None)
    monkeypatch.setattr(news_gdelt.requests, "get",
                        lambda url, params=None, **k: seen.update(params) or _Resp('{"articles":[]}'))
    news_gdelt.fetch_gdelt("X", start="2026-05-01", end="2026-05-31")
    assert seen["startdatetime"] == "20260501000000" and seen["enddatetime"] == "20260531235959"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/USER/Desktop/창업/_wt-diag && PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/test_news_gdelt.py -q`
Expected: FAIL (`ModuleNotFoundError: news_gdelt`).

- [ ] **Step 3: Write the module**

```python
# core/quant_core/data/feeds/news_gdelt.py
"""과거/글로벌 뉴스 — GDELT doc API(무키·무료·2015+). 쿼리+날짜범위. rate-limit 1req/5s(throttle).

기사 메타(제목·URL·도메인·날짜·언어)만 반환 — 본문은 news_body가 URL에서 따로 추출.
비-JSON 응답(레이트리밋 안내문 등)·네트워크 실패는 빈 리스트(정직).
"""
from __future__ import annotations

import time

import requests

_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_UA = {"User-Agent": "Mozilla/5.0"}
_MIN_INTERVAL = 5.1            # GDELT 권고: 1 req / 5s
_last = [0.0]


def _throttle() -> None:
    dt = time.time() - _last[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last[0] = time.time()


def fetch_gdelt(query: str, start: str | None = None, end: str | None = None,
                max_records: int = 20) -> list[dict]:
    """GDELT 기사 목록. start/end='YYYY-MM-DD'(없으면 최근). [{title,url,domain,date,lang}]. 실패=[]."""
    if not query:
        return []
    params = {"query": query, "mode": "ArtList", "format": "json",
              "maxrecords": max(1, min(max_records, 75)), "sort": "datedesc"}
    if start:
        params["startdatetime"] = start.replace("-", "") + "000000"
    if end:
        params["enddatetime"] = end.replace("-", "") + "235959"
    try:
        _throttle()
        r = requests.get(_URL, params=params, headers=_UA, timeout=15)
        if not r.text.strip().startswith("{"):   # 레이트리밋 안내문 등 비-JSON
            return []
        arts = r.json().get("articles", [])
    except Exception:   # noqa: BLE001 — 외부 API 실패는 빈 결과(정직)
        return []
    return [{"title": a.get("title", ""), "url": a["url"], "domain": a.get("domain", ""),
             "date": a.get("seendate", ""), "lang": a.get("language", "")}
            for a in arts if a.get("url")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/test_news_gdelt.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add core/quant_core/data/feeds/news_gdelt.py core/tests/test_news_gdelt.py
git commit -m "feat(news): GDELT 과거/글로벌 뉴스 fetcher (throttle·날짜범위)"
```

---

## Task 2: 기사 본문 추출 (`news_body.py`)

**Files:**
- Create: `core/quant_core/data/feeds/news_body.py`
- Test: `core/tests/test_news_body.py`

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_news_body.py
"""기사 본문 추출 — <p> 폴백·길이상한·실패 graceful (HTTP mock; trafilatura 미설치 경로)."""
from __future__ import annotations
import sys
from pathlib import Path
_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
from quant_core.data.feeds import news_body  # noqa: E402


class _Resp:
    def __init__(self, text): self.text = text


def test_fetch_body_p_fallback(monkeypatch):
    html = "<html><body><p>첫 문단.</p><p>둘째 <b>문단</b>.</p></body></html>"
    monkeypatch.setattr(news_body.requests, "get", lambda *a, **k: _Resp(html))
    news_body.fetch_body.cache_clear()
    body = news_body.fetch_body("http://x.com/article-1")
    assert "첫 문단." in body and "둘째 문단." in body


def test_fetch_body_length_cap(monkeypatch):
    html = "<p>" + ("가" * 9000) + "</p>"
    monkeypatch.setattr(news_body.requests, "get", lambda *a, **k: _Resp(html))
    news_body.fetch_body.cache_clear()
    assert len(news_body.fetch_body("http://x.com/article-2")) <= news_body._MAX


def test_fetch_body_graceful(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(news_body.requests, "get", boom)
    news_body.fetch_body.cache_clear()
    assert news_body.fetch_body("http://x.com/article-3") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/test_news_body.py -q`
Expected: FAIL (`ModuleNotFoundError: news_body`).

- [ ] **Step 3: Write the module**

```python
# core/quant_core/data/feeds/news_body.py
"""기사 본문 추출 — URL→본문 텍스트. trafilatura 우선, 미설치 시 <p> 휴리스틱. URL 캐시.

best-effort: 네트워크/파싱 실패=빈 문자열. 길이 상한(_MAX)으로 다이제스트 입력 토큰 가드.
"""
from __future__ import annotations

import re
from functools import lru_cache

import requests

_UA = {"User-Agent": "Mozilla/5.0"}
_TAG = re.compile(r"<[^>]+>")
_MAX = 4000   # 본문 길이 상한(자)


def _extract(html: str) -> str:
    try:
        import trafilatura
        t = trafilatura.extract(html, include_comments=False, include_tables=False)
        if t:
            return re.sub(r"\s+", " ", t).strip()
    except Exception:   # noqa: BLE001 — trafilatura 미설치/실패 시 폴백
        pass
    ps = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S)   # 폴백: <p> 텍스트 합산
    return re.sub(r"\s+", " ", _TAG.sub("", " ".join(ps))).strip()


@lru_cache(maxsize=256)
def fetch_body(url: str) -> str:
    """기사 URL→본문(≤_MAX자). 실패=''."""
    try:
        html = requests.get(url, headers=_UA, timeout=8).text
    except Exception:   # noqa: BLE001
        return ""
    return _extract(html)[:_MAX]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/test_news_body.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add core/quant_core/data/feeds/news_body.py core/tests/test_news_body.py
git commit -m "feat(news): 기사 본문 추출(trafilatura+<p> 폴백·캐시·길이상한)"
```

---

## Task 3: 리서치 오케스트레이션 (`news_research.py`)

**Files:**
- Create: `server/app/chat/news_research.py`
- Test: `server/tests/test_news_research.py`

라우팅(recent→네이버 `news_kr`, range/과거→`news_gdelt`)·URL dedup·상한·본문 수집·Haiku 다이제스트·**인용 결정적**. Haiku는 `anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)` + `client.messages.create(model=settings.NL_COMPILE_MODEL,…)`(ir_compiler.py:445-458 패턴).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_news_research.py
"""research_news 오케스트레이션 — 라우팅·dedup·상한·인용 결정적·graceful (fetcher·Haiku mock)."""
from __future__ import annotations
from app.chat import news_research as nr


def test_recent_routes_to_naver(monkeypatch):
    monkeypatch.setattr(nr.news_kr, "fetch_news",
                        lambda q, display=10: [{"title": "T", "link": "http://a", "desc": "d", "pub": "p"}])
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: "다이제스트[1]")
    out = nr.research_news(["삼성전자"], {"kind": "recent", "days": 7}, depth="full")
    assert out["shape"] == "news_research"
    assert out["citations"][0]["url"] == "http://a"
    assert out["digest"] == "다이제스트[1]"


def test_range_routes_to_gdelt_and_dedups(monkeypatch):
    arts = [{"title": "T1", "url": "http://a", "domain": "a", "date": "20260510", "lang": "ko"},
            {"title": "T2", "url": "http://a", "domain": "a", "date": "20260510", "lang": "ko"}]  # 중복 url
    monkeypatch.setattr(nr.news_gdelt, "fetch_gdelt", lambda q, start=None, end=None, max_records=20: arts)
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: "D")
    out = nr.research_news(["X"], {"kind": "range", "start": "2026-05-01", "end": "2026-05-31"})
    assert len(out["citations"]) == 1            # url 중복 제거


def test_caps_queries_and_articles(monkeypatch):
    monkeypatch.setattr(nr.news_gdelt, "fetch_gdelt",
                        lambda q, start=None, end=None, max_records=20:
                        [{"title": q, "url": f"http://{q}", "domain": "d", "date": "1", "lang": "ko"}])
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: "D")
    out = nr.research_news([f"q{i}" for i in range(10)],   # 10개 쿼리 → ≤4만 사용
                           {"kind": "range", "start": "2026-05-01", "end": "2026-05-31"}, max_articles=3)
    assert len(out["citations"]) <= 3


def test_digest_failure_falls_back_to_headlines(monkeypatch):
    monkeypatch.setattr(nr.news_kr, "fetch_news",
                        lambda q, display=10: [{"title": "헤드라인", "link": "http://a", "desc": "스니펫", "pub": "p"}])
    monkeypatch.setattr(nr.news_body, "fetch_body", lambda url: "본문")
    monkeypatch.setattr(nr, "_digest", lambda arts, queries: None)   # 다이제스트 실패
    out = nr.research_news(["삼성전자"], {"kind": "recent", "days": 7})
    assert "헤드라인" in out["digest"]           # 폴백: 헤드라인+스니펫


def test_no_articles_returns_empty_note(monkeypatch):
    monkeypatch.setattr(nr.news_kr, "fetch_news", lambda q, display=10: [])
    out = nr.research_news(["없는종목"], {"kind": "recent", "days": 7})
    assert out["success"] is True and out["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && PYTHONUTF8=1 PYTHONPATH=../core python -m pytest tests/test_news_research.py -q`
Expected: FAIL (`ModuleNotFoundError: news_research`).

- [ ] **Step 3: Write the module**

```python
# server/app/chat/news_research.py
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
    seen, out = set(), []
    for a in arts:
        if a["url"] and a["url"] not in seen:
            seen.add(a["url"]); out.append(a)
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
            model=settings.NL_COMPILE_MODEL, max_tokens=900,
            system=system,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && PYTHONUTF8=1 PYTHONPATH=../core python -m pytest tests/test_news_research.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/news_research.py server/tests/test_news_research.py
git commit -m "feat(news): research_news 오케스트레이션(라우팅·dedup·Haiku 다이제스트·인용 결정적)"
```

---

## Task 4: 도구 등록 + 요약 + capability + 프롬프트

**Files:**
- Modify: `server/app/chat/tools.py` (TOOLS 스키마 리스트 + run_tool dispatch — `run_inspect` 패턴 참조)
- Modify: `core/quant_core/ir_engine/summarize.py` (`result_shape`/`summarize_result`)
- Modify: `core/quant_core/ir_engine/capabilities.py`
- Modify: `server/app/chat/prompt.py`
- Test: `server/tests/test_chat_api.py`(도구 등장) 또는 신규 어서션

- [ ] **Step 1: summarize에 news_research 추가 (test 먼저)**

`core/tests/test_summarize.py`에 추가:
```python
def test_news_research_summary_returns_digest():
    res = {"success": True, "shape": "news_research", "digest": "핵심 드라이버[1]…", "citations": []}
    from quant_core.ir_engine import summarize_result, result_shape
    assert result_shape(res) == "news_research"
    assert summarize_result(res) == "핵심 드라이버[1]…"
```
Run(FAIL): `PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/test_summarize.py::test_news_research_summary_returns_digest -q`

- [ ] **Step 2: summarize.py 구현**

`result_shape` 의 select 분기 옆에 추가(`summarize.py`, stamped shape는 이미 우선 반환되므로 query 분기에 합류):
```python
    if result.get("query") == "prescribe":
        return "prescribe"
    if result.get("shape") == "news_research" or result.get("query") == "research_news":
        return "news_research"
```
`summarize_result`의 shape 분기에 추가(맨 앞 성공 체크 뒤):
```python
    if shape == "news_research":
        return str(result.get("digest") or "[뉴스 리서치 결과 없음]")
```
Run(PASS): 위 명령 재실행.

- [ ] **Step 3: tools.py — research_news 도구 스키마 + dispatch**

`tools.py`의 TOOLS(Anthropic 도구 스키마 리스트)에 추가:
```python
    {
        "name": "research_news",
        "description": ("뉴스로 답해야 하는 질문(최근 이슈·왜 움직였나·특정 시점 사건·시장/매크로 동향)에 "
                        "쓴다. queries에 엔티티+관련 매크로/섹터 키워드를, period에 기간을 네가 판단해 넣으면 "
                        "최근=네이버·과거=GDELT로 수집해 본문까지 읽고 증거 다이제스트(인용 포함)를 돌려준다."),
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"},
                            "description": "엔티티 + 관련 매크로/섹터 키워드(2~4). 예: ['삼성전자','반도체 업황','D램 가격']"},
                "period": {"type": "object", "description":
                           "{kind:'recent',days:N} 또는 {kind:'range',start:'YYYY-MM-DD',end:'YYYY-MM-DD'}"},
                "max_articles": {"type": "integer"},
                "depth": {"type": "string", "enum": ["headlines", "full"]},
            },
            "required": ["queries", "period"],
        },
    },
```
`run_tool`의 inspect 분기 옆에 dispatch 추가:
```python
    if tool_name == "research_news":
        from .news_research import research_news
        ti = tool_input
        return research_news(ti.get("queries") or [], ti.get("period") or {"kind": "recent", "days": 7},
                             int(ti.get("max_articles") or 8), str(ti.get("depth") or "full"))
```

- [ ] **Step 4: capability_spec — research_news 추가**

`capabilities.py`의 query 옵션 리스트(prescribe/breadth 다음)에 추가:
```python
            {"value": "research_news",
             "does": "뉴스 리서치 — 엔티티+매크로 키워드·기간을 받아 최근(네이버)/과거(GDELT) 수집·본문·증거 다이제스트",
             "use_for": "'최근 이슈'·'왜 올랐나/빠졌나'·'그날 왜'·시장 동향. queries(자유키워드)·period(recent/range)."},
```
> ⚠ capability coverage 테스트는 *엔진 query Literal*만 검사한다. research_news는 **챗 도구**(엔진 verb 아님)이므로 StrategyIR.query Literal에는 넣지 않는다 — capability_spec엔 참고용으로만 추가(coverage 게이트 무관). 테스트 깨지면 capabilities의 별도 섹션(chat_tools)으로 옮긴다.

- [ ] **Step 5: prompt.py — 도구 라우팅 안내**

`prompt.py`의 `<tools_guidance>` describe 줄 아래에 추가:
```
- research_news: 뉴스로 답할 질문(최근 이슈·왜 움직였나·특정 시점·시장 동향)에. queries(엔티티+관련 매크로/섹터)·period(recent days N / range start~end)를 네가 판단. 단순 "○○ 어때"는 describe(헤드라인 자동)면 충분 — 심층·기간·매크로·본문이 필요할 때 research_news.
```
"미수급" 거짓고지 줄에서 뉴스 제거(이제 research_news로 본문까지 가능):
```
데이터 미수급은 지어내지 말고 솔직히 한계를 밝힌다(뉴스는 research_news로 수집 가능).
```

- [ ] **Step 6: Run + Commit**

Run: `cd server && PYTHONUTF8=1 PYTHONPATH=../core python -m pytest -q` (전체 server green) + `PYTHONPATH=core python -m pytest core/tests/test_summarize.py core/tests/test_capability_coverage.py -q`
```bash
git add server/app/chat/tools.py server/app/chat/prompt.py core/quant_core/ir_engine/summarize.py core/quant_core/ir_engine/capabilities.py core/tests/test_summarize.py
git commit -m "feat(news): research_news 도구 등록 + 요약(digest)·capability·프롬프트 배선"
```

---

## Task 5: 웹 NewsDigest 렌더러

**Files:**
- Modify: `web/src/types.ts` (`NewsDigestResult`)
- Modify: `web/src/components/ResultCharts.tsx` (`NewsDigest` 컴포넌트)
- Modify: `web/src/components/ChatResultView.tsx` (`RENDERERS["news_research"]`)

- [ ] **Step 1: types.ts — 타입 추가** (PrescribeResult 옆)

```typescript
export interface NewsDigestResult {
  digest?: string;
  citations?: { n: number; title: string; url: string; date: string; source: string }[];
  period?: { kind: string; days?: number; start?: string; end?: string };
  n?: number;
  sources?: string[];
}
```

- [ ] **Step 2: ResultCharts.tsx — NewsDigest 컴포넌트** (파일 끝에 추가)

```tsx
export function NewsDigest({ r }: { r: NewsDigestResult }) {
  const cites = r.citations ?? [];
  return (
    <Box title="뉴스 리서치" sub={`${r.n ?? 0}건 · ${(r.sources ?? []).join(", ")}`}>
      <div style={{ whiteSpace: "pre-wrap", fontSize: 13, color: C.text, lineHeight: 1.55 }}>
        {r.digest ?? "—"}
      </div>
      {cites.length > 0 ? (
        <div style={{ marginTop: 8, borderTop: `1px solid ${C.grid}`, paddingTop: 6 }}>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 4 }}>출처</div>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 11 }}>
            {cites.map((c) => (
              <li key={c.n} style={{ margin: "2px 0" }}>
                <a href={c.url} target="_blank" rel="noopener noreferrer"
                   style={{ color: C.accent, textDecoration: "none" }}>{c.title}</a>
                <span style={{ color: C.muted }}> · {c.source} · {c.date?.slice(0, 8)}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </Box>
  );
}
```
import 타입에 `NewsDigestResult` 추가(ResultCharts.tsx 상단 `from "../types"`).

- [ ] **Step 3: ChatResultView.tsx — RENDERERS 등록 + import**

import에 `NewsDigest`(ResultCharts) + `NewsDigestResult`(types) 추가. RENDERERS에:
```tsx
  news_research: (result) => (
    <div className="chat-result"><NewsDigest r={result as unknown as NewsDigestResult} /></div>
  ),
```
(EXCEL_SHAPES엔 미포함 — 엑셀 비대상이라 자동으로 버튼 숨김. deriveShape 폴백엔 불필요 — 엔진이 shape 스탬프.)

- [ ] **Step 4: Run build + Commit**

Run: `cd web && npm run build && npx eslint src/components/ChatResultView.tsx src/types.ts`
Expected: built OK, my files lint 0.
```bash
git add web/src/types.ts web/src/components/ResultCharts.tsx web/src/components/ChatResultView.tsx
git commit -m "feat(news): 웹 NewsDigest 렌더러(내러티브+인용 링크)"
```

---

## Task 6: 최종 검증

- [ ] 전체 core: `PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/ -q` → 골든 byte-identical 포함 green.
- [ ] 전체 server: `cd server && PYTHONUTF8=1 PYTHONPATH=../core python -m pytest -q` → green.
- [ ] 하니스: `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=core python scripts/analysis_diag.py` → 18/18(뉴스는 엔진 미진입 — 무영향 확인).
- [ ] 웹 빌드 + ruff(신규 0).
- [ ] (선택·라이브) 키 있는 환경에서 research_news 1회 수동 호출 — 인용 결정적·digest 생성 확인.
- [ ] 설계서 §완료 반영 + 메모리·brief 갱신.

> 라이브 E2E(로그인 브라우저서 "엔비디아 지난달 왜 급락" → research_news → 다이제스트+인용 렌더)는 자격증명 경계라 사용자측.

---

## 의존성·환경 노트

- **trafilatura(선택)**: `pip install trafilatura` + `requirements.txt`(server) 추가. 미설치여도 `news_body`는 `<p>` 폴백으로 동작(테스트는 폴백 경로). 설치 시 본문 품질↑.
- **NAVER 키**: 최근 뉴스(네이버)는 서버 env `NAVER_CLIENT_ID/SECRET` 필요. 미설정 시 recent는 빈 결과 → 과거(GDELT)만 동작.
- **GDELT**: 무키. 1req/5s throttle(프로세스 전역) — 동시 호출 많으면 직렬화 지연. queries 상한 4로 가드.
- **Haiku 티어**: `settings.NL_COMPILE_MODEL` 재사용(별도 env 불필요).
