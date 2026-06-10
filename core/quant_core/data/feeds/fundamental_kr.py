"""fundamental.equity (KR) 피드 — OpenDART 분기 (filing-date PIT).

종목당 연 4개 보고서(1분기 11013·반기 11012·3분기 11014·사업보고서 11011)에서 분기 raw 재무를
정규화 → compute_fundamentals(quarterly=True). as_of=각 보고서 접수일(rcept_no 앞8자리)=진짜 PIT.

OpenDART 분기보고서 구조(실측):
  - 손익(IS): thstrm_amount = **당분기 3개월** standalone (Q1·Q2·Q3). 사업보고서는 연간(12M).
    → Q4 = 연간 − (Q1+Q2+Q3).
  - 현금흐름(CF): thstrm_amount = **누적(YTD)**. → 분기값 = 당기 YTD − 직전 YTD.
  - 재무상태(BS): thstrm_amount = 분기말 시점값(level).
표준 account_id(ifrs-full_*) 우선, 영업이익 등 비표준은 account_nm 폴백. 발행주식수는 FDR 'Stocks'.

무료 10k콜/일 → 4300종목×4분기=17.2k콜/년치(백필 ~1.7일/년치), 신규분기 4.3k콜(여유). 한번 수집하면
정적 history로 모든 사용자에게 제공. 키는 OPENDART_API_KEY. 저장: FUNDAMENTALS_DIR/{code}.parquet.
"""

from __future__ import annotations

import os

import requests

from ..manifest import default_manifest_path
from .fundamentals_common import compute_fundamentals
from ...parquet_io import write_parquet_atomic

