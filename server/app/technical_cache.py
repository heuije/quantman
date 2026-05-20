"""기술적 지표 캐시 — RSI/MA/모멘텀/거래량비율.

FinanceDataReader로 시총 상위 500종목의 60일 OHLCV를 받아 quant_core.indicators의
compute_all로 지표 계산. 최신 행만 추출해 krx_cache의 메모리에 merge.

매일 17:00 KST KRX 캐시 직후 cron 실행. 500종목 × 0.4초 = 약 3분 (workers=5).

V1.1 활성화 지표 (daily_metrics에 추가되는 필드):
- rsi_14, ma_dev_5/20/60 (이평 괴리), bb_pct, atr_14_pct
- momentum_12_1m (12개월-1개월 모멘텀)
- volume_ratio_20d (20일 평균 대비 거래량)
- pct_change_5d / _20d (단기/중기 수익률)
- dist_52w_high_pct (52주 신고가 대비 거리)

KRX 시계열 historical은 V2 (백테스트용 데이터 누적 후) — 본 모듈은 latest snapshot만.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import quant_core as qc

from . import krx_cache

log = logging.getLogger("app.technical_cache")

# 시총 상위 N개만 — 매일 ~3분으로 cron 부담 회피
DEFAULT_UNIVERSE_SIZE = 500
# OHLCV fetch window — RSI 14 + MA 60 + momentum 252 등 다 cover하려면 길게
LOOKBACK_DAYS = 280

_lock = threading.Lock()
_state: dict = {
    "fetched_at": None,
    "n_universe": 0,
    "n_ok": 0,
    "n_fail": 0,
    "last_error": None,
}


def _pick_universe(size: int) -> list[str]:
    """시총 상위 size개 KOSPI+KOSDAQ 종목 코드 — 우선주·ETF·관리 제외."""
    all_metrics = krx_cache.get_all_metrics()
    candidates = []
    for sym, m in all_metrics.items():
        if m.get("kind") != "stock":         continue
        if m.get("is_pref"):                 continue
        if m.get("is_managed"):              continue
        if m.get("is_halt"):                 continue
        cap = m.get("market_cap") or 0
        if cap <= 0:                          continue
        candidates.append((sym, cap))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in candidates[:size]]


def _fetch_one(symbol: str, start: str, end: str) -> dict | None:
    """단일 종목 OHLCV 받아 지표 계산. 최신 행 dict 반환."""
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(symbol, start, end)
        if df is None or df.empty or len(df) < 20:
            return None
        # fdr 컬럼 — Open/High/Low/Close/Volume/Change. 일부 종목 'Volume' 누락 처리
        if "Volume" not in df.columns:
            df["Volume"] = 0
        # compute_all은 fundamental df도 받지만 None이면 skip
        df = qc.compute_all(df, fund_df=None)
        # 최신 행에서 필요한 필드만
        last = df.iloc[-1]
        out = {}
        for col in ("rsi_14", "atr_14", "atr_14_pct", "bb_pct", "bb_width",
                    "momentum_12_1m", "volume_ratio_20d",
                    "pct_change_5d", "pct_change_20d", "pct_change_252d",
                    "ma_dev_20d", "ma_dev_60d", "ma_dev_200d", "ma_gap_20_60",
                    "high_dev_20d", "log_return_1d", "streak"):
            if col in last.index:
                v = last[col]
                try:
                    fv = float(v)
                    if fv == fv:                # not NaN
                        out[col] = fv
                except (TypeError, ValueError):
                    pass
        return out or None
    except Exception as e:
        log.debug("OHLCV fetch/지표 실패 [%s]: %s", symbol, e)
        return None


def refresh(universe_size: int = DEFAULT_UNIVERSE_SIZE,
             workers: int = 5, *, max_symbols: int | None = None) -> dict:
    """시총 상위 N종목 기술적 지표 갱신.

    Args:
        universe_size: 대상 종목 수 (기본 500).
        workers: 동시 fetch worker.
        max_symbols: 디버그용 상한.
    """
    symbols = _pick_universe(universe_size)
    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    today = datetime.now(timezone(timedelta(hours=9))).date()
    start = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    log.info("기술적 지표 fetch 시작: %d종목, workers=%d, window=%d일",
             len(symbols), workers, LOOKBACK_DAYS)
    started = datetime.now(timezone.utc)
    n_ok = 0
    n_fail = 0
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, s, start, end): s for s in symbols}
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

    # krx_cache 메모리에 merge
    metrics_all = krx_cache.get_all_metrics()
    merged = 0
    for sym, ind in results.items():
        if sym in metrics_all:
            metrics_all[sym].update(ind)
            merged += 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    with _lock:
        _state["fetched_at"] = datetime.now(timezone.utc)
        _state["n_universe"] = len(symbols)
        _state["n_ok"] = n_ok
        _state["n_fail"] = n_fail
        _state["last_error"] = None

    log.info("기술적 지표 갱신 완료: ok=%d, fail=%d, merge=%d, 소요=%.1fs",
             n_ok, n_fail, merged, elapsed)

    return {
        "ok": True,
        "n_universe": len(symbols),
        "n_ok": n_ok,
        "n_fail": n_fail,
        "merged": merged,
        "elapsed_sec": round(elapsed, 1),
        "fetched_at": _state["fetched_at"].isoformat(),
    }


def get_status() -> dict:
    with _lock:
        return {
            "n_universe": _state["n_universe"],
            "n_ok": _state["n_ok"],
            "n_fail": _state["n_fail"],
            "fetched_at": _state["fetched_at"].isoformat()
                           if _state["fetched_at"] else None,
            "last_error": _state["last_error"],
        }
