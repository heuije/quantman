"""한국 종목 일별 스냅샷 캐시 — 스크리너 전용.

데이터 소스: FinanceDataReader (NAVER 금융 백엔드).
KRX 정보데이터시스템(data.krx.co.kr) 직접 호출은 403 차단되어 fdr 사용.

매일 16:30 KST 한 번 fetch:
- KOSPI + KOSDAQ 전 종목 (~2,800개) 한 번에
- 시세 (close/open/high/low), 시가총액, 거래량/거래대금
- 등락률, 상장주식수, 시장 분류

V1 한계 (KRX 사이트 차단 우회로 인한):
- PER/PBR/배당수익률 미포함 — NAVER 별도 스크래핑 필요, V1.1
- 외국인 한도소진율 미포함 — V1.1
- 관리종목 플래그: fdr StockListing의 'Dept' 필드로 대체 (관리/투자위험)

기술적 지표 (RSI/MA/momentum)는 별도 단계 (technical_cache).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import kis_master_cache

log = logging.getLogger("app.krx_cache")

KST = timezone(timedelta(hours=9))

_lock = threading.Lock()
_state: dict = {
    "snapshot_date": None,     # "YYYY-MM-DD" — fdr가 반환한 그 시점
    "metrics": {},             # {symbol: {field: value}}
    "fetched_at": None,
    "n_total": 0,
    "n_kospi": 0,
    "n_kosdaq": 0,
    "last_error": None,
}


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _to_num(v) -> float | None:
    """NaN·None을 None으로, 숫자면 float."""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:        # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    f = _to_num(v)
    return int(f) if f is not None else None


def _classify_dept(dept: str) -> tuple[bool, bool]:
    """fdr 'Dept' 필드 → (is_managed, is_halt) 분류.

    NAVER 데이터 기준 값 예시:
      "" / NaN          — 일반 종목
      "관리종목"          — 관리 지정
      "투자주의"          — 투자주의 환기
      "투자경고" / "투자위험" — 투자위험 종목
      "정리매매" / "거래정지" — 거래정지/정리
    """
    if dept is None:
        return False, False
    s = str(dept).strip()
    if not s or s.lower() == "nan":
        return False, False
    is_halt = ("거래정지" in s) or ("정리매매" in s)
    is_managed = ("관리" in s) or ("투자위험" in s) or ("투자경고" in s)
    return is_managed, is_halt


# ── 메인 fetch ────────────────────────────────────────────────────────────────

def refresh() -> dict:
    """FinanceDataReader로 KRX 전 종목 latest snapshot 받기.

    실패 시 직전 캐시 유지 (graceful degradation).
    """
    try:
        import FinanceDataReader as fdr
    except ImportError as e:
        log.error("FinanceDataReader 미설치: %s", e)
        with _lock:
            _state["last_error"] = "FinanceDataReader not installed"
        return {"ok": False, "error": "FinanceDataReader not installed"}

    try:
        log.info("KRX 스냅샷 fetch 시작 (fdr.StockListing 'KRX')")
        df = fdr.StockListing("KRX")
        if df is None or df.empty:
            raise RuntimeError("StockListing returned empty DataFrame")

        # KIS 마스터 — 우선주·종목구분(kind) 매칭
        master_list = kis_master_cache.get_master_list()
        master_by_code = {m["symbol"]: m for m in master_list
                          if m.get("currency") == "KRW"}

        metrics: dict[str, dict] = {}
        for _, row in df.iterrows():
            ticker = str(row.get("Code", "")).strip()
            if not ticker or len(ticker) < 6:
                continue
            market = str(row.get("Market", "")).strip()
            if market not in ("KOSPI", "KOSDAQ"):
                continue

            meta = master_by_code.get(ticker, {})
            kind = meta.get("kind", "stock")
            # 우선주 식별 — KIS 마스터에 없으면 종목명 패턴 ('우', '우B')
            name = str(row.get("Name", "")).strip()
            is_pref = bool(name) and (
                name.endswith("우") or name.endswith("우B") or name.endswith("우C"))
            # 관리·거래정지
            is_managed, is_halt = _classify_dept(row.get("Dept"))

            metrics[ticker] = {
                "symbol": ticker,
                "name": meta.get("name") or name,
                "market": market,
                "kind": kind,
                "is_pref": is_pref,
                "is_managed": is_managed,
                "is_halt": is_halt,
                # 시세
                "close":         _to_num(row.get("Close")),
                "open":          _to_num(row.get("Open")),
                "high":          _to_num(row.get("High")),
                "low":           _to_num(row.get("Low")),
                "volume":        _to_int(row.get("Volume")),
                "trade_value":   _to_num(row.get("Amount")),
                "pct_change_1d": _to_num(row.get("ChagesRatio")),  # fdr 컬럼 오타 보존
                "change_won":    _to_num(row.get("Changes")),
                # 시총
                "market_cap":    _to_num(row.get("Marcap")),
                "shares_listed": _to_int(row.get("Stocks")),
            }

        n_kospi  = sum(1 for m in metrics.values() if m["market"] == "KOSPI")
        n_kosdaq = sum(1 for m in metrics.values() if m["market"] == "KOSDAQ")
        snapshot_date = datetime.now(KST).strftime("%Y-%m-%d")

        with _lock:
            _state["snapshot_date"] = snapshot_date
            _state["metrics"] = metrics
            _state["fetched_at"] = datetime.now(timezone.utc)
            _state["n_total"] = len(metrics)
            _state["n_kospi"] = n_kospi
            _state["n_kosdaq"] = n_kosdaq
            _state["last_error"] = None

        log.info("KRX 스냅샷 갱신 완료: %s (KOSPI=%d, KOSDAQ=%d, 총=%d)",
                 snapshot_date, n_kospi, n_kosdaq, len(metrics))
        return {
            "ok": True,
            "snapshot_date": snapshot_date,
            "n_total": len(metrics),
            "n_kospi": n_kospi, "n_kosdaq": n_kosdaq,
            "fetched_at": _state["fetched_at"].isoformat(),
        }

    except Exception as e:
        log.exception("KRX 스냅샷 fetch 실패")
        with _lock:
            _state["last_error"] = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": str(e)}


# ── 조회 ──────────────────────────────────────────────────────────────────────

def get_metrics(symbol: str) -> dict | None:
    with _lock:
        return _state["metrics"].get(symbol)


def get_all_metrics() -> dict[str, dict]:
    with _lock:
        return dict(_state["metrics"])


def get_status() -> dict:
    with _lock:
        return {
            "snapshot_date": _state["snapshot_date"],
            "n_total": _state["n_total"],
            "n_kospi": _state["n_kospi"],
            "n_kosdaq": _state["n_kosdaq"],
            "fetched_at": _state["fetched_at"].isoformat()
                           if _state["fetched_at"] else None,
            "last_error": _state["last_error"],
        }
