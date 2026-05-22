"""미국(S&P500) 스크리너 metrics 캐시 — 국내 krx_cache와 동일 스키마.

스테이지1 미국 자동선택용. 단일 소스(FDR)인 국내와 달리 미국은 조립한다:
  - close/volume/pct_change_1d/trade_value : 서버 dataset OHLCV (yfinance)
  - market_cap                             : yfinance fast_info (주1회, 디스크 캐시)
  - name/market(NAS/NYS/AMS)/kind          : KIS 마스터(kis_master_cache)
  - per/pbr                                : 스테이지1 미지원(None)
  - is_pref/is_managed/is_halt             : 미국 미적용(False)

심볼 키는 dataset/ledger와 동일한 **대시 표준형(BRK-B)**으로 통일한다(reconcile 정합).
통화는 USD — 스크리너 spec/preset의 시총·거래대금 임계는 USD 기준으로 둔다.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from quant_core import data_fetcher

from . import data_cache, kis_master_cache

log = logging.getLogger("app.us_metrics")

_CAP_PATH = data_fetcher.DATA_DIR / "us_market_caps.json"
_lock = threading.Lock()
_state: dict = {"metrics": {}, "fetched_at": None, "n": 0, "n_capped": 0}


# ── 시가총액 (fast_info, 주1회) ───────────────────────────────────────────────

def refresh_market_caps(timeout_each: float = 0.0, limit: int | None = None) -> dict:
    """S&P500 fast_info marketCap(USD) 수집 → 디스크 캐시. 주1회 호출 권장.

    fast_info는 분기재무 호출보다 가벼우나 종목당 1콜이라 ~500콜. rate 완화를 위해
    종목 간 짧은 sleep. 실패 종목은 직전 캐시 값 유지(부분 성공 허용).
    limit=N이면 앞 N개만(개발/검증용).
    """
    import time
    import yfinance as yf

    caps = _load_caps()
    codes = data_fetcher.sp500_yf_codes()
    if limit is not None:
        codes = codes[:limit]
    n_ok = 0
    for code in codes:
        try:
            mc = yf.Ticker(code).fast_info.market_cap
            if mc and float(mc) > 0:
                caps[code] = float(mc)
                n_ok += 1
        except Exception as e:
            log.debug("market_cap fetch 실패 [%s]: %s", code, e)
        if timeout_each:
            time.sleep(timeout_each)
    try:
        _CAP_PATH.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("us_market_caps 저장 실패: %s", e)
    log.info("미국 시가총액 갱신 — %d종목", n_ok)
    return {"ok": True, "n": n_ok}


def _load_caps() -> dict:
    if _CAP_PATH.exists():
        try:
            return json.loads(_CAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# ── metrics 조립 ──────────────────────────────────────────────────────────────

def build_metrics(dataset: dict | None = None,
                  caps: dict | None = None) -> dict[str, dict]:
    """dataset OHLCV + 시총 + 마스터 메타 → {code: metric} (krx 스키마 호환).

    dataset/caps 미지정 시 서버 캐시에서 로드(테스트 주입용 인자).
    """
    ds = dataset if dataset is not None else data_cache.get_dataset()
    caps = caps if caps is not None else _load_caps()

    master = {m["symbol"]: m for m in kis_master_cache.get_master_list()}

    def _meta(code: str) -> dict:
        # dataset 코드(대시) → KIS 마스터(클래스주는 슬래시) 보정 조회
        return master.get(code) or master.get(code.replace("-", "/")) or {}

    metrics: dict[str, dict] = {}
    for c in data_fetcher.load_sp500():
        code = (c.get("symbol") or "").replace(".", "-")
        if not code:
            continue
        df = ds.get(code)
        if df is None or len(df) == 0 or "Close" not in df.columns:
            continue                       # 아직 OHLCV 미수집 → 스크리너 제외
        meta = _meta(code)
        if not meta or meta.get("market") not in ("NAS", "NYS", "AMS"):
            # KIS 마스터 미로드(콜드스타트)·미수록 종목은 제외 — 거래소를 추측해
            # 잘못 분류(예: NYSE를 NAS로)하면 market 필터가 빗나가므로 skip.
            continue
        close = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2]) if len(df) >= 2 else close
        vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
        metrics[code] = {
            "symbol": code,
            "name": meta.get("name") or c.get("name", ""),
            "market": meta["market"],                # NAS / NYS / AMS (마스터 확정)
            "close": close,
            "pct_change_1d": (close / prev - 1) * 100 if prev > 0 else 0.0,
            "market_cap": caps.get(code),            # USD (없으면 None)
            "trade_value": close * vol,              # USD
            "volume": vol,
            "kind": meta.get("kind", "stock"),
            "currency": "USD",
            "is_pref": False, "is_managed": False, "is_halt": False,
            "per": None, "pbr": None,
        }
    return metrics


def refresh() -> dict:
    """us_metrics 재빌드 후 캐시 교체."""
    caps = _load_caps()
    metrics = build_metrics(caps=caps)
    n_capped = sum(1 for m in metrics.values() if m.get("market_cap"))
    with _lock:
        _state["metrics"] = metrics
        _state["n"] = len(metrics)
        _state["n_capped"] = n_capped
        _state["fetched_at"] = datetime.now(timezone.utc)
    log.info("미국 metrics 갱신 — %d종목 (시총 %d)", len(metrics), n_capped)
    return {"ok": True, "n": len(metrics), "n_capped": n_capped}


def get_all_metrics() -> dict[str, dict]:
    with _lock:
        return dict(_state["metrics"])


def get_metric(symbol: str) -> dict | None:
    with _lock:
        return _state["metrics"].get(symbol.replace(".", "-"))


def get_status() -> dict:
    with _lock:
        return {
            "n_total": _state["n"],
            "n_with_market_cap": _state["n_capped"],
            "fetched_at": _state["fetched_at"].isoformat()
                           if _state["fetched_at"] else None,
        }
