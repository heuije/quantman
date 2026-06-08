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
        common = r[r["se"] == "보통주"]
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


def _report(code: str, year: int, reprt_code: str):
    """CFS(연결) 우선, 없으면 OFS(별도) — 백업 소스가 아니라 필러별 명세 차이."""
    dart = _client()
    for div in ("CFS", "OFS"):
        try:
            fs = dart.finstate_all(code, str(year), reprt_code=reprt_code, fs_div=div)
        except Exception:
            fs = None
        if fs is not None and len(fs) > 0 and "rcept_no" in fs.columns:
            return fs
    return None


def fetch_one(code: str, years: list[int]) -> "object":
    """KR 종목의 분기 펀더멘털(years) → fund_df(as_of 인덱스). 종목당 연 4콜."""
    import pandas as pd

    periods: list[dict] = []
    for y in years:
        raw: dict = {}                           # q -> {field: amount, "_rcept": date}
        for q, rc, _ in _QUARTERS:
            fs = _report(code, y, rc)
            if fs is None:
                continue
            vals = {f: _pick(fs, d[0], d[1]) for f, d in _FIELD_DEFS.items()}
            vals["td"] = _pick_sum(fs, _DEBT_IDS, _DEBT_NMS)   # 총차입금(다계정 합산)
            rcept = str(fs["rcept_no"].iloc[0])
            vals["_rcept"] = f"{rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}" if len(rcept) >= 8 else None
            raw[q] = vals

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
    share_hist = _shares_history(code, years)
    for p in periods:
        p["shares"] = _shares_asof(share_hist, p["as_of"])
    periods.sort(key=lambda p: p["end"])
    if not periods:
        return pd.DataFrame()
    return compute_fundamentals(periods, quarterly=True)


def _fund_path(code: str):
    return default_manifest_path().parent / "fundamentals" / f"{code.replace('/', '_')}.parquet"


def fetch(codes: list[str], years: list[int], budget_calls: int = 9000,
          fresh_days: int = 80) -> dict:
    """KR 분기 펀더멘털 증분 수급 — 미수집·오래된 것 우선, 일일 콜 예산 내(10k/일 준수).

    parquet mtime을 신선도 신호로 — fresh_days 이내 수집분은 skip. 종목당 4×len(years) 콜.
    여러 날 cron이 점진적으로 전 종목·연도를 채운다. 한번 모은 history는 정적이라 전 사용자 공유.
    """
    import time
    now = time.time()

    def _mtime(c: str) -> float:
        p = _fund_path(c)
        return p.stat().st_mtime if p.exists() else 0.0

    calls = n_ok = n_fail = 0
    for c in sorted(codes, key=_mtime):          # 미수집(0)·오래된 것 우선
        p = _fund_path(c)
        if p.exists() and (now - p.stat().st_mtime) < fresh_days * 86400:
            continue                              # 최근 수집 — skip
        try:
            df = fetch_one(c, years)
        except Exception:
            n_fail += 1
        else:
            if df is not None and not df.empty:
                write_parquet_atomic(df, p)       # 원자적 — 중단 시 잘린 파일 안 남김
                n_ok += 1
        calls += 5 * len(years)                  # 재무 4분기 + 주식총수 1(연간)
        if calls >= budget_calls:
            break
    return {"ok": n_ok, "fail": n_fail, "calls": calls}
