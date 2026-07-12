"""KR 선물·ETF 투자자별 수급 피드 — 외국인·기관 일별 순매수 대금(원)의 매크로 심볼 6종.

소스: KRX 정보데이터시스템 MDC getJsonData(data.krx.co.kr — 로그인) 일별추이 화면 2종.
  · 선물 = MDCSTAT13102([13102] 파생상품 투자자별 거래실적). 상품 지정은 `isuCd`이며 값은
    파생상품ID의 KRDRV→KR___(언더스코어 3개) 변형: 코스피200선물 KR___FUK2I·코스닥150선물
    KR___FUKQI. ⚠ prodId=KRDRVFUK2I로 보내면 에러 없이 전부 0(조용한 무데이터 — 실측 함정).
  · ETF = MDCSTAT04802([13106] ETF 투자자별 거래실적 일별추이, 시장 전체 집계) —
    inqCondTpCd1=1(거래대금)·inqCondTpCd2=1(순매수).
둘 다 이력 2005~ 실측(2010 Core floor 초과)·당일 최종치는 18시 이후 제공(증분 cron 18:40).

컬럼→투자자 매핑(2026-07-12 산술 확정 — 같은 구간을 라벨 있는 기간합계 13101/04801로 조회해
10거래일 합산 대조·전 컬럼 ±2원 내 정확 일치. 추측 아님):
  13102: A07=기관합계 · A08=기타법인 · A09=개인 · A12=외국인
  04802: NUM_ITM_VAL21=기관합계 · 22=기타법인 · 23=개인 · 24=외국인(외국인+기타외국인 정확합)
         · 25=전체(순매수 항등 0)
단위: 원 — money=1/money=3 응답 동일(비율 1.0 실측) → TRDVAL은 파라미터 무관 원 고정.

명명 시계열 6종(코스피200선물미결제약정·KRETF순자금유입·COT와 동형 — 값=일별 순매수 대금·원):
  {코스피200선물|코스닥150선물|KRETF}{외국인|기관}순매수
노출 투자자는 실수요(외국인·기관) 2종만 — 세분류(보험·투신 등)는 수요 실측 후 추가.
'N일 연속/누적 순매도' 류는 ts_sum·streak 등 기존 시계열 연산자로 조합(원시 일별만 적재).

수집은 소스(상품/ETF)당 기간 1콜 — 선물 13102는 서버 소요가 거래일당 ~0.1s 선형(240일≈24s
실측·600일은 45s 타임아웃 초과)이라 백필 청크 240일 기준 전체 이력 ~26청크×3콜로 끝난다.
KRX_ID/PW 미설정 시 feed 비활성(빈결과·골든 무영향). 하나라도 실패면 전체 중단·저장 없음·
커서 유지(부분 전진 금지 — flow_kr 규약). 로그인 세션은 pykrx 공용(flow_kr와 같은 인프라 —
만료 시 내부 재로그인·프로덕션 일일 검증 경로)."""

from __future__ import annotations

import os
import time

import pandas as pd

from ... import data_fetcher as _df

_MDC_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_REFERER = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201050302"
_FUT_BLD = "dbms/MDC/STAT/standard/MDCSTAT13102"
_ETF_BLD = "dbms/MDC/STAT/standard/MDCSTAT04802"

_FUT_PRODUCTS = {"코스피200선물": "KR___FUK2I", "코스닥150선물": "KR___FUKQI"}
_INVESTORS = ("외국인", "기관")
_FUT_COLS = {"외국인": "A12", "기관": "A07"}                      # 매핑 근거: 모듈 docstring
_ETF_COLS = {"외국인": "NUM_ITM_VAL24", "기관": "NUM_ITM_VAL21"}  # 매핑 근거: 모듈 docstring
_SUFFIX = "순매수"

_MAX_RANGE_DAYS = 300   # 구간조회 상한 — 선물 13102 소요가 거래일당 ~0.1s 선형(240일≈24s 실측),
                        # 장기창은 read timeout. 백필 청크 240일 + 헤드룸.
