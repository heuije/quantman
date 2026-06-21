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
