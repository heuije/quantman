"""static.market_cap 피드 — KR 시가총액·거래대금·상장주식수 이력 (공식 KRX Open API `sto`).

소스: `sto/stk_bydd_trd`(KOSPI)·`sto/ksq_bydd_trd`(KOSDAQ) — basDd 하루 1콜=그날 전종목의
MKTCAP(시총)·ACC_TRDVAL(거래대금)·LIST_SHRS(상장주식수). 이력 2010~. 각 basDd=그날 실제값
이라 PIT-safe(as_of=거래일). 기존 FDR 스냅샷(krx_cache 메모리·스크리너 전용·이력 없음)의
"백테스트 store 미부착" 갭을 근본 해소하는 수집 경로.

⚠ **`sto` 서비스는 KRX 포털에서 별도 신청 필요**(서비스 단위 인가 — idx/bon/drv/etp 기신청과
별개). 미신청 상태의 에러 페이로드를 휴장 빈응답으로 오인하면 커서가 데이터 없이 침묵 전진
(거짓 완료)하므로, generic `_fetch_day`를 쓰지 않고 **OutBlock 존재를 명시 검사**하는 전용
fetcher를 둔다: OutBlock 없음(에러)=None(실패·커서 유지·재시도) / OutBlock 빈 리스트=휴장.

저장: 종목별 parquet(`marketcap/{code}.parquet`, as_of=거래일 인덱스) — flow_kr 패턴.
컬럼: market_cap·trade_value·shares_listed. **수집만 먼저** — indicators/엔진 소비 배선은
라이브 데이터로 응답 형태(ISU_CD 포맷 등) 검증 후 후속(미검증 배선 금지).
"""

from __future__ import annotations

import pandas as pd
import requests

from ... import data_fetcher as _df
from ...parquet_io import read_parquet_safe, write_parquet_atomic
from .krx_openapi import _BASE, _key, is_active  # AUTH_KEY 공유(같은 키·서비스만 별도 인가)

_SVC_STK = "sto/stk_bydd_trd"       # 유가증권(KOSPI) 일별매매정보
_SVC_KSQ = "sto/ksq_bydd_trd"       # 코스닥 일별매매정보
_SERVICES = (_SVC_STK, _SVC_KSQ)

MARKETCAP_DIR = _df.DATA_DIR / "marketcap"

_COLS = ["market_cap", "trade_value", "shares_listed"]


def _fetch_day(service: str, bas_dd: str, timeout: int = 60):
    """전용 fetcher — OutBlock 명시 검사로 '에러 페이로드'와 '휴장 빈응답'을 구분.

    반환: list(OutBlock 존재 — 휴장이면 []) / None(네트워크·HTTP·에러 페이로드 = 실패).
    krx_openapi._fetch_day는 에러 dict도 []로 접어 미신청 서비스에서 커서가 침묵 전진하는
    거짓 완료를 낳는다 — 여기선 그 부류를 차단한다.
    """
    key = _key()
    if not key:
        return None
    try:
        r = requests.get(f"{_BASE}/{service}", headers={"AUTH_KEY": key},
                         params={"basDd": bas_dd}, timeout=timeout)
    except Exception:
        return None
    if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
        return None
    try:
        j = r.json()
    except Exception:
        return None
    blk = next((v for v in j.values() if isinstance(v, list)), None)
    return blk                      # None=에러 페이로드(OutBlock 부재) — 휴장 []와 구분됨


def _num(x):
    """문자열 숫자 → float. 빈값/'-' → None (가짜 0 금지)."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _short_code(isu: str) -> str | None:
    """ISU_CD → 6자 단축코드. 12자 ISIN(KR7005930003)이면 [3:9], 6자면 그대로. 그 외 None."""
    s = str(isu or "").strip()
    if len(s) == 12 and s.upper().startswith("KR"):
        return s[3:9]
    if len(s) == 6:
        return s
    return None


def extract_rows(rows: list[dict]) -> dict[str, tuple]:
    """일별매매정보 행들 → {6자코드: (market_cap, trade_value, shares_listed)}. 순수함수.

    셋 다 None인 행은 제외(무의미). 코드 미해석 행 skip(정직 — 오귀속 금지).
    """
    out: dict[str, tuple] = {}
    for r in rows:
        code = _short_code(r.get("ISU_CD"))
        if code is None:
            continue
        mc, tv, sh = _num(r.get("MKTCAP")), _num(r.get("ACC_TRDVAL")), _num(r.get("LIST_SHRS"))
        if mc is None and tv is None and sh is None:
            continue
        out[code] = (mc, tv, sh)
    return out


def _merge_save(code: str, df_new: pd.DataFrame) -> None:
    """종목별 parquet에 as_of 기준 merge(신규 우선·정정 반영) 후 atomic write."""
    p = MARKETCAP_DIR / f"{code}.parquet"
    existing = read_parquet_safe(p)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing[~existing.index.isin(df_new.index)], df_new]).sort_index()
    else:
        merged = df_new.sort_index()
    write_parquet_atomic(merged, p)


def fetch_range(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """[sdate, edate] 평일의 KOSPI+KOSDAQ 시총·거래대금을 수집해 종목별 parquet에 merge.

    날짜×서비스 단위 수집을 메모리에 모아 **종목당 1회** write(윈도우 배치 — 날짜마다
    수천 파일 재작성 방지). 하루라도 실패(None)면 ok=False(커서 유지·재시도 — 부분 전진
    금지: 윈도우 중간 구멍 방지). 반환 {"ok", "days", "fail", "stocks"}.
    """
    if not is_active():
        return {"ok": False, "inactive": True, "days": 0, "fail": 0, "stocks": 0}
    from .krx_openapi import _weekdays
    per_code: dict[str, list] = {}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        day_rows: list[dict] = []
        day_failed = False
        for svc in _SERVICES:
            rows = fetch(svc, bd)
            if rows is None:                 # 에러 페이로드/네트워크 — 휴장 아님
                day_failed = True
                break
            day_rows.extend(rows)
        if day_failed:
            fail += 1
            continue
        days += 1
        ts = pd.to_datetime(bd, format="%Y%m%d")
        for code, (mc, tv, sh) in extract_rows(day_rows).items():
            per_code.setdefault(code, []).append(
                {"as_of": ts, "market_cap": mc, "trade_value": tv, "shares_listed": sh})
    if fail:                                 # 부분 전진 금지 — 윈도우 전체 재시도(멱등)
        return {"ok": False, "days": days, "fail": fail, "stocks": 0}
    if per_code:
        MARKETCAP_DIR.mkdir(parents=True, exist_ok=True)
        for code, recs in per_code.items():
            df_new = pd.DataFrame(recs).set_index("as_of")[_COLS].astype("float64")
            _merge_save(code, df_new)
        _df.mark_data_dirty()
    return {"ok": True, "days": days, "fail": 0, "stocks": len(per_code)}


def load_marketcap(code: str) -> pd.DataFrame:
    """종목별 시총·거래대금 이력 로드(소비층 진입점 — 후속 배선이 사용). 없으면 빈 DF."""
    df = read_parquet_safe(MARKETCAP_DIR / f"{code}.parquet")
    return df if df is not None else pd.DataFrame()
