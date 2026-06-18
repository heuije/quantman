"""한경컨센서스 financial-summary 스크래퍼 — 5개년 기업가치·재무비율·재무제표(키 불필요).

markets.hankyung.com/stock/{code}/financial-summary 페이지를 read_html로 파싱한다.
표는 라벨표(홀수번째 제외)·값표가 번갈아 나와 (라벨[i] ↔ 값표 row[i]) 로 묶는다.
제공: 기업가치(EPS·BPS·PER·PEG·PBR·EV/EBITDA·BETA) + 수익성/성장성/안정성(ROE·부채비율 등) +
재무제표(매출액·영업이익·당기순이익·자산·부채·자본·현금흐름 등) × 최근 5개 회계연도.

밸류에이션을 한경 기준으로 통일하기 위한 historical 소스. 예상치(E)는 없으므로 forward는 FnGuide.
historical은 안 변하니 디스크 저장 후 즉시 서빙(분기/마감 cron 갱신).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from io import StringIO

import pandas as pd
import requests

_log = logging.getLogger("app.hankyung")
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hankyung")
_FRESH_DAYS = 80
_UA = {"User-Agent": "Mozilla/5.0"}
_URL = "https://markets.hankyung.com/stock/{code}/financial-summary"
_YEAR_RE = re.compile(r"^(\d{4})\.\d{2}\.\d{2}$")


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "N/A", "nan", "None"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    # 한경은 결측을 0.0으로 표기 — 비율류(PER/PBR/EV)의 0은 의미상 결측이나 여기선 원값 보존,
    # 소비측(통일 로직)에서 0→None 처리 + FnGuide 보완.
    return f


def _fetch(code: str) -> dict:
    r = requests.get(_URL.format(code=code), headers=_UA, timeout=20)
    if r.encoding in (None, "ISO-8859-1"):
        r.encoding = r.apparent_encoding
    tabs = pd.read_html(StringIO(r.text))
    # 연도 컬럼(yyyy) — 첫 값표에서 추출
    years: list[str] = []
    for t in tabs:
        ys = [m.group(1) for c in t.columns if (m := _YEAR_RE.match(str(c)))]
        if ys:
            years = ys
            break
    if not years:
        return {"fetched": date.today().isoformat(), "years": [], "rows": {}}
    # 라벨표(1열) ↔ 값표(연도열) 번갈아 → 라벨: [연도별 값]
    rows: dict[str, list] = {}
    i = 0
    while i < len(tabs) - 1:
        lab_t, val_t = tabs[i], tabs[i + 1]
        ycols = [c for c in val_t.columns if _YEAR_RE.match(str(c))]
        if lab_t.shape[1] == 1 and ycols:
            labels = lab_t.iloc[:, 0].tolist()
            for j, lab in enumerate(labels):
                name = str(lab).strip()
                if not name or j >= val_t.shape[0]:
                    continue
                rows[name] = [_num(val_t.iloc[j][c]) for c in ycols]
            i += 2
        else:
            i += 1
    return {"fetched": date.today().isoformat(), "years": years, "rows": rows}


def _path(code: str) -> str:
    return os.path.join(_DIR, f"{code}.json")


def _save(code: str, data: dict) -> None:
    os.makedirs(_DIR, exist_ok=True)
    tmp = _path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _path(code))


def refresh(code: str) -> dict:
    data = _fetch(str(code).strip())
    try:
        _save(str(code).strip(), data)
    except Exception:
        _log.exception("한경 저장 실패 %s", code)
    return data


def get(code: str) -> dict:
    """한경 financial-summary 저장본(신선하면 즉시, 아니면 1회 조회·저장)."""
    code = str(code).strip()
    path = _path(code)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            fetched = cached.get("fetched", "")
            if fetched and (date.today() - date.fromisoformat(fetched)).days < _FRESH_DAYS:
                return cached
    except Exception:
        pass
    try:
        return refresh(code)
    except Exception as e:
        _log.warning("한경 조회 실패 %s: %s", code, e)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"fetched": "", "years": [], "rows": {}}


def refresh_all(codes: list[str]) -> int:
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for d in ex.map(lambda c: _safe(c), codes):
            if d:
                ok += 1
    _log.info("한경 %d/%d 갱신", ok, len(codes))
    return ok


def _safe(code: str):
    try:
        d = refresh(code)
        return d if d.get("rows") else None
    except Exception:
        return None
