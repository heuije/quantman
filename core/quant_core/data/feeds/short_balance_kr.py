"""short_balance.kr (KR) 피드 — 종목별 일별 공매도 잔고(수량·금액·비중).

소스: KRX 정보데이터시스템(data.krx.co.kr) via **pykrx** `get_shorting_balance_by_ticker(date, market)`
— 일별·시장당 1콜(그날 전종목). KRX 2025-12-27 전체 로그인 의무화 → 무료 KRX 계정 `KRX_ID`/`KRX_PW`로
pykrx가 로그인. 조회 무료·이력 ~2010. **미설정 시 feed 비활성**(빈결과·골든 무영향).

⚠️ 옛 웹 경로(`krdata._shorting`: KRX MDC OTP→CSV·종목당 1콜·봇차단 취약·in-memory lru라 재배포마다
콜드 재크롤)를 대체한다. 일별 전종목 1콜 패턴은 flow_kr·marketcap_krx와 동일 원리(요청 수·형태가
달라 차단 회피). 저장 형식·소비 계약(bal_qty/bal_amt/bal_ratio)은 웹 그대로.

저장: 종목별 parquet(`shortbal/{code}.parquet`·`as_of`=거래일 인덱스·일별) — `bal_qty`(공매도 잔고수량)·
`bal_amt`(공매도 잔고금액·원)·`bal_ratio`(잔고비중·%). 원시 일별만 적재(직교 프리미티브 — 'N일 급증'
등은 시계열 연산자로 조합). 수집은 flow_kr.fetch_range와 동형(날짜축 cursor 백필·종목당 1 write·부분
전진 금지). 소비: 웹 kr_extras가 종목별 최근 시계열로 서빙(krdata.shorting 피드 우선 + 라이브 KRX 폴백).
"""

from __future__ import annotations

import os
import time

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import read_parquet_safe, write_parquet_atomic

# 우리 컬럼 → pykrx get_shorting_balance_by_ticker 반환 컬럼(한글). substring 매칭(정확명·위치 비의존):
# 반환 컬럼 = 공매도잔고·상장주식수·공매도금액·시가총액·비중.
_COL_KEYS = {"bal_qty": ("공매도잔고",), "bal_amt": ("공매도금액",), "bal_ratio": ("비중",)}
_MARKETS = ("KOSPI", "KOSDAQ")
_MAX_RETRY = 3
_THROTTLE_S = 0.2                    # 콜 간 throttle(로그인 세션 보호 — flow_kr 실증값)


def _has_login() -> bool:
    """KRX 로그인 자격 존재 여부 — 없으면 feed 비활성(pykrx 헛호출·경고 스팸 방지)."""
    return bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))


def _short_path(code: str):
    return default_manifest_path().parent / "shortbal" / f"{code.replace('/', '_')}.parquet"


def load_short_balance(code: str) -> pd.DataFrame:
    """종목 공매도 잔고 일별 시계열(bal_qty/bal_amt/bal_ratio) — 볼륨 parquet. 없으면 빈 DF."""
    p = _short_path(code)
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)
    return df if df is not None else pd.DataFrame()


def load_short_balance_all() -> dict[str, pd.DataFrame]:
    """전 종목 공매도 잔고 패널 — data_cache aux 캐시(디렉터리 주도·요청 시에만 로드)."""
    out: dict[str, pd.DataFrame] = {}
    d = default_manifest_path().parent / "shortbal"
    if d.exists():
        for p in d.glob("*.parquet"):
            df = read_parquet_safe(p)
            if df is not None and not df.empty:
                out[p.stem] = df
    return out


