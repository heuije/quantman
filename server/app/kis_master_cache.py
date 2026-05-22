"""KIS 종목마스터 서버 캐시 — 국내 + 해외(미국·일본·홍콩).

KIS 공식 URL에서 매일 마스터를 다운로드해 메모리 캐싱한다.

수록 시장:
- 국내: KOSPI (.mst, 고정폭) / KOSDAQ (.mst, 고정폭)
- 해외: NASDAQ / NYSE / AMEX / 도쿄 (TSE) / 홍콩 (HKS) — 모두 .cod (탭 TSV)

펀드(F-prefix 9자리) · 채권성 상품(meta[1]='B')은 자동매매에 부적합해 제외.
종목 메타: kind (stock/etf_etn/reits), currency (KRW/USD/JPY/HKD), market (KOSPI/NAS/...).
"""

from __future__ import annotations

import io
import logging
import threading
import urllib.request
import zipfile
from datetime import datetime, timezone

log = logging.getLogger("app.kis_master")

# 국내 (.mst 고정폭)
KOSPI_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
KOSDAQ_URL = "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"
_META_LEN = {"KOSPI": 228, "KOSDAQ": 222}

# 해외 (.cod 탭 TSV, 24 컬럼)
OVERSEAS_URLS = {
    "NAS": "https://new.real.download.dws.co.kr/common/master/nasmst.cod.zip",   # NASDAQ
    "NYS": "https://new.real.download.dws.co.kr/common/master/nysmst.cod.zip",   # NYSE
    "AMS": "https://new.real.download.dws.co.kr/common/master/amsmst.cod.zip",   # AMEX
    "TSE": "https://new.real.download.dws.co.kr/common/master/tsemst.cod.zip",   # 도쿄
    "HKS": "https://new.real.download.dws.co.kr/common/master/hksmst.cod.zip",   # 홍콩
}

_OVERSEAS_CCY = {"NAS": "USD", "NYS": "USD", "AMS": "USD",
                  "TSE": "JPY", "HKS": "HKD"}

_lock = threading.Lock()
_state = {
    "symbols": set(),
    "by_symbol": {},      # {code: {name, market, kind, currency}}
    "fetched_at": None,
    "n_kospi": 0,
    "n_kosdaq": 0,
    "n_nas": 0, "n_nys": 0, "n_ams": 0,
    "n_tse": 0, "n_hks": 0,
}


