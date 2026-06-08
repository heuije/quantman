"""KR 종목 뉴스 — 네이버 검색 API(news.json). 360 리포트 "왜 올랐나" facet용 on-demand 헤드라인.

키: NAVER_CLIENT_ID·NAVER_CLIENT_SECRET (X-Naver-Client-* 헤더). 미설정이면 빈 리스트(정직 — 가짜 채움 0).
사용처: run_describe_report가 종목명으로 최근 뉴스를 붙인다. 종목당 1콜(on-demand, 벌크 아님 — 뉴스는
전 종목 사전수집 비현실적). 응답의 <b>·&quot; 등 HTML 태그·엔티티는 제거한다.
"""

from __future__ import annotations

import os
import re

_BASE = "https://openapi.naver.com/v1/search/news.json"
_TAG = re.compile(r"<[^>]+>")
_ENTITIES = {"&quot;": '"', "&amp;": "&", "&lt;": "<", "&gt;": ">", "&#39;": "'", "&apos;": "'"}


def _clean(s: str) -> str:
    """HTML 태그 제거 + 기본 엔티티 디코드(네이버 응답은 <b>·&quot; 포함)."""
    s = _TAG.sub("", s or "")
    for k, v in _ENTITIES.items():
        s = s.replace(k, v)
    return s.strip()


def fetch_news(query: str, display: int = 10) -> list[dict]:
    """종목명 등으로 최근 뉴스 헤드라인. [{title, link, desc, pub}, ...]. 키 미설정·실패 시 []."""
    cid = os.environ.get("NAVER_CLIENT_ID")
    sec = os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and sec) or not query:
        return []
    import requests
    try:
        r = requests.get(
            _BASE,
            params={"query": query, "display": max(1, min(display, 100)), "sort": "date"},
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception:                                # noqa: BLE001 — 외부 API 실패는 빈 결과(정직)
        return []
    return [{"title": _clean(it.get("title", "")),
             "link": it.get("originallink") or it.get("link", ""),
             "desc": _clean(it.get("description", "")),
             "pub": it.get("pubDate")}
            for it in items]