_MAX_RETRY = 3
_RETRY_SLEEP_S = 2.0
_THROTTLE_S = 1.0       # 콜 간 스로틀(pykrx 관행 — 소스당 1콜이라 총 ~3초/윈도우)

# 챗 카탈로그·spec·매니페스트가 소비하는 심볼 목록(data_fetcher.MACRO_FLOW_DERIV_SYMBOLS의
# 원천). 정합은 드리프트 가드가 잠근다(test_flow_deriv_kr).
SYMBOLS: list[str] = ([p + inv + _SUFFIX for p in _FUT_PRODUCTS for inv in _INVESTORS]
                      + ["KRETF" + inv + _SUFFIX for inv in _INVESTORS])


def _has_login() -> bool:
    """KRX 로그인 자격 존재 여부 — 없으면 feed 비활성(flow_kr 게이트 동형)."""
    return bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))


def _post_mdc(payload: dict):
    """로그인 세션으로 getJsonData 1콜 → JSON dict | None(네트워크/비200/비JSON — 호출자 재시도)."""
    from pykrx.website.comm.auth import get_auth_session
    krxs = get_auth_session()
    headers = dict(krxs.get_headers())
    headers["Referer"] = _REFERER
    try:
        r = krxs.session.post(_MDC_URL, data=payload, headers=headers, timeout=90)  # 240일≈24s 실측 ×4 헤드룸
        if r.status_code != 200:
            return None
        j = r.json()
        return j if isinstance(j, dict) else None
    except Exception:                                   # noqa: BLE001
        return None


def _fut_payload(isu_cd: str, sdate: str, edate: str) -> dict:
    """13102 실측 payload — 웹 폼이 보내는 그대로(빈 문자열 필드 포함)."""
    return {"bld": _FUT_BLD, "locale": "ko_KR", "isuCd": isu_cd, "isuOpt": "ALL",
            "inqTpCd": "2", "share": "1", "money": "3", "prtType": "AMT", "prtCheck": "SUN",
            "strtDd": sdate, "endDd": edate, "aggBasTpCd": "", "isuCd2": "", "prodId": ""}


def _etf_payload(sdate: str, edate: str) -> dict:
    return {"bld": _ETF_BLD, "locale": "ko_KR", "strtDd": sdate, "endDd": edate,
            "inqCondTpCd1": "1", "inqCondTpCd2": "1"}   # 거래대금·순매수


def parse_rows(rows: list[dict], cols: dict[str, str]) -> pd.DataFrame:
    """일별추이 행 → DataFrame(index=거래일, columns=투자자, 값=원). 순수함수(픽스처 테스트).

    수치는 콤마 포함 문자열("-8,979") — 콤마 제거 후 수치화. 날짜 불량 행은 drop, 응답이
    역순(최신 먼저)이라 정렬, 중복 거래일은 마지막 값(정정 반영). 매핑 컬럼이 통째로 없으면
    그 컬럼은 전행 NaN으로 남는다(호출자가 스키마 드리프트로 fail 처리)."""
    recs = []
    for r in rows:
        try:
            d = pd.to_datetime(str(r["TRD_DD"]), format="%Y/%m/%d")
        except (KeyError, ValueError):
            continue
        rec = {"date": d}
        for inv, col in cols.items():
            v = pd.to_numeric(str(r.get(col, "")).replace(",", ""), errors="coerce")
            rec[inv] = float(v) if pd.notna(v) else None
        recs.append(rec)
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).set_index("date").sort_index()
    return df[~df.index.duplicated(keep="last")]