def _download_mst(url: str, timeout: int = 30) -> bytes:
    """ZIP을 받아 내부 첫 마스터 파일의 raw bytes를 반환 (.mst 또는 .cod 통용)."""
    req = urllib.request.Request(url, headers={"User-Agent": "quant-platform-server"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        zdata = r.read()
    with zipfile.ZipFile(io.BytesIO(zdata)) as z:
        names = z.namelist()
        # .mst 우선, 없으면 .cod
        name = next((n for n in names if n.lower().endswith(".mst")), None) \
               or next((n for n in names if n.lower().endswith(".cod")), None) \
               or names[0]
        return z.read(name)


# ── 국내 .mst 파서 (고정폭) ───────────────────────────────────────────────────

def _parse_domestic(raw: bytes, market: str) -> list[dict]:
    """KOSPI/KOSDAQ .mst — 고정폭 라인 + 메타 영역.

    메타[1]: 'S'=주식, 'E'=ETF/ETN, 'R'=REITs, 'B'=채권성(제외).
    F-prefix 9자리: 펀드(제외).
    """
    meta_len = _META_LEN[market]
    out: list[dict] = []
    for row in raw.decode("cp949", errors="ignore").splitlines():
        if len(row) <= meta_len + 21:
            continue
        code = row[0:9].rstrip()
        name = row[21:len(row) - meta_len].strip()
        meta = row[-meta_len:]
        meta_byte1 = meta[1] if len(meta) >= 2 else ""
        if not code or not code[:6].isalnum():
            continue
        if len(code) == 9 and code.startswith("F"):
            continue            # 펀드
        if meta_byte1 == "B":
            continue            # 채권성
        kind = {"S": "stock", "E": "etf_etn",
                "R": "reits"}.get(meta_byte1, "stock")
        out.append({"symbol": code, "name": name, "market": market,
                     "kind": kind, "currency": "KRW"})
    return out


# ── 해외 .cod 파서 (탭 TSV) ──────────────────────────────────────────────────

def _parse_overseas(raw: bytes, exchange: str) -> list[dict]:
    """NAS/NYS/AMS/TSE/HKS .cod — 탭 구분 24컬럼.

    핵심 컬럼:
      [4] 단축 티커 (KIS overseas API의 OVRS_PDNO)
      [6] 한글명, [7] 영문명
      [8] 종목구분 (2=주식, 3=ETF)
      [9] 통화 (USD/JPY/HKD)
    """
    out: list[dict] = []
    ccy = _OVERSEAS_CCY.get(exchange, "USD")
    for row in raw.decode("cp949", errors="ignore").splitlines():
        if "\t" not in row:
            continue
        cols = row.split("\t")
        if len(cols) < 10:
            continue
        ticker = cols[4].strip()
        if not ticker:
            continue
        kor_name = cols[6].strip()
        eng_name = cols[7].strip()
        sec_type = cols[8].strip()
        kind = "etf_etn" if sec_type == "3" else "stock"
        out.append({
            "symbol": ticker,
            "name": kor_name or eng_name,
            "market": exchange,
            "kind": kind,
            "currency": ccy,
        })
    return out


# ── 마스터 새로 받기 ─────────────────────────────────────────────────────────

def refresh() -> dict:
    """국내 + 해외 마스터를 모두 새로 받아 캐시 교체.

    실패한 시장은 직전 캐시 값 유지 (graceful degradation).
    """
    new_by_symbol: dict[str, dict] = {}
    n_per: dict[str, int] = {}
    any_success = False

    # 국내 KOSPI / KOSDAQ
    for market, url in [("KOSPI", KOSPI_URL), ("KOSDAQ", KOSDAQ_URL)]:
        try:
            raw = _download_mst(url)
            rows = _parse_domestic(raw, market)
            for r in rows:
                # 단일 키 충돌 방지: 같은 6자리 코드가 KOSPI/KOSDAQ에 동시에 있을 일 없음
                new_by_symbol[r["symbol"]] = {
                    "name": r["name"], "market": market,
                    "kind": r["kind"], "currency": r["currency"],
                }
            n_per[market] = len(rows)
            any_success = True
            log.info("KIS 마스터 [%s] %d개 갱신", market, len(rows))
        except Exception as e:
            log.warning("KIS 마스터 [%s] 다운로드 실패: %s", market, e)
            n_per[market] = -1

    # 해외 NAS/NYS/AMS/TSE/HKS
    for exchange, url in OVERSEAS_URLS.items():
        try:
            raw = _download_mst(url)
            rows = _parse_overseas(raw, exchange)
            for r in rows:
                # 국내와 충돌 가능성 낮지만, 충돌 시 거래소 prefix로 namespace
                # 단순화: 티커가 국내 종목과 겹치면 'NAS:AAPL' 같은 키 사용
                key = r["symbol"]
                if key in new_by_symbol:
                    key = f"{exchange}:{r['symbol']}"
                new_by_symbol[key] = {
                    "name": r["name"], "market": exchange,
                    "kind": r["kind"], "currency": r["currency"],
                    "ticker": r["symbol"],   # 실제 KIS API에 보낼 코드 (NAS:AAPL → AAPL)
                }
            n_per[exchange] = len(rows)
            any_success = True
            log.info("KIS 마스터 [%s] %d개 갱신", exchange, len(rows))
        except Exception as e:
            log.warning("KIS 마스터 [%s] 다운로드 실패: %s", exchange, e)
            n_per[exchange] = -1

    if not any_success:
        return {"ok": False, "fetched_at": None}

    with _lock:
        # 부분 성공 시 실패한 시장은 직전 값 유지
        merged = dict(_state["by_symbol"])
        merged.update(new_by_symbol)
        _state["by_symbol"] = merged
        _state["symbols"] = set(_state["by_symbol"].keys())
        _state["fetched_at"] = datetime.now(timezone.utc)
        for k, n in [
            ("n_kospi", n_per.get("KOSPI", -1)),
            ("n_kosdaq", n_per.get("KOSDAQ", -1)),
            ("n_nas", n_per.get("NAS", -1)),
            ("n_nys", n_per.get("NYS", -1)),
            ("n_ams", n_per.get("AMS", -1)),
            ("n_tse", n_per.get("TSE", -1)),
            ("n_hks", n_per.get("HKS", -1)),
        ]:
            if n >= 0:
                _state[k] = n

    return {
        "ok": True,
        "n_total": len(_state["symbols"]),
        "n_kospi": _state["n_kospi"], "n_kosdaq": _state["n_kosdaq"],
        "n_nas": _state["n_nas"], "n_nys": _state["n_nys"], "n_ams": _state["n_ams"],
        "n_tse": _state["n_tse"], "n_hks": _state["n_hks"],
        "fetched_at": _state["fetched_at"].isoformat(),
    }


def get_fetched_epoch() -> int:
    """마지막 마스터 갱신 시각의 epoch(초). 미갱신이면 0.

    dataset 버전과 함께 /symbols 응답 캐시의 무효화 키로 쓴다 — 마스터가 새로
    받아지면 이 값이 바뀌어 캐시가 자동 재빌드된다.
    """
    with _lock:
        fa = _state["fetched_at"]
        return int(fa.timestamp()) if fa else 0


def get_master_set() -> set[str]:
    with _lock:
        return set(_state["symbols"])


def get_master_list() -> list[dict]:
    """전 종목 — [{symbol, name, market, kind, currency, ticker?}, ...]."""
    with _lock:
        out = []
        for code, meta in _state["by_symbol"].items():
            row = {
                "symbol": code,
                "name": meta.get("name", ""),
                "market": meta.get("market", ""),
                "kind": meta.get("kind", "stock"),
                "currency": meta.get("currency", "KRW"),
            }
            if "ticker" in meta:
                row["ticker"] = meta["ticker"]
            out.append(row)
        return out


def get_status() -> dict:
    with _lock:
        return {
            "n_symbols": len(_state["symbols"]),
            "n_kospi": _state["n_kospi"], "n_kosdaq": _state["n_kosdaq"],
            "n_nas": _state["n_nas"], "n_nys": _state["n_nys"], "n_ams": _state["n_ams"],
            "n_tse": _state["n_tse"], "n_hks": _state["n_hks"],
            "fetched_at": _state["fetched_at"].isoformat()
                           if _state["fetched_at"] else None,
        }
