"""flow.kr_investor (KR) 피드 — 종목별 일별 기관·외국인 순매수(거래대금).

소스: KRX 정보데이터시스템(data.krx.co.kr) via **pykrx** — `get_market_trading_value_by_date`.
KRX가 2025-12-27 전체 로그인 의무화 → 무료 KRX 계정(native id/pw)의 `KRX_ID`/`KRX_PW` env로
pykrx가 로그인. 조회는 무료, 이력 ~2010. **미설정 시 feed 비활성**(빈결과·골든 무영향).

저장: 종목별 parquet(`as_of`=거래일 인덱스, 일별 dense) — `inst_net_buy`(기관합계)·`foreign_net_buy`(외국인).
'N일 연속/누적 순매수'·'급증'은 ts_sum·rolling·pct 등 기존 시계열 연산자로 사용자가 조합
(원시 일별 컬럼만 적재 — 직교 프리미티브). 백필(start=~2014)·일일증분 모두 merge로 한 경로.

⚠️ pykrx는 봇차단된 KRX 웹을 스크랩 → 취약(버전핀·retry/resume·캐시). pykrx 내부가 2년청크+throttle.
"""

from __future__ import annotations

import os
import time

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import write_parquet_atomic

# pykrx 투자자 컬럼(한글) → 우리 컬럼. substring 매칭(필러 표기 변화·순서에 robust).
_COL_MAP = {"inst_net_buy": "기관합계", "foreign_net_buy": "외국인"}


def _has_login() -> bool:
    """KRX 로그인 자격 존재 여부 — 없으면 feed 비활성(pykrx 헛호출·경고 스팸 방지)."""
    return bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))


def fetch_one(code: str, start: str, end: str) -> tuple:
    """종목 1개의 일별 기관/외국인 순매수(거래대금) → (df[as_of 인덱스], ok).

    df 컬럼: inst_net_buy·foreign_net_buy. ok=False면 로그인/네트워크 오류(호출자 마커 금지·재시도),
    ok=True+빈df면 genuine 무데이터(상폐·신규). start/end='YYYYMMDD'. pykrx가 2년초과 자동청크.
    """
    from pykrx import stock
    try:
        raw = stock.get_market_trading_value_by_date(start, end, code)
    except Exception:                              # 로그인 실패·네트워크·파싱 — transient(마커 금지)
        return pd.DataFrame(), False
    if raw is None or raw.empty:
        return pd.DataFrame(), True                # genuine 무데이터

    def _col(want_kr: str):                         # 한글 컬럼 substring 매칭(표기·순서 비의존)
        for c in raw.columns:
            if want_kr in str(c).replace(" ", ""):
                return c
        return None

    out = pd.DataFrame(index=pd.to_datetime(raw.index))
    out.index.name = "as_of"
    found = False
    for our, kr in _COL_MAP.items():
        c = _col(kr)
        if c is not None:
            out[our] = pd.to_numeric(raw[c], errors="coerce")
            found = True
        else:
            out[our] = pd.NA
    if not found:                                   # 두 컬럼 다 못찾음 = 스키마 변동 → 빈결과(정직)
        return pd.DataFrame(), True
    return out, True


def _flow_path(code: str):
    return default_manifest_path().parent / "flow" / f"{code.replace('/', '_')}.parquet"


def _marker_path(code: str):
    """빈결과(상폐·수급 없음) 시도 마커 — parquet 옆 `.empty`. mtime만 신선도 신호(reader 무영향)."""
    return _flow_path(code).with_suffix(".empty")


def _write_marker(code: str) -> None:
    m = _marker_path(code)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("")


def _clear_marker(code: str) -> None:
    m = _marker_path(code)
    if m.exists():
        m.unlink()


def _merge_write(code: str, new: pd.DataFrame) -> None:
    """새 윈도우를 기존 parquet에 merge(as_of dedup·최신 우선) — 백필·증분 공용. 원자적 기록."""
    p = _flow_path(code)
    if p.exists():
        old = pd.read_parquet(p)
        merged = pd.concat([old, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new.sort_index()
    write_parquet_atomic(merged, p)


def fetch(codes: list[str], start: str, end: str,
          budget_symbols: int = 200, fresh_days: int = 2) -> dict:
    """일별 수급 증분 수급 — 미수집·오래된 종목 우선, 종목 예산 내. **KRX_ID/PW 미설정 시 no-op**.

    백필=start 이른 날짜(예: '20140101')로 전 종목 점진 적재, 증분=start 최근(예: 최근 N일).
    성공 parquet·빈결과 마커 중 최신 mtime을 신선도 신호로 — fresh_days 이내 시도분 skip.
    """
    if not _has_login():
        return {"ok": 0, "empty": 0, "fail": 0, "symbols": 0, "inactive": True}

    now = time.time()

    def _attempted_mtime(c: str) -> float:
        ts = 0.0
        for p in (_flow_path(c), _marker_path(c)):
            if p.exists():
                ts = max(ts, p.stat().st_mtime)
        return ts

    n_ok = n_empty = n_fail = n_done = 0
    for c in sorted(codes, key=_attempted_mtime):       # 미시도(0)·오래된 시도 우선
        at = _attempted_mtime(c)
        if at and (now - at) < fresh_days * 86400:
            continue
        try:
            df, ok = fetch_one(c, start, end)
        except Exception:                                # noqa: BLE001 — 예외는 마커 금지(재시도 유지)
            n_fail += 1
            n_done += 1
            if n_done >= budget_symbols:
                break
            continue
        if not ok:                                       # 로그인/네트워크 — 마커 금지 + 청크 중단(망치질 방지)
            n_fail += 1
            break
        if df is not None and not df.empty:
            _merge_write(c, df)
            _clear_marker(c)
            n_ok += 1
        else:
            _write_marker(c)                             # genuine 무데이터
            n_empty += 1
        n_done += 1
        if n_done >= budget_symbols:
            break
    return {"ok": n_ok, "empty": n_empty, "fail": n_fail, "symbols": n_done, "inactive": False}
