"""flow.us_short_volume 피드 — US 일별 공매도 거래량 (FINRA Reg SHO consolidated).

소스: FINRA 일별 Consolidated NMS 파일(cdn.finra.org/equity/regsho/daily/CNMSshvol
{YYYYMMDD}.txt) — 무키·무로그인, pipe-delimited(Date|Symbol|ShortVolume|ShortExemptVolume|
TotalVolume|Market), ~12k 종목/일(2026-07 라이브 프로브 검증). **통합 포맷은 2018-08-01~**
(이전은 TRF별 구포맷 — 후속·floor 갱신 시점에 병합). 휴장일엔 파일 미게시(404)=비거래일.

⚠ 정직 한계: 이 수치는 **off-exchange(TRF 보고분) 공매도 거래량**이지 시장 전체가 아니며,
공매도 잔고(short interest·포지션 스냅샷)와 다른 데이터다(잔고는 2010 floor 불가로 제외).

저장: 종목별 parquet(`short_volume/{SYM}.parquet`, as_of=거래일 인덱스) — flow_kr 규약.
컬럼: short_volume·short_exempt_volume·total_volume(전부 off-exchange 기준 원시값 —
직교 프리미티브. 비율·급증은 소비층/시계열 연산자 파생). as_of=거래일(당일 18:00 ET 게시 —
KR 수급과 동일 규약·spec 노트 명시). **수집만 먼저** — 엔진 소비 배선(indicators attach·
컴파일러 노출)은 marketcap과 묶어 후속(golden 영향 검증 포함. 엔진이 계산 못 하는 컬럼을
컴파일러에 노출하지 않는다).
"""

from __future__ import annotations

import pandas as pd
import requests

from ... import data_fetcher as _df
from ...parquet_io import read_parquet_safe, write_parquet_atomic

_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"

SHORTVOL_DIR = _df.DATA_DIR / "short_volume"

# 소스 자연 floor — FINRA consolidated 포맷 시작일(구포맷 병합 시 갱신).
SOURCE_FLOOR = "2018-08-01"

# 저장 컬럼 — spec.provides·매니페스트 _FIELD_GROUPS가 참조(단일 출처).
SHORTVOL_COLS = ["short_volume", "short_exempt_volume", "total_volume"]
_COLS = SHORTVOL_COLS


def _fetch_day(bd: str, timeout: int = 30) -> str | None:
    """하루치 파일 텍스트. 404=비거래일(빈 문자열 반환과 구분해 ''), 그 외 실패=None.

    FINRA CDN은 부재 파일에 **403**(S3 AccessDenied)을, 때로 404를 반환한다 — 2026-07-03
    프로덕션 실측: 게시=200·미게시(당일/주말)=403(111바이트). 공개 CDN(무인증)이라 403의
    유일 의미=객체 부재 → 404와 함께 비거래일/미게시('')로 취급. 이걸 실패로 취급하면
    최신일 포함 창이 항상 실패해 백필이 wedged된다(프로덕션 첫 청크 실측).
    네트워크/5xx는 None(호출자 실패 처리·커서 유지·재시도).
    """
    try:
        r = requests.get(_URL.format(d=bd), timeout=timeout)
    except Exception:
        return None
    if r.status_code in (403, 404):
        return ""                          # 비거래일/미게시 — 정상(빈 하루)
    if r.status_code != 200:
        return None
    return r.text


def parse_file(text: str) -> dict[str, tuple]:
    """파일 텍스트 → {심볼: (short, short_exempt, total)}. 순수함수(픽스처 테스트).

    헤더·트레일러·비정형 행은 skip(정직 — 오파싱 값 미적재).
    """
    out: dict[str, tuple] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5 or parts[0] == "Date":
            continue
        try:
            sym = parts[1].strip()
            sv, se, tv = float(parts[2]), float(parts[3]), float(parts[4])
        except (ValueError, IndexError):
            continue
        if not sym:
            continue
        out[sym] = (sv, se, tv)
    return out


def _merge_save(sym: str, df_new: pd.DataFrame) -> None:
    """종목별 parquet에 as_of 기준 merge(신규 우선) 후 atomic write. '/'는 '_' 치환.

    존재 체크 후 read — read_parquet_safe는 '손상 격리'용이지 '부재 허용'이 아니라서
    부재 파일에 부르면 격리 시도 소음 로그가 신규 종목 수만큼 쏟아진다(프로덕션 실측)."""
    p = SHORTVOL_DIR / f"{sym.replace('/', '_')}.parquet"
    existing = read_parquet_safe(p) if p.exists() else None
    if existing is not None and not existing.empty:
        merged = pd.concat([existing[~existing.index.isin(df_new.index)], df_new]).sort_index()
    else:
        merged = df_new.sort_index()
    write_parquet_atomic(merged, p)


def fetch_range(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """[sdate, edate] 평일의 공매도량을 수집해 종목별 parquet에 merge.

    날짜별 수집을 메모리에 모아 **종목당 1회** write(윈도우 배치). 하루라도 실패(None)면
    ok=False(커서 유지·전체 재시도 — 부분 전진 금지·멱등). 반환 {"ok","days","fail","stocks"}.
    """
    from .krx_openapi import _weekdays          # 평일 이터레이터 재사용(휴장은 404가 처리)
    per_sym: dict[str, list] = {}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        text = fetch(bd)
        if text is None:
            fail += 1
            continue
        days += 1
        if not text:
            continue                            # 비거래일(404) — 빈 하루
        ts = pd.to_datetime(bd, format="%Y%m%d")
        for sym, (sv, se, tv) in parse_file(text).items():
            per_sym.setdefault(sym, []).append(
                {"as_of": ts, "short_volume": sv, "short_exempt_volume": se,
                 "total_volume": tv})
    if fail:                                    # 부분 전진 금지 — 윈도우 전체 재시도
        return {"ok": False, "days": days, "fail": fail, "stocks": 0}
    if per_sym:
        SHORTVOL_DIR.mkdir(parents=True, exist_ok=True)
        for sym, recs in per_sym.items():
            df_new = pd.DataFrame(recs).set_index("as_of")[_COLS].astype("float64")
            _merge_save(sym, df_new)
        _df.mark_data_dirty()
    return {"ok": True, "days": days, "fail": 0, "stocks": len(per_sym)}


def load_short_volume(sym: str) -> pd.DataFrame:
    """종목별 공매도량 이력 로드(소비층 진입점 — 후속 배선이 사용). 없으면 빈 DF."""
    df = read_parquet_safe(SHORTVOL_DIR / f"{sym.replace('/', '_')}.parquet")
    return df if df is not None else pd.DataFrame()
