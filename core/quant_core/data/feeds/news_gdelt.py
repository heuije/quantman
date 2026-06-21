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
