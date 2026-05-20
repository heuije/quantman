"""NAVER 금융 종목 펀더멘털 캐시 — KRX 정보데이터 사이트 403 차단 우회.

소스: NAVER mobile JSON API — `m.stock.naver.com/api/stock/{code}/integration`
종목당 1 GET, JSON 한 번에 PER/PBR/EPS/BPS/DPS/배당수익률/외국인 소진율 다 제공.

매일 17:30 KST KRX 캐시 직후 cron 실행. 약 2,700 종목 × 0.3초 = 15분.
ThreadPoolExecutor로 동시 5개 (NAVER 차단 회피 + 시간 단축).

V1 한계 (Phase 17)의 가장 큰 누락이었던 펀더멘털 데이터를 채우는 모듈.

응답에서 추출되는 필드 (NAVER `totalInfos`의 key — totalInfos는 dict가 아니라 list of {code, key, value}):
  per, eps, pbr, bps, dividendYieldRatio, dividend, foreignRate

NAVER 응답 형식:
  totalInfos: [{ code: "per", key: "PER", value: "42.05배" }, ...]
값은 한국어 문자열 ("42.05배", "0.60%", "1,668원") — 우리가 숫자로 파싱.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from . import krx_cache

log = logging.getLogger("app.naver_fundamentals")

# NAVER mobile API endpoint
_API_URL = "https://m.stock.naver.com/api/stock/{code}/integration"
_UA = "Mozilla/5.0 (Linux; Android 10) quant-platform/0.6"

# 우리가 뽑을 NAVER totalInfos.code → daily_metrics 필드 매핑
_FIELD_MAP = {
    "per":                "per",
    "pbr":                "pbr",
    "eps":                "eps",
    "bps":                "bps",
    "dividend":           "dps",
    "dividendYieldRatio": "dividend_yield",
    "foreignRate":        "foreign_rate",
    # 52주 고저는 KRX 캐시에 high/low 있지만 NAVER는 52주 기준 — 별도 필드
    "highPriceOf52Weeks": "high_52w",
    "lowPriceOf52Weeks":  "low_52w",
}

_lock = threading.Lock()
_state: dict = {
    "fetched_at": None,
    "n_total": 0,
    "n_ok": 0,
    "n_fail": 0,
    "last_error": None,
}


def _parse_num(s) -> float | None:
    """NAVER 문자열 → float. '42.05배' → 42.05, '1,668원' → 1668, '0.60%' → 0.60."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s in ("-", "N/A"):
        return None
    m = re.search(r"-?[\d,]+\.?\d*", s.replace(",", ""))
    if not m:
        return None
    try:
        v = float(m.group())
        return v if v == v else None    # NaN 방어
    except ValueError:
        return None


def _fetch_one(symbol: str, timeout: int = 8) -> dict | None:
    """단일 종목 펀더멘털. NAVER API 응답 → 우리 필드 dict."""
    url = _API_URL.format(code=symbol)
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        log.debug("NAVER fetch 실패 [%s]: %s", symbol, e)
        return None

    out: dict = {}
    for it in data.get("totalInfos", []) or []:
        code = it.get("code")
        if code not in _FIELD_MAP:
            continue
        val = _parse_num(it.get("value"))
        if val is None:
            continue
        # PER/PBR 음수·0 → None (적자/자본잠식)
        if code in ("per", "pbr") and val <= 0:
            continue
        out[_FIELD_MAP[code]] = val
    return out if out else None


def refresh(symbols: list[str] | None = None, *,
            workers: int = 5, max_symbols: int | None = None) -> dict:
    """전 종목 NAVER 펀더멘털 fetch.

    Args:
        symbols: 대상 종목 리스트. None이면 krx_cache의 모든 KRX 종목.
        workers: 동시 호출 worker 수 (NAVER 차단 회피 위해 5 권장).
        max_symbols: 디버그용 상한 (None=무제한).
    """
    if symbols is None:
        metrics_all = krx_cache.get_all_metrics()
        symbols = list(metrics_all.keys())
    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    log.info("NAVER 펀더멘털 fetch 시작: %d종목, workers=%d", len(symbols), workers)
    started = datetime.now(timezone.utc)
    n_ok = 0
    n_fail = 0
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                d = fut.result()
            except Exception:
                d = None
            if d:
                results[sym] = d
                n_ok += 1
            else:
                n_fail += 1

    # krx_cache의 메모리 metrics에 펀더멘털 필드 merge
    metrics_all = krx_cache.get_all_metrics()
    merged = 0
    for sym, fund in results.items():
        if sym in metrics_all:
            metrics_all[sym].update(fund)
            merged += 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    with _lock:
        _state["fetched_at"] = datetime.now(timezone.utc)
        _state["n_total"] = len(symbols)
        _state["n_ok"] = n_ok
        _state["n_fail"] = n_fail
        _state["last_error"] = None

    log.info("NAVER 펀더멘털 갱신 완료: ok=%d, fail=%d, 메모리 merge=%d, 소요=%.1fs",
             n_ok, n_fail, merged, elapsed)

    return {
        "ok": True,
        "n_total": len(symbols),
        "n_ok": n_ok,
        "n_fail": n_fail,
        "merged": merged,
        "elapsed_sec": round(elapsed, 1),
        "fetched_at": _state["fetched_at"].isoformat(),
    }


def get_status() -> dict:
    with _lock:
        return {
            "n_total": _state["n_total"],
            "n_ok": _state["n_ok"],
            "n_fail": _state["n_fail"],
            "fetched_at": _state["fetched_at"].isoformat()
                           if _state["fetched_at"] else None,
            "last_error": _state["last_error"],
        }
