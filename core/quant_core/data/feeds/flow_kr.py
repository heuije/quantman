"""flow.kr_investor (KR) 피드 — 종목별 일별 기관·외국인 순매수(거래대금).

소스: KRX 정보데이터시스템(data.krx.co.kr) via **pykrx** — 일별 전종목
`get_market_net_purchases_of_equities_by_ticker`(하루·시장·투자자당 1콜 = 그날 전종목).
KRX가 2025-12-27 전체 로그인 의무화 → 무료 KRX 계정(native id/pw) `KRX_ID`/`KRX_PW`로 pykrx가
로그인. 조회 무료·이력 ~2010. **미설정 시 feed 비활성**(빈결과·골든 무영향).

⚠️ 옛 경로(`get_market_trading_value_by_date`·종목당 1콜 = 3,579콜/전종목)는 KRX MDC 봇차단을
유발했다(대량 백필 시 `get_stock_ticker_isin: NoneType` → 이후 전부 빈응답·조용한 영구결손).
일별 전종목 1콜 패턴은 요청 수·형태가 달라 차단을 회피한다 — marketcap_krx(공식 API 일별 전종목)와
동일 원리(로컬 실증: 2010~2026 전종목 `blocked=0`). 저장 형식·소비 계약은 옛 경로와 동일.

저장: 종목별 parquet(`flow/{code}.parquet`·`as_of`=거래일 인덱스·일별) — `inst_net_buy`(기관합계)·
`foreign_net_buy`(외국인). 원시 일별 컬럼만 적재(직교 프리미티브) — 'N일 연속/누적 순매수'·'급증'은
ts_sum·rolling·pct 등 기존 시계열 연산자로 조합. 수집은 marketcap_krx.fetch_range와 동형(날짜축
cursor 백필·종목당 1 write·부분 전진 금지). 소비: data_fetcher.load_flow_all(무변경).
"""

from __future__ import annotations

import os
import time

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import write_parquet_atomic

# 우리 컬럼 → pykrx 투자자 인자(한글). 순서 = spec provides 계약(test_provides_match_spec_contract).
_COL_MAP = {"inst_net_buy": "기관합계", "foreign_net_buy": "외국인"}
_MARKETS = ("KOSPI", "KOSDAQ")
_VALUE_COL = "순매수거래대금"        # 일별 전종목 함수의 순매수 거래대금(원) 컬럼
_MAX_RETRY = 3
_THROTTLE_S = 0.2                    # 콜 간 throttle(로그인 세션 보호 — 실증값)


def _has_login() -> bool:
    """KRX 로그인 자격 존재 여부 — 없으면 feed 비활성(pykrx 헛호출·경고 스팸 방지)."""
    return bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))


def _flow_path(code: str):
    return default_manifest_path().parent / "flow" / f"{code.replace('/', '_')}.parquet"


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


def _fetch_day_investor(market: str, investor_kr: str, bas_dd: str, fetch=None):
    """그날·시장·투자자의 전종목 순매수 → df(index=티커·`순매수거래대금` 포함) | None.

    None = 봇차단/네트워크(재시도 소진 — 호출자가 그 윈도우를 fail 처리·커서 유지).
    빈 df = 휴장/무데이터(정상 skip). 휴장일 pykrx 내부 'Length mismatch'(0 elements)는 빈 df로
    접어 차단 오판(fail)을 막는다. fetch 주입 가능(단위테스트 — 실 로그인·네트워크 없음).
    """
    if fetch is None:
        from pykrx import stock
        fetch = stock.get_market_net_purchases_of_equities_by_ticker
    for _ in range(_MAX_RETRY):
        try:
            return fetch(bas_dd, bas_dd, market, investor_kr)
        except Exception as e:                          # noqa: BLE001
            msg = str(e)
            if "Length mismatch" in msg or "0 elements" in msg:
                return pd.DataFrame()                   # 휴장/무데이터 — 차단 아님(재시도·fail 금지)
            time.sleep(2)
    return None                                          # 재시도 소진 = 차단/네트워크


def fetch_range(sdate: str, edate: str, fetch=None) -> dict:
    """[sdate, edate] 평일의 KOSPI+KOSDAQ 기관·외국인 순매수(거래대금)를 일별 전종목으로 수집해
    종목별 parquet에 merge. marketcap_krx.fetch_range와 동형(날짜 배치·종목당 1 write).

    하루라도 실패(None=차단/네트워크)면 그 즉시 중단·ok=False(커서 유지·윈도우 전체 재시도 —
    부분 전진 금지: 윈도우 중간 구멍 방지). 반환 {"ok", "days", "fail", "stocks"}.
    **KRX_ID/PW 미설정 시 inactive**(no-op). fetch 주입 가능(단위테스트).
    """
    if not _has_login():
        return {"ok": False, "inactive": True, "days": 0, "fail": 0, "stocks": 0}
    from .krx_openapi import _weekdays        # 역순 평일 'YYYYMMDD' — marketcap/컨센서스 공용

    per_code: dict[str, list] = {}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        day_rows: dict[str, dict] = {}
        blocked = False
        for our_col, inv in _COL_MAP.items():
            for mkt in _MARKETS:
                df = _fetch_day_investor(mkt, inv, bd, fetch=fetch)
                if df is None:                          # 차단/네트워크 — 휴장 아님
                    blocked = True
                    break
                if not df.empty and _VALUE_COL in df.columns:
                    for code, val in df[_VALUE_COL].items():
                        v = pd.to_numeric(val, errors="coerce")
                        if pd.notna(v):
                            day_rows.setdefault(str(code), {})[our_col] = float(v)
                time.sleep(_THROTTLE_S)
            if blocked:
                break
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
            for c in _COL_MAP:                          # 누락 투자자 컬럼 채움(스키마 고정)
                if c not in df_new.columns:
                    df_new[c] = pd.NA
            _merge_write(code, df_new[list(_COL_MAP)])
    return {"ok": True, "days": days, "fail": 0, "stocks": len(per_code), "saved": len(per_code)}