def _merge_write(code: str, new: pd.DataFrame) -> None:
    """새 윈도우를 기존 parquet에 merge(as_of dedup·최신 우선) — 백필·증분 공용. 원자적 기록."""
    p = _short_path(code)
    if p.exists():
        old = pd.read_parquet(p)
        merged = pd.concat([old, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new.sort_index()
    write_parquet_atomic(merged, p)


def _pick_col(df: pd.DataFrame, keys: tuple[str, ...]):
    """한글 헤더 substring 매칭(컬럼 순서·정확명 비의존) — pykrx 헤더 변화에 강건."""
    for c in df.columns:
        s = str(c).replace(" ", "")
        if all(k in s for k in keys):
            return c
    return None


def _fetch_day(market: str, bas_dd: str, fetch=None):
    """그날·시장의 전종목 공매도 잔고 → df(index=티커·공매도잔고/금액/비중 포함) | None.

    None = 봇차단/네트워크(재시도 소진 — 호출자가 그 윈도우를 fail 처리·커서 유지).
    빈 df = 휴장/무데이터(정상 skip). 휴장일 pykrx 내부 'Length mismatch'(0 elements)는 빈 df로
    접어 차단 오판(fail)을 막는다. fetch 주입 가능(단위테스트 — 실 로그인·네트워크 없음).
    """
    if fetch is None:
        from pykrx import stock
        fetch = stock.get_shorting_balance_by_ticker
    for _ in range(_MAX_RETRY):
        try:
            return fetch(bas_dd, market)
        except Exception as e:                          # noqa: BLE001
            msg = str(e)
            if "Length mismatch" in msg or "0 elements" in msg:
                return pd.DataFrame()                   # 휴장/무데이터 — 차단 아님(재시도·fail 금지)
            time.sleep(2)
    return None                                          # 재시도 소진 = 차단/네트워크


def fetch_range(sdate: str, edate: str, fetch=None) -> dict:
    """[sdate, edate] 평일의 KOSPI+KOSDAQ 전종목 공매도 잔고를 일별로 수집해 종목별 parquet에 merge.
    flow_kr.fetch_range와 동형(날짜 배치·종목당 1 write).

    하루라도 실패(None=차단/네트워크)면 그 즉시 중단·ok=False(커서 유지·윈도우 전체 재시도 —
    부분 전진 금지: 윈도우 중간 구멍 방지). 반환 {"ok", "days", "fail", "stocks"}.
    **KRX_ID/PW 미설정 시 inactive**(no-op). fetch 주입 가능(단위테스트).
    """
    if not _has_login():
        return {"ok": False, "inactive": True, "days": 0, "fail": 0, "stocks": 0}
    from .krx_openapi import _weekdays        # 역순 평일 'YYYYMMDD' — marketcap/flow 공용

    per_code: dict[str, list] = {}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        day_rows: dict[str, dict] = {}
        blocked = False
        for mkt in _MARKETS:
            df = _fetch_day(mkt, bd, fetch=fetch)
            if df is None:                              # 차단/네트워크 — 휴장 아님
                blocked = True
                break
            if not df.empty:
                cols = {our: _pick_col(df, keys) for our, keys in _COL_KEYS.items()}
                for code, row in df.iterrows():
                    rec: dict = {}
                    for our, c in cols.items():
                        if c is not None:
                            v = pd.to_numeric(row.get(c), errors="coerce")
                            if pd.notna(v):
                                rec[our] = float(v)
                    if rec:
                        day_rows.setdefault(str(code), {}).update(rec)
            time.sleep(_THROTTLE_S)
        if blocked:
            fail = 1
            break                                       # 부분 전진 금지 — 윈도우 전체 재시도(멱등)
        days += 1
        ts = pd.to_datetime(bd, format="%Y%m%d")
        for code, cols in day_rows.items():
            per_code.setdefault(code, []).append({"as_of": ts, **cols})

    if fail:                                            # 커서 유지 — 다음 청크가 같은 윈도우 재시도
        return {"ok": False, "days": days, "fail": fail, "stocks": 0}
    if per_code:
        for code, recs in per_code.items():
            df_new = pd.DataFrame(recs).set_index("as_of")
            for c in _COL_KEYS:                          # 누락 컬럼 채움(스키마 고정)
                if c not in df_new.columns:
                    df_new[c] = pd.NA
            _merge_write(code, df_new[list(_COL_KEYS)])
    return {"ok": True, "days": days, "fail": 0, "stocks": len(per_code), "saved": len(per_code)}