# field -> (account_id 별칭, account_nm 폴백, kind). kind: is(손익·3M)·cf(현금흐름·YTD)·bs(시점).
_FIELD_DEFS = {
    "rev":   (["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익"], "is"),
    "gp":    (["ifrs-full_GrossProfit"], ["매출총이익"], "is"),
    "ebit":  (["dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"],
              ["영업이익", "영업이익(손실)"], "is"),
    "ni":    (["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"], "is"),
    "ocf":   (["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름"], "cf"),
    "capex": (["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
              ["유형자산의 취득", "유형자산의취득"], "cf"),
    "ta":    (["ifrs-full_Assets"], ["자산총계"], "bs"),
    "tl":    (["ifrs-full_Liabilities"], ["부채총계"], "bs"),
    "ca":    (["ifrs-full_CurrentAssets"], ["유동자산"], "bs"),
    "cl":    (["ifrs-full_CurrentLiabilities"], ["유동부채"], "bs"),
    "cash":  (["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"], "bs"),
    "re":    (["ifrs-full_RetainedEarnings"], ["이익잉여금"], "bs"),
    "eq":    (["ifrs-full_Equity"], ["자본총계"], "bs"),
}
_IS = [f for f, d in _FIELD_DEFS.items() if d[2] == "is"]
_CF = [f for f, d in _FIELD_DEFS.items() if d[2] == "cf"]
_BS = [f for f, d in _FIELD_DEFS.items() if d[2] == "bs"]
# (분기태그, reprt_code, 분기말 MM-DD). Q4는 사업보고서(연간) — IS는 연간−누적, CF는 연간YTD−3Q YTD.
_QUARTERS = [("Q1", "11013", "03-31"), ("Q2", "11012", "06-30"),
             ("Q3", "11014", "09-30"), ("Q4", "11011", "12-31")]

_dart = None


def _client():
    global _dart
    if _dart is None:
        import OpenDartReader
        key = os.environ.get("OPENDART_API_KEY")
        if not key:
            raise RuntimeError("OPENDART_API_KEY 환경변수 필요")
        _dart = OpenDartReader(key)
    return _dart


def _is_common_share(se) -> bool:
    """주식총수 보고서의 보통주(의결권 있는) 행 판별 — 필러별 'se' 표기가 제각각이라 robust 매칭.

    실측 표기(같은 '보통주'인데 라벨이 다 다름): '보통주' · '의결권 있는 주식\\n(보통주)' ·
    '의결권있는 주식' · '의결권 있는\\n보통주' · '의결권이 있는주식(보통주)'. 우선주(의결권 없는)·
    합계·비고는 제외. `se=='보통주'` 정확매칭은 LG전자·SK·신한지주·셀트리온·삼성전기 등 다수를 놓쳐
    shares=null→pb 미산출의 근본원인이었다(공백·개행 제거 후 '보통주' 또는 '의결권있는' 포함 판정)."""
    s = str(se).replace("\n", "").replace(" ", "")
    if "합계" in s or "비고" in s:
        return False
    return "보통주" in s or "의결권있는" in s   # 의결권 있는 = 보통주(우선주는 '의결권 없는')


def _shares_history(code: str, years: list[int]) -> list:
    """OpenDART 주식총수(사업보고서·연간)의 보통주 발행주식총수 시계열 → [(as_of, shares), ...] 정렬.

    연 1콜/종목. as_of=접수일(rcept). _shares_asof가 PIT 조회(Q1~Q3엔 직전 연도분이 적용돼 미래참조 0).
    FDR 'Stocks'(현행 스냅샷) 대신 — 소스 단일화(OpenDART) + 진짜 PIT(증자·감자 반영).
    """
    dart = _client()
    hist: list = []
    for y in years:
        try:
            r = dart.report(code, "주식총수", str(y), "11011")
        except Exception:
            continue
        if r is None or len(r) == 0 or "se" not in r.columns:
            continue
        common = r[r["se"].apply(_is_common_share)]
        if not len(common):
            continue
        sh = _amt(common.iloc[0].get("istc_totqy"))
        rc = str(r["rcept_no"].iloc[0]) if "rcept_no" in r.columns else ""
        if sh and len(rc) >= 8:
            hist.append((f"{rc[:4]}-{rc[4:6]}-{rc[6:8]}", sh))
    hist.sort()
    return hist


def _shares_asof(hist: list, as_of: str) -> float:
    """as_of 시점에 알려진 최신 발행주식총수(PIT). 없으면 NaN."""
    val = float("nan")
    for d, v in hist:
        if d <= as_of:
            val = v
        else:
            break
    return val


def _amt(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick(fs, ids: list[str], nms: list[str]):
    for _, r in fs.iterrows():                   # BS→IS→CIS→CF 순 → 첫 매칭이 본문
        if str(r.get("account_id")) in ids or str(r.get("account_nm")) in nms:
            return _amt(r.get("thstrm_amount"))
    return None


# 총차입금(이자부부채) — OpenDART에 단일 '총차입금' 표준계정이 없어 표준 차입금 라인을 합산한다.
# account_nm 우선(KR filer는 account_id가 비표준인 경우 많음). 비차입금 부채(매입채무·미지급금 등)는
# 제외 → EV·net_debt는 *이자부부채* 기준(총부채 tl과 구분). 빠진 계정은 보수적 과소추정(정직).
_DEBT_IDS = ["ifrs-full_ShorttermBorrowings", "ifrs-full_LongtermBorrowings",
             "ifrs-full_BondsIssued", "ifrs-full_CurrentPortionOfLongtermBorrowings"]
_DEBT_NMS = ["단기차입금", "유동성장기부채", "유동성장기차입금", "장기차입금", "사채", "유동성사채"]


def _pick_sum(fs, ids: list[str], nms: list[str]):
    """매칭되는 *모든* 행의 thstrm_amount 합 — 단일 표준계정이 없는 총차입금 등 다계정 항목용.
    하나도 없으면 None(0으로 가장하지 않음 — 정직)."""
    total = None
    for _, r in fs.iterrows():
        if str(r.get("account_id")) in ids or str(r.get("account_nm")) in nms:
            v = _amt(r.get("thstrm_amount"))
            if v is not None:
                total = (total or 0.0) + v
    return total


_FINSTATE_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"


def _report(code: str, year: int, reprt_code: str):
    """CFS(연결) 우선, 없으면 OFS(별도). 반환 (fs|None, rate_limited).

    OpenDartReader.finstate_all은 013(데이터없음)·020(요청제한)을 **둘 다 빈 df로 뭉개** status를
    숨긴다 → 한도초과를 무데이터로 오인해 false 빈결과 마커를 찍는 사고의 근본원인. 그래서 직접
    호출해 status를 본다: 020(요청제한)·HTTP/파싱 오류 = rate_limited True(호출자가 마커 금지+청크
    중단), 013·정상-무데이터 = (None, False). 정상 데이터 = (df, False).
    """
    import pandas as pd
    dart = _client()
    corp = dart.find_corp_code(code)             # 6자리→corp_code(라이브러리와 동일 캐시 해석)
    if not corp:
        return None, False
    key = os.environ.get("OPENDART_API_KEY")
    for div in ("CFS", "OFS"):                   # 필러별 명세 차이(백업 소스 아님)
        try:
            r = requests.get(_FINSTATE_URL, params={
                "crtfc_key": key, "corp_code": corp, "bsns_year": str(year),
                "reprt_code": reprt_code, "fs_div": div}, timeout=20)
            jo = r.json()
        except Exception:
            return None, True                    # 네트워크/파싱 오류 = transient(마커 금지)
        status = jo.get("status")
        if status == "020":                      # 요청 제한 초과 — transient
            return None, True
        if status == "000" and jo.get("list"):
            return pd.DataFrame(jo["list"]), False
        # 013(데이터없음)·list 없음 → 이 div 없음, 다음 div 시도
    return None, False                           # 양 div 다 무데이터(genuine 013)


def fetch_one(code: str, years: list[int]) -> tuple:
    """KR 종목의 분기 펀더멘털(years) → (fund_df, rate_limited). 종목당 연 ~5콜.

    rate_limited=True면 OpenDART 요청제한(020)·네트워크 오류를 만난 것 — 호출자(fetch)가 마커를
    안 찍고 청크를 중단해 false 빈결과를 방지한다(데이터 부재와 한도초과를 구분)."""
    import pandas as pd

    periods: list[dict] = []
    rate_limited = False
    for y in years:
        if rate_limited:
            break
        raw: dict = {}                           # q -> {field: amount, "_rcept": date}
        for q, rc, _ in _QUARTERS:
            fs, rl = _report(code, y, rc)
            if rl:
                rate_limited = True
                break                            # 한도 — 더 안 긁고 호출자에 신호
            if fs is None:
                continue
            vals = {f: _pick(fs, d[0], d[1]) for f, d in _FIELD_DEFS.items()}
            vals["td"] = _pick_sum(fs, _DEBT_IDS, _DEBT_NMS)   # 총차입금(다계정 합산)
            rcept = str(fs["rcept_no"].iloc[0])
            vals["_rcept"] = f"{rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}" if len(rcept) >= 8 else None
            raw[q] = vals
        if rate_limited:
            break

        is_sum = {f: 0.0 for f in _IS}           # Q1~Q3 IS 누적(Q4 도출용)
        is_have = {f: True for f in _IS}
        prev_ytd = {f: None for f in _CF}        # CF 직전 누적
        for q, _, mmdd in _QUARTERS:
            v = raw.get(q)
            if v is None or v.get("_rcept") is None:
                is_have = {f: False for f in _IS}   # 분기 결손 → Q4 IS 도출 불가
                prev_ytd = {f: None for f in _CF}   # CF 차분 체인 끊김
                continue
            row: dict = {"end": f"{y}-{mmdd}", "as_of": v["_rcept"], "ebitda": None, "td": None}
            # IS: Q1~Q3 = 3개월 thstrm; Q4 = 연간 − 누적(Q1~Q3)
            for f in _IS:
                if q != "Q4":
                    row[f] = v[f]
                    if v[f] is not None:
                        is_sum[f] += v[f]
                    else:
                        is_have[f] = False
                else:
                    row[f] = (v[f] - is_sum[f]) if (v[f] is not None and is_have[f]) else None
            # CF: 분기 = 당기 YTD − 직전 YTD (Q1 직전=0)
            for f in _CF:
                ytd = v[f]
                base = prev_ytd[f] if prev_ytd[f] is not None else 0.0
                row[f] = (ytd - base) if ytd is not None else None
                if ytd is not None:
                    prev_ytd[f] = ytd
            # BS: 분기말 시점값
            for f in _BS:
                row[f] = v[f]
            row["td"] = v.get("td")          # 총차입금(다계정 합산) — _FIELD_DEFS 단일픽 밖
            ocf, capex = row.pop("ocf"), row.pop("capex")
            row["fcf"] = (ocf - capex) if (ocf is not None and capex is not None) else None
            periods.append(row)

    # PIT 발행주식수(OpenDART 주식총수) 주입 — 각 분기 as_of 시점에 알려진 최신 보통주 발행총수.
    if not rate_limited:
        share_hist = _shares_history(code, years)
        for p in periods:
            p["shares"] = _shares_asof(share_hist, p["as_of"])
    periods.sort(key=lambda p: p["end"])
    if rate_limited or not periods:
        return pd.DataFrame(), rate_limited
    return compute_fundamentals(periods, quarterly=True), False


def _fund_path(code: str):
    return default_manifest_path().parent / "fundamentals" / f"{code.replace('/', '_')}.parquet"


def _marker_path(code: str):
    """빈결과(OpenDART에 추출할 데이터 없음) 시도 마커 — parquet 옆 `.empty`.

    금융지주·상폐 등은 표준 계정과 안 맞아 매번 빈 결과를 낸다. parquet을 안 쓰면 mtime이
    0으로 남아 **매일 맨 앞으로 재정렬돼 재시도** → 일일 콜 예산을 영구히 갉아먹는다. 시도했음을
    마커 mtime으로 남겨 fresh_days 동안 skip한다(reader는 `{code}.parquet`만 읽으므로 마커 무영향).
    """
    return _fund_path(code).with_suffix(".empty")


def _write_marker(code: str) -> None:
    m = _marker_path(code)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("")                              # 내용 불필요 — mtime만 신선도 신호로 사용


def _clear_marker(code: str) -> None:
    """빈결과였다가 데이터가 생기면(성공 적재 시) 스테일 마커 제거."""
    m = _marker_path(code)
    if m.exists():
        m.unlink()


def fetch(codes: list[str], years: list[int], budget_calls: int = 9000,
          fresh_days: int = 80) -> dict:
    """KR 분기 펀더멘털 증분 수급 — 미수집·오래된 것 우선, 일일 콜 예산 내(10k/일 준수).

    성공 parquet 또는 빈결과 마커 중 최신 시도 mtime을 신선도 신호로 — fresh_days 이내 시도분은
    skip. 종목당 4×len(years)+1 콜. 여러 날 cron이 점진적으로 전 종목·연도를 채운다.
    빈결과(데이터 없음)는 `.empty` 마커를 남겨 매일 재시도로 예산 낭비하지 않게 한다(_marker_path).
    """
    import time
    now = time.time()

    def _attempted_mtime(c: str) -> float:
        """직전 시도 시각 — 성공 parquet 또는 빈결과 마커 중 최신(없으면 0)."""
        ts = 0.0
        for p in (_fund_path(c), _marker_path(c)):
            if p.exists():
                ts = max(ts, p.stat().st_mtime)
        return ts

    calls = n_ok = n_fail = n_empty = n_rl = 0
    for c in sorted(codes, key=_attempted_mtime):     # 미시도(0)·오래된 시도 우선
        at = _attempted_mtime(c)
        if at and (now - at) < fresh_days * 86400:
            continue                                   # 최근 시도(성공·빈결과 무관) — skip
        try:
            df, rate_limited = fetch_one(c, years)
        except Exception:
            n_fail += 1                                # 예외도 마커 안 찍음(재시도 유지)
            calls += 5 * len(years)
            if calls >= budget_calls:
                break
            continue
        if rate_limited:
            # OpenDART 요청제한(020)·네트워크 오류 — false 빈결과 방지: 마커 금지 + 청크 중단.
            # 이 종목은 미시도로 남아 다음 청크 재시도(한도 후 망치질도 방지).
            n_rl += 1
            break
        if df is not None and not df.empty:
            write_parquet_atomic(df, _fund_path(c))   # 원자적 — 중단 시 잘린 파일 안 남김
            _clear_marker(c)                          # 빈결과→데이터 생김 시 마커 정리
            n_ok += 1
        else:
            _write_marker(c)                          # 확정 무데이터(013) — genuine 마커
            n_empty += 1
        calls += 5 * len(years)                  # 재무 4분기 + 주식총수 1(연간)
        if calls >= budget_calls:
            break
    return {"ok": n_ok, "fail": n_fail, "empty": n_empty, "rate_limited": n_rl, "calls": calls}


def coverage(codes: list[str]) -> dict:
    """백필 진척 스냅샷 — 파일시스템 스캔만(데이터 미로드, 경량). /admin/fundamental-coverage 소비.

    have_fundamentals=성공 parquet 수, empty_marked=빈결과 마커 수, written_last_24h=최근 24h
    적재(직전 크론 순증분 근사), newest/oldest_mtime=적재 신선도(epoch). 데이터를 로드하지 않아
    수천 종목도 ~1초.
    """
    import time
    now = time.time()
    have = empty = recent = 0
    newest = oldest = 0.0
    for c in codes:
        p = _fund_path(c)
        if p.exists():
            have += 1
            mt = p.stat().st_mtime
            newest = max(newest, mt)
            oldest = mt if oldest == 0.0 else min(oldest, mt)
            if now - mt < 86400:
                recent += 1
        elif _marker_path(c).exists():
            empty += 1
    return {"have_fundamentals": have, "empty_marked": empty,
            "written_last_24h": recent,
            "newest_mtime": newest or None, "oldest_mtime": oldest or None}


def probe(codes: list[str]) -> list[dict]:
    """종목별 백필 상태 진단 — parquet/marker 존재 + **이 프로세스의** find_corp_code 결과.

    false 빈결과의 원인 분리용: corp_code가 None이면 _report가 즉시 (None,False)로 빈결과 마킹
    (line 156). 배포 컨테이너에서 OpenDartReader corp 캐시(CORPCODE) 해석이 깨졌는지를 데이터
    미로드로 드러낸다. 데이터는 안 받고 corp 해석만 — quota 무영향(CORPCODE는 1회 캐시).

    parquet은 있는데 pb가 안 뜨는 부류 진단을 위해, parquet 존재 시 내용도 peek한다(최신 행 전
    컬럼값 — eq·shares 등 pb 입력 중 무엇이 null인지)."""
    import pandas as pd
    dart = _client()
    out: list[dict] = []
    for c in codes:
        try:
            corp = dart.find_corp_code(c)
        except Exception as e:                    # 진단 — 다운로드/파싱 실패를 삼키지 말고 표면화
            corp = f"ERROR:{type(e).__name__}"
        rec = {"code": c, "parquet": _fund_path(c).exists(),
               "marker": _marker_path(c).exists(), "corp_code": corp or None}
        if rec["parquet"]:
            try:
                df = pd.read_parquet(_fund_path(c))
                rec["content"] = {"rows": int(len(df)),
                                  "last_date": str(df.index[-1])[:10] if len(df) else None,
                                  "last_row": {k: (None if pd.isna(v) else
                                                   (round(float(v), 4) if isinstance(v, (int, float))
                                                    else str(v)))
                                               for k, v in df.iloc[-1].items()} if len(df) else None}
            except Exception as e:
                rec["content"] = {"error": f"{type(e).__name__}:{e}"}
        out.append(rec)
    return out


def clear_markers(codes: list[str] | None = None) -> int:
    """빈결과(.empty) 마커 제거 — false 마커(한도초과를 무데이터로 오인) 회복용.

    codes=None이면 fundamentals 디렉터리의 모든 .empty 제거(다음 청크가 재수집·재분류).
    genuine 무데이터(ETF 등)는 재수집 시 013으로 다시 정상 마킹된다. 반환=제거 개수."""
    if codes is None:
        d = _fund_path("_").parent
        markers = list(d.glob("*.empty")) if d.exists() else []
    else:
        markers = [_marker_path(c) for c in codes]
    removed = 0
    for m in markers:
        if m.exists():
            m.unlink()
            removed += 1
    return removed


def delete_shares_null(codes: list[str]) -> dict:
    """일회성 마이그레이션 — shares_outstanding 마지막 값이 null인 parquet 삭제.

    보통주 라벨 정확매칭 버그(_is_common_share 도입 전)로 주식수가 누락된 parquet들이 'fresh'(최근
    mtime)라 백필이 skip한다 → 삭제해 미수집 상태로 만들어 수정된 _shares_history로 재수집되게 한다.
    shares 정상 parquet은 보존(재수집 중 데이터 손실 방지). 반환={'deleted','kept','no_col'}."""
    import pandas as pd
    deleted = kept = no_col = 0
    for c in codes:
        p = _fund_path(c)
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p, columns=["shares_outstanding"])
        except Exception:
            no_col += 1                              # 컬럼 없음 등 — 보수적으로 보존
            continue
        if len(df) and pd.isna(df["shares_outstanding"].iloc[-1]):
            p.unlink()
            deleted += 1
        else:
            kept += 1
    return {"deleted": deleted, "kept": kept, "no_col": no_col}