def _fetch_window(payload: dict, cols: dict[str, str], post=None):
    """한 소스의 [strtDd,endDd] 일별추이 → 투자자 wide df | None=실패(호출자 중단·커서 유지).

    실패로 치는 것: 네트워크/비200(재시도 소진) · 매핑 소스컬럼 *키*가 전 행에 부재(스키마
    드리프트) · 파싱 가능한 행 0(형식 드리프트) · ≥5행 전부 정확히 0(isuCd 형식 드리프트의
    조용한 무데이터 — prodId 함정과 같은 부류·실측). 빈 output=휴장/상장 전 구간(정상 skip).
    키는 있는데 값이 빈 문자열인 시대(실측: 코스닥150선물 상장 초기 A12='' 전면 — 외국인 미집계,
    A07+A08+A09=0 항등)는 NaN → 그 날만 해당 투자자 시리즈에서 자연 제외(window fail 아님 —
    소스가 안 주는 걸 실패로 치면 백필 커서가 그 경계에서 영구 정체한다).
    post 주입 가능(단위테스트 — 실 로그인·네트워크 없음)."""
    post = post or _post_mdc
    j = None
    for _ in range(_MAX_RETRY):
        j = post(payload)
        if j is not None:
            break
        time.sleep(_RETRY_SLEEP_S)
    if j is None:
        return None
    rows = j.get("output") or []
    if not rows:
        return pd.DataFrame()
    for src in cols.values():
        if not any(src in r for r in rows):
            return None                                 # 키 자체가 전 행 부재 = 스키마 드리프트
    df = parse_rows(rows, cols)
    if df.empty:
        return None                                     # 행은 오는데 파싱 0건 = 형식 드리프트
    if len(df) >= 5 and (df.fillna(0.0) == 0.0).all().all():
        return None                                     # 조용한 전부-0 — isuCd 변형 규칙 드리프트
    return df


def _save_series(name: str, val: pd.Series) -> int:
    """값 시리즈 → 명명 parquet(OHLCV-format·매크로 패턴) merge-save. cot_cftc와 동형."""
    val = val.dropna().astype("float64")
    if val.empty:
        return 0
    df = pd.DataFrame({"Open": val, "High": val, "Low": val, "Close": val, "Volume": 0.0})
    df.index = pd.to_datetime(df.index).tz_localize(None)
    merged = _df._merge(_df._load_existing(name), df)
    _df._save(name, merged)
    return len(val)


def fetch_range(sdate: str, edate: str, post=None) -> dict:
    """[sdate,edate](YYYYMMDD)의 선물 2상품+ETF 시장 수급을 소스당 1콜로 수집해 매크로 심볼
    6종에 merge-save(멱등). 하나라도 실패면 전체 중단·저장 없음·ok=False(커서 유지 —
    부분 전진 금지). 윈도우는 _MAX_RANGE_DAYS 이하(호출자 계약 — 백필 청크 240일·증분 7일).

    반환 {"ok", "saved": {심볼: 저장행수}, "fail"?: 실패소스, "inactive"?}. KRX_ID/PW 미설정 시
    inactive no-op. post 주입 가능(단위테스트)."""
    if not _has_login():
        return {"ok": False, "inactive": True, "saved": {}}
    if (pd.to_datetime(edate, format="%Y%m%d")
            - pd.to_datetime(sdate, format="%Y%m%d")).days > _MAX_RANGE_DAYS:
        raise ValueError(f"윈도우 {_MAX_RANGE_DAYS}일 초과 — 백필 청크로 나눠 호출하세요")

    frames: dict[str, pd.DataFrame] = {}                # 전 소스 성공 후에만 저장(원자성)
    for prod, isu in _FUT_PRODUCTS.items():
        df = _fetch_window(_fut_payload(isu, sdate, edate), _FUT_COLS, post=post)
        if df is None:
            return {"ok": False, "fail": prod, "saved": {}}
        frames[prod] = df
        time.sleep(_THROTTLE_S)
    df = _fetch_window(_etf_payload(sdate, edate), _ETF_COLS, post=post)
    if df is None:
        return {"ok": False, "fail": "KRETF", "saved": {}}
    frames["KRETF"] = df

    saved: dict[str, int] = {}
    for prefix, f in frames.items():
        for inv in _INVESTORS:
            if inv in f.columns:
                saved[prefix + inv + _SUFFIX] = _save_series(prefix + inv + _SUFFIX, f[inv])
    if any(saved.values()):
        _df.mark_data_dirty()
    return {"ok": True, "saved": saved}
