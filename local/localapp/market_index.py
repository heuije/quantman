"""종목 → 시장/거래소 매핑 (로컬 주문 라우팅).

KIS 미국 마스터(NAS/NYS/AMS)를 받아 {티커: {exchange, currency, kind}} 인덱스를
APP_DIR에 캐싱한다. 주문 발주 시 정확한 거래소(EXCD)를 결정하는 권위 소스다.

견고성: 서버가 끊겨도 로컬이 자급해 보유분 청산·손절을 올바른 거래소로 보낸다
(runner 견고성 원칙). 코드길이 휴리스틱(과거 _detect_market)을 대체한다.

안전: 미국 티커(영문)인데 인덱스에 없으면(다운로드 실패 등) 거래소를 추측하지
않고 RuntimeError로 발주를 차단한다 — 오라우팅으로 인한 거부/오체결 방지.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from .config import APP_DIR
from .file_security import restrict_to_owner

log = logging.getLogger("localapp.market_index")

_CACHE_PATH = APP_DIR / ".us_market_index.json"
_STALE_DAYS = 2          # 이보다 오래되면 갱신 권장(읽기는 계속 가능)

_lock = threading.Lock()
_state: dict = {"by_ticker": None, "fetched_at": None}


class RoutingError(RuntimeError):
    """거래소를 확정할 수 없어 발주를 차단해야 하는 상태."""


def refresh(timeout: int = 30) -> dict:
    """미국 마스터를 새로 받아 인덱스를 디스크·메모리에 저장."""
    from . import kis_master
    rows = kis_master.fetch_us_masters(timeout=timeout)
    if not rows:
        log.warning("미국 마스터 0건 — 인덱스 갱신 실패 (기존 캐시 유지)")
        return {"ok": False, "n": 0}

    by_ticker: dict[str, dict] = {}
    for r in rows:
        by_ticker[r["symbol"]] = {
            "exchange": r["exchange"],
            "currency": r["currency"],
            "kind": r["kind"],
        }
    fetched_at = datetime.now(timezone.utc).isoformat()
    payload = {"fetched_at": fetched_at, "by_ticker": by_ticker}
    try:
        _CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding="utf-8")
        restrict_to_owner(_CACHE_PATH)
    except Exception as e:
        log.warning("미국 인덱스 캐시 저장 실패(메모리만 사용): %s", e)

    with _lock:
        _state["by_ticker"] = by_ticker
        _state["fetched_at"] = fetched_at
    log.info("미국 마스터 인덱스 갱신 — %d종목", len(by_ticker))
    return {"ok": True, "n": len(by_ticker), "fetched_at": fetched_at}


def _ensure_loaded() -> dict:
    """메모리 인덱스 확보. 없으면 캐시 로드, 캐시도 없으면 1회 다운로드.

    트레이딩 사이클 도중 implicit 네트워크 호출을 피하려 캐시를 우선한다.
    캐시가 stale해도 읽기는 계속 — 갱신은 startup/scheduler가 담당.
    """
    with _lock:
        if _state["by_ticker"] is not None:
            return _state["by_ticker"]

    # 캐시 로드 시도
    if _CACHE_PATH.exists():
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            with _lock:
                _state["by_ticker"] = data.get("by_ticker", {})
                _state["fetched_at"] = data.get("fetched_at")
            return _state["by_ticker"]
        except Exception as e:
            log.warning("미국 인덱스 캐시 로드 실패 — 재다운로드: %s", e)

    # 캐시 없음 → 1회 다운로드
    refresh()
    with _lock:
        return _state["by_ticker"] or {}


def is_stale() -> bool:
    """캐시가 _STALE_DAYS보다 오래됐는지 — scheduler 갱신 판단용."""
    with _lock:
        fa = _state["fetched_at"]
    if not fa:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fa)
        return age.days >= _STALE_DAYS
    except Exception:
        return True


def _looks_domestic(symbol: str) -> bool:
    """국내(KRX) 코드 형태 — 6자리 숫자/알파넘(ETF 등). 미국 티커는 영문 1~5자."""
    s = symbol.strip().upper()
    return len(s) >= 6 and s[:6].isalnum() and not s.isalpha()


def _candidates(symbol: str) -> list[str]:
    """티커 심볼로지 정규화 후보 — 점·대시·슬래시 클래스 표기 차이 흡수.

    KIS 마스터/주문 PDNO는 슬래시 형식(BRK/B, BF/B). yfinance는 대시(BRK-B),
    위키피디아 S&P500은 점(BRK.B). 입력이 어떤 형식이든 KIS 키로 찾는다.
    """
    s = symbol.strip().upper()
    out = [s]
    for a, b in (("/", "."), ("/", "-"), (".", "/"), ("-", "/")):
        if a in s:
            v = s.replace(a, b)
            if v not in out:
                out.append(v)
    return out


def _lookup(symbol: str) -> tuple[str, dict] | None:
    """정규화 후보로 인덱스를 조회 → (KIS 키, meta) 또는 None."""
    idx = _ensure_loaded()
    for cand in _candidates(symbol):
        meta = idx.get(cand)
        if meta:
            return cand, meta
    return None


def _looks_us_ticker(symbol: str) -> bool:
    """미국 티커 형태 — 영문 1~5자, 선택적 클래스 접미(.B/-B//B)."""
    import re
    s = symbol.strip().upper()
    return bool(re.fullmatch(r"[A-Z]{1,5}([./-][A-Z])?", s))


def exchange_of(symbol: str) -> str | None:
    """미국 거래소(NAS/NYS/AMS) 반환. 미국 종목이 아니면 None."""
    hit = _lookup(symbol)
    return hit[1].get("exchange") if hit else None


def is_us(symbol: str) -> bool:
    return _lookup(symbol) is not None


def currency_of(symbol: str) -> str:
    """결제 통화 — 미국이면 USD, 그 외 KRW(국내 기본)."""
    hit = _lookup(symbol)
    return hit[1].get("currency", "KRW") if hit else "KRW"


def kis_ticker_of(symbol: str) -> str:
    """KIS 주문 PDNO로 보낼 정규화 티커. 미국 종목은 슬래시 형식(BRK/B).

    미국 종목이 아니면 입력을 그대로(국내 6자리 코드 등) 반환.
    """
    hit = _lookup(symbol)
    return hit[0] if hit else symbol.strip().upper()


def market_group_of(symbol: str) -> str:
    """스케줄·사이클 배칭용 시장 그룹 — 'US' 또는 'KRX'.

    선물은 계약 스펙(instrument_spec)이 SSOT — 국내선물(KRW)=KRX, 해외선물(USD)=US.
    주식은 미국 인덱스에 있으면 US, 국내 코드 형태면 KRX. 둘 다 아니고 미국 티커
    형태면(인덱스 미로드 등) 추측하지 않고 RoutingError — 잘못된 배칭 방지.

    ※ 선물을 먼저 가르는 이유: US 마스터·_looks_domestic 휴리스틱은 주식용이라 한글
    선물명을 잘못 분류한다 — 해외선물('원유선물' 등)이 폴백 KRX로 새던 잠재 오라우팅 차단.
    """
    from quant_core.exec_defaults import instrument_spec
    sp = instrument_spec(symbol)
    if sp.asset_class == "futures":
        return "KRX" if sp.currency == "KRW" else "US"
    if is_us(symbol):
        return "US"
    if _looks_domestic(symbol):
        return "KRX"
    if _looks_us_ticker(symbol):
        raise RoutingError(
            f"미국 티커로 보이나 마스터 인덱스에 없음: {symbol} — "
            f"인덱스 갱신 필요(market_index.refresh). 발주 보류.")
    return "KRX"      # 안전한 국내 기본
