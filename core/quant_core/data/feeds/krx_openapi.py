"""KRX 정보데이터시스템 **공식 Open API** (data-dbg.krx.co.kr/svc/apis) 피드.

로그인벽 `data.krx.co.kr` MDC와 **별개** — `AUTH_KEY` 헤더 + `basDd`(하루 1콜=그날 전종목)·
이력 2010~. `KRX_API_KEY` 미설정 시 no-op(비활성·로그 침묵, flow_kr 패턴).

매크로형 명명 시계열(VIX·미국채처럼 하루 1값):
  S1 시장지표 — 코스피200변동성지수(V-KOSPI) · 옵션풋콜비율 · KRX채권지수 · 국고채3년 · 국고채10년
  S2 선물 OI — 코스피200선물미결제약정 · 코스닥150선물미결제약정
  S3 ETF — KRETF순자산총액(AUM) · KRETF순자금유입(Δ상장좌수×NAV)
데이터포인트당 소스 1개 원칙(no-backup) — 이 지표들의 진실원천 = 공식 KRX API.

라이브 검증(2026-06-30): 전 서비스 2010 깊이·필드 확정. 옵션은 하루 ~19k행이라 timeout 90s.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import pandas as pd
import requests

from ... import data_fetcher as _df

_BASE = "https://data-dbg.krx.co.kr/svc/apis"

# 서비스 경로 (라이브 검증된 slug)
_SVC_DRVPROD = "idx/drvprod_dd_trd"   # 파생상품지수 (V-KOSPI 포함)
_SVC_BOND_IDX = "idx/bon_dd_trd"      # 채권지수
_SVC_KTS = "bon/kts_bydd_trd"         # 국채전문유통시장 일별
_SVC_FUT = "drv/fut_bydd_trd"         # 선물 일별 (미결제약정 OI)
_SVC_ETF = "etp/etf_bydd_trd"         # ETF 일별 (순자산·상장좌수 → AUM·flow)
_SVC_OPT = "drv/opt_bydd_trd"         # 옵션 일별 (P/C용·무거움)


def _key() -> str:
    return os.getenv("KRX_API_KEY", "").strip()


def is_active() -> bool:
    """AUTH_KEY 존재 여부 — 없으면 feed 비활성(헛호출·경고 스팸 방지)."""
    return bool(_key())


def _fetch_day(service: str, bas_dd: str, timeout: int = 30):
    """공식 KRX API 하루치 호출 → row dict 리스트.

    반환: list(성공·휴장 포함 — 휴장은 []), None(실패 — 호출자가 `fail += 1`로 세고 재시도).

    🔴 **에러 페이로드를 `[]`로 접지 않는다.** KRX는 미신청 서비스·키 오류에도 HTTP 200 +
    JSON을 주는데 그 본문엔 OutBlock 리스트가 없다. 종전엔 `blk is None → []`로 접어
    호출자가 "휴장(데이터 없음)"으로 읽고 **커서를 전진**시켰다 — 그 구간이 영구 유실되고
    `ok=True`라 재시도도 안 걸리는 조용한 거짓 완료. 형제 `marketcap_krx._fetch_day`가
    이 부류를 이미 진단해 **사본에서만** 고쳤던 것을 뿌리로 승격한다(2026-07-21).
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
    blk = next((j[k] for k in j if isinstance(j[k], list)), None)
    return blk      # None=에러 페이로드(OutBlock 부재) — 휴장 []와 구분한다(위 docstring)


def _f(x):
    """문자열 숫자 → float. 빈값/'-' → None."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── 추출기 (순수 함수 — 네트워크 무관·단위테스트 대상) ─────────────────────────

def extract_vkospi(rows: list[dict]):
    """파생상품지수에서 코스피200 변동성지수(V-KOSPI) 종가."""
    for r in rows:
        if r.get("IDX_NM") == "코스피 200 변동성지수":
            return _f(r.get("CLSPRC_IDX"))
    return None


def extract_bond_index(rows: list[dict]):
    """채권지수에서 'KRX 채권지수' 총수익지수."""
    for r in rows:
        if r.get("BND_IDX_GRP_NM") == "KRX 채권지수":
            return _f(r.get("TOT_EARNG_IDX"))
    return None


def extract_ktb_yield(rows: list[dict], maturity: str):
    """국채전문에서 명목 국고채 지표물(benchmark)의 만기별 종가수익률(CLSPRC_YD).

    BND_EXP_TP_NM=만기년수('3','10'…)·GOVBND_ISU_TP_NM='지표'(벤치마크)·
    명목채(ISU_NM '국고…', 물가연동·원금분리 제외).
    """
    for r in rows:
        if (r.get("BND_EXP_TP_NM") == maturity
                and r.get("GOVBND_ISU_TP_NM") == "지표"
                and str(r.get("ISU_NM", "")).startswith("국고")
                and "원금" not in str(r.get("ISU_NM", ""))):
            return _f(r.get("CLSPRC_YD"))
    return None


def extract_futures_oi(rows: list[dict], prod_name: str):
    """선물 일별에서 특정 상품의 총 미결제약정(OI) = 주간 월물 OI 합.

    ⚠ 주간/야간 세션이 같은 계약 OI를 두 행으로 보고 → '(야간)' 제외해 이중계산 방지.
    야간 도입(2016~) 전 데이터는 세션 접미사가 없어 그대로 포함된다.
    """
    total = 0.0
    found = False
    for r in rows:
        if r.get("PROD_NM") != prod_name:
            continue
        if "(야간)" in str(r.get("ISU_NM", "")):
            continue
        oi = _f(r.get("ACC_OPNINT_QTY"))
        if oi is not None:
            total += oi
            found = True
    return total if found else None


_CONTRACT_RE = re.compile(r"\bF\s+(\d{6})\b")   # "코스피200 F 202609 (주간)" → 202609


def extract_futures_panel(rows: list[dict], prod_name: str) -> list[dict]:
    """선물 일별에서 특정 상품의 **outright 만기물별 일봉** → [{contract, OHLC, Settle, Volume, OI}].

    스프레드(ISU_NM 'SP …'·계약월 정규식 미스로 제외)·야간 세션 제외. 계약월 = 'F YYYYMM'.
    OHLC(TDD_*)는 비유동 원월이면 결측 → build_continuous가 Settle로 폴백.
    """
    out = []
    for r in rows:
        if r.get("PROD_NM") != prod_name:
            continue
        nm = str(r.get("ISU_NM", ""))
        if "(야간)" in nm:
            continue
        mo = _CONTRACT_RE.search(nm)
        if not mo:                                # outright 아님(스프레드 등)
            continue
        out.append({
            "contract": mo.group(1),
            "Open": _f(r.get("TDD_OPNPRC")), "High": _f(r.get("TDD_HGPRC")),
            "Low": _f(r.get("TDD_LWPRC")), "Close": _f(r.get("TDD_CLSPRC")),
            "Settle": _f(r.get("SETL_PRC")), "Volume": _f(r.get("ACC_TRDVOL")),
            "OI": _f(r.get("ACC_OPNINT_QTY")),
        })
    return out


def extract_putcall(rows: list[dict]):
    """옵션 일별에서 코스피200 옵션(미니 제외) 풋/콜 **거래량** 비율 = PUT_vol / CALL_vol.

    표준 시장심리 P/C(거래량 기준). 코스피200 정규+위클리 포함, 미니·코스닥150 제외.
    """
    call_v = put_v = 0
    for r in rows:
        prod = str(r.get("PROD_NM", ""))
        if not prod.startswith("코스피200") or "미니" in prod:
            continue
        v = _f(r.get("ACC_TRDVOL")) or 0.0
        rt = r.get("RGHT_TP_NM")
        if rt == "CALL":
            call_v += v
        elif rt == "PUT":
            put_v += v
    if call_v <= 0:
        return None
    return round(put_v / call_v, 4)


def extract_etf_aum(rows: list[dict]):
    """전 ETF 순자산총액 합 = KR ETF 시장 총 AUM(원)."""
    total = 0.0
    found = False
    for r in rows:
        a = _f(r.get("INVSTASST_NETASST_TOTAMT"))
        if a is not None:
            total += a
            found = True
    return total if found else None


def compute_etf_flow(prev_rows: list[dict], cur_rows: list[dict]):
    """전 ETF 순자금유입(원) = Σ (상장좌수_t − 상장좌수_{t-1}) × NAV_t (양일 공통 ISU).

    상장좌수(LIST_SHRS) 변화 = 설정/환매(creation/redemption) → 가격효과를 제거한 순수 자금흐름.
    AUM의 단순 차분(ΔAUM)은 가격등락이 섞이지만, Δ좌수×NAV는 실제 유출입만 잡는다.
    """
    prev = {}
    for r in prev_rows:
        s = _f(r.get("LIST_SHRS"))
        if s is not None:
            prev[r.get("ISU_CD")] = s
    flow = 0.0
    found = False
    for r in cur_rows:
        isu = r.get("ISU_CD")
        s = _f(r.get("LIST_SHRS"))
        nav = _f(r.get("NAV"))
        if isu in prev and s is not None and nav is not None:
            flow += (s - prev[isu]) * nav
            found = True
    return flow if found else None


# ── 시리즈 정의: (시리즈명, 서비스, 추출기) ────────────────────────────────────

# 가벼운 지표(서비스 3종·각 수~수백행) — 한 백필 청크에서 넓은 윈도우 가능
_LIGHT_SERIES = [
    ("코스피200변동성지수", _SVC_DRVPROD, extract_vkospi),
    ("KRX채권지수", _SVC_BOND_IDX, extract_bond_index),
    ("국고채3년", _SVC_KTS, lambda rows: extract_ktb_yield(rows, "3")),
    ("국고채10년", _SVC_KTS, lambda rows: extract_ktb_yield(rows, "10")),
    ("코스피200선물미결제약정", _SVC_FUT, lambda rows: extract_futures_oi(rows, "코스피200 선물")),
    ("코스닥150선물미결제약정", _SVC_FUT, lambda rows: extract_futures_oi(rows, "코스닥150 선물")),
]
# 무거운 지표(옵션 하루 ~19k행·timeout 90s) — 좁은 윈도우로 분리
_PUTCALL_SERIES = ("옵션풋콜비율", _SVC_OPT, extract_putcall)

_ETF_AUM_SYMBOL = "KRETF순자산총액"
_ETF_FLOW_SYMBOL = "KRETF순자금유입"

# 선물 만기물 패널 — 연속물 서빙뷰 심볼(엔진/백테스트 소비) ↔ KRX 상품명(공백 포함).
# 하루 1콜(fut_bydd_trd)이 전 상품 만기물을 담으므로 상품 추가 = 추가 콜 0(같은 응답에서 추출).
# 코스닥150선물은 상장(2015-11-23) 이후만 행 존재(상장 전일 0행 실측) — 이전 날짜는 자연 skip.
_FUT_PANEL = {"코스피200선물": "코스피200 선물", "코스닥150선물": "코스닥150 선물"}

LIGHT_SYMBOLS = [name for name, _, _ in _LIGHT_SERIES]
PUTCALL_SYMBOL = _PUTCALL_SERIES[0]
ETF_SYMBOLS = [_ETF_AUM_SYMBOL, _ETF_FLOW_SYMBOL]
ALL_SYMBOLS = LIGHT_SYMBOLS + [PUTCALL_SYMBOL] + ETF_SYMBOLS


def _weekdays(sdate: str, edate: str):
    """[sdate, edate] 평일(월~금) 'YYYYMMDD' 역순 — 휴장은 API가 []."""
    d0 = datetime.strptime(sdate, "%Y%m%d").date()
    d1 = datetime.strptime(edate, "%Y%m%d").date()
    d = d1
    while d >= d0:
        if d.weekday() < 5:
            yield d.strftime("%Y%m%d")
        d -= timedelta(days=1)


def _save_series(name: str, points: dict) -> int:
    """{날짜str: 값} → 명명 parquet(OHLCV-format·매크로 패턴)에 merge-save."""
    points = {k: v for k, v in points.items() if v is not None}
    if not points:
        return 0
    idx = pd.to_datetime(list(points.keys()), format="%Y%m%d")
    val = pd.Series(list(points.values()), index=idx, dtype="float64")
    df = pd.DataFrame({"Open": val, "High": val, "Low": val, "Close": val, "Volume": 0.0})
    df.index = df.index.tz_localize(None)
    merged = _df._merge(_df._load_existing(name), df)
    _df._save(name, merged)
    return len(points)


def fetch_market_indicators(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """가벼운 시장지표 3서비스(V-KOSPI·채권지수·국고채)를 [sdate,edate] 평일 수집·merge-save.

    하루당 서비스별 1콜(중복 제거)·추출기로 각 시리즈 값 산출. None=네트워크 실패(그날 skip).
    fetch 주입 가능(테스트). 반환 {"ok", "days", "saved": {시리즈: n}}.
    """
    if not is_active():
        return {"inactive": True}
    services = {svc for _, svc, _ in _LIGHT_SERIES}
    points = {name: {} for name, _, _ in _LIGHT_SERIES}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        rows_by_svc = {}
        day_failed = False
        for svc in services:
            rows = fetch(svc, bd)
            if rows is None:
                day_failed = True
                break
            rows_by_svc[svc] = rows
        if day_failed:
            fail += 1
            continue
        days += 1
        for name, svc, extract in _LIGHT_SERIES:
            v = extract(rows_by_svc[svc])
            if v is not None:
                points[name][bd] = v
    saved = {name: _save_series(name, pts) for name, pts in points.items()}
    if any(saved.values()):
        _df.mark_data_dirty()
    return {"ok": fail == 0, "days": days, "fail": fail, "saved": saved}


def fetch_putcall(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """옵션 풋콜비율(무거움·timeout 90s)을 [sdate,edate] 평일 수집·merge-save."""
    if not is_active():
        return {"inactive": True}
    name, svc, extract = _PUTCALL_SERIES
    points = {}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        rows = fetch(svc, bd, 90) if fetch is _fetch_day else fetch(svc, bd)
        if rows is None:
            fail += 1
            continue
        days += 1
        v = extract(rows)
        if v is not None:
            points[bd] = v
    n = _save_series(name, points)
    if n:
        _df.mark_data_dirty()
    return {"ok": fail == 0, "days": days, "fail": fail, "saved": {name: n}}


def fetch_etf_flow(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """ETF 총 AUM + 순자금유입을 [sdate,edate] 평일 수집·merge-save.

    flow는 전일 대비 Δ상장좌수가 필요(cross-day) → sdate 직전 평일까지 확장 fetch(경계 결손 0).
    AUM은 per-day 독립, flow는 직전 거래일과의 Δ. 결손일(fetch 실패)은 건너뛴다.
    """
    if not is_active():
        return {"inactive": True}
    # sdate 직전 평일 1일 확장(flow의 prev용)
    sd = datetime.strptime(sdate, "%Y%m%d").date()
    ext = sd
    for _ in range(5):
        ext -= timedelta(days=1)
        if ext.weekday() < 5:
            break
    rows_by_day = {}
    fail = 0
    for bd in _weekdays(ext.strftime("%Y%m%d"), edate):   # 역순
        r = fetch(_SVC_ETF, bd)
        if r is None:
            fail += 1
            continue
        rows_by_day[bd] = r
    aum_pts, flow_pts = {}, {}
    sorted_days = sorted(rows_by_day)                       # ascending
    for i, bd in enumerate(sorted_days):
        if bd < sdate:                                     # 확장일은 prev로만 사용
            continue
        a = extract_etf_aum(rows_by_day[bd])
        if a is not None:
            aum_pts[bd] = a
        if i > 0:
            f = compute_etf_flow(rows_by_day[sorted_days[i - 1]], rows_by_day[bd])
            if f is not None:
                flow_pts[bd] = f
    n_aum = _save_series(_ETF_AUM_SYMBOL, aum_pts)
    n_flow = _save_series(_ETF_FLOW_SYMBOL, flow_pts)
    if n_aum or n_flow:
        _df.mark_data_dirty()
    return {"ok": fail == 0, "days": len(aum_pts),
            "saved": {_ETF_AUM_SYMBOL: n_aum, _ETF_FLOW_SYMBOL: n_flow}}


def fetch_futures_panel(sdate: str, edate: str, symbols: list[str] | None = None,
                        fetch=_fetch_day) -> dict:
    """[sdate,edate] 평일 선물 만기물 패널 수집·merge-save + 연속물 서빙뷰 재구성.

    하루 1콜(fut_bydd_trd)이 전 상품 만기물을 담으므로 등록 상품 전부를 같은 응답에서
    추출한다(symbols 미지정 = _FUT_PANEL 전체 — 상품 수 증가에 콜 수 불변). 진실원천
    (만기물 패널)을 상품별 저장하고, 백테스트용 단일 연속물(카탈로그 기본 롤·조정)을
    data_fetcher.rebuild_futures_continuous로 파생한다.
    반환 {ok, days, fail, rows(총 패널행), cont(총 연속물행), saved: {심볼: {패널행, 연속물}}}.
    """
    if not is_active():
        return {"inactive": True}
    symbols = list(_FUT_PANEL) if symbols is None else symbols
    frames: dict[str, list] = {s: [] for s in symbols}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        rows = fetch(_SVC_FUT, bd)
        if rows is None:
            fail += 1
            continue
        days += 1
        for s in symbols:
            recs = extract_futures_panel(rows, _FUT_PANEL[s])
            if recs:                       # 상장 전/휴장 상품은 그 날 행 없음 — 자연 skip
                df = pd.DataFrame(recs)
                df.index = pd.to_datetime([bd] * len(df), format="%Y%m%d")
                frames[s].append(df)
    saved: dict[str, dict] = {}
    total_rows = total_cont = 0
    for s in symbols:
        if not frames[s]:
            continue
        n_rows = _df.merge_futures_panel(s, pd.concat(frames[s]))
        n_cont = _df.rebuild_futures_continuous(s)
        saved[s] = {"패널행": n_rows, "연속물": n_cont}
        total_rows += n_rows
        total_cont += n_cont
    if saved:
        _df.mark_data_dirty()
    return {"ok": fail == 0, "days": days, "fail": fail,
            "rows": total_rows, "cont": total_cont, "saved": saved}
