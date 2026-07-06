"""estimate.earnings_kr (KR) 피드 — FnGuide 컨센서스 추정실적(연간 Financial Highlight).

소스: FnGuide 기업정보 스냅샷 재무 API `getSnpFinancial`(wcomp SPA·JSON·키리스·무로그인).
증권사 애널리스트 추정 3개월 평균을 FnGuide가 가공해 **5년 확정 + 3년 추정(E)** 으로 연간 제공.
(옛 `comp.fnguide SVD_Main` HTML 스크래핑은 2026 wcomp SPA 이전으로 302 폐기 — 같은 데이터의
JSON 후신이 getSnpFinancial. 서버 `krdata._earnings`가 먼저 이전한 검증 파싱을 core로 통일한다.)

**왜 엔진에 두나(뿌리① 단일화):** 기존 엔진은 forward 추정치를 못 만든다 — OpenDART는 과거
공시만, `reports_kr`(네이버 애널 컨센서스)은 목표*주가*만(추정 이익 아님). FnGuide highlight가 유일한 무료
forward 이익 소스다. 이 feed로 챗(describe)·웹·엑셀이 동일 소스의 추정실적을 읽는다.

**명명 주의:** `estimate_kr`=forward **이익 추정**(FnGuide) vs `reports_kr`=목표**주가**
컨센서스(네이버 애널 리포트). 별개 데이터·상보적.

**현재 스냅샷 only:** FnGuide highlight는 *현재* 추정만 준다(과거 추정 아카이브 없음) → 종목별
parquet 1개를 덮어쓴다. as_of=fetch일. **현시점 분석(describe/screen)엔 적합, PIT 백테스트엔
부적합**(lookahead — 과거 시점의 당시 추정이 아니라 현재 추정이므로).

단위: rev·op·ni·controlling_ni = 억원 · eps = 원 · per·pbr = 배 · roe·op_margin·
net_margin = %. 실패는 빈결과(가짜 0 금지).
"""

from __future__ import annotations

import time
from datetime import date

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import read_parquet_safe, write_parquet_atomic

# FnGuide 기업정보 스냅샷 재무 API(wcomp SPA·JSON). 옛 comp.fnguide SVD_Main(HTML)은
# 302로 wcomp 루트에 리다이렉트되며 highlight_D_Y 표가 사라졌다 — getSnpFinancial이 후신.
_URL = "https://wcomp.fnguide.com/CompanyInfo/getSnpFinancial"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
       "Referer": "https://wcomp.fnguide.com/"}
_FREQ_TYPE = "Y"                # 연간(5확정+3추정) — 옛 highlight_D_Y 동일 기준
# consol_typ: 연결(C) 우선 → 연결 미작성 법인만 별도(I) 폴백(krdata 검증 로직 계승).
_CONSOL_ORDER = ("C", "I")

# getSnpFinancial data 행 NAME(strip) → 우리 컬럼. '영업이익'·'영업이익(발표기준)'은 둘 다 op:
# 종목에 따라 확정만 있는 행/추정(E)까지 담는 행이 갈려서, parse에서 **값이 더 많은 행**을 채택한다
# (발표기준이 forward를 담는 종목·순수 영업이익만 있는 종목 모두 커버). '당기순이익(지배)'는
# 정확일치로 '당기순이익(비지배)'와 구분. op_margin·net_margin은 매출액 대비 계산(아래).
_ITEM_MAP = {
    "매출액": "rev",
    "영업이익": "op", "영업이익(발표기준)": "op",
    "당기순이익": "ni",
    "당기순이익(지배)": "controlling_ni",
    "EPS": "eps",
    "PER": "per",
    "PBR": "pbr",
    "ROE": "roe",
}
METRIC_COLS = ["rev", "op", "ni", "controlling_ni", "per", "pbr", "roe", "eps",
               "op_margin", "net_margin"]


def _num(v):
    """FnGuide 값 셀(문자열/None) → float. '-'·'N/A'·'완전잠식'·빈칸은 None(가짜 0 금지)."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in ("-", "N/A", "완전잠식"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_financial(payload: dict) -> dict:
    """getSnpFinancial JSON → {years, is_estimate, metrics{col:[...]}}.

    순수 함수(네트워크 무의존) — 픽스처로 테스트 가능. dataset/header 없으면 빈 구조.
    header의 YYMM('YYYY/MM')·EP_CHK('E'=추정)로 연도축을, data 행 NAME→컬럼으로 지표축을 만든다.
    각 지표는 header의 CD(VAL1·VAL2…)로 행 값을 뽑아 연도축과 정렬(null YYMM 열은 제외).
    같은 컬럼에 매핑되는 행이 둘이면(영업이익/발표기준) 값이 더 많은 행을 채택. 마진은 매출액 대비 계산.
    """
    ds = (payload or {}).get("dataset") or {}
    header = [h for h in (ds.get("header") or []) if h.get("YYMM")]   # 유효 연도만(null YYMM 제외)
    if not header:
        return {"years": [], "is_estimate": [], "metrics": {}}
    val_keys = [h.get("CD") for h in header]
    years = [h["YYMM"] for h in header]
    is_est = [(h.get("EP_CHK") or "").strip() == "E" for h in header]
    metrics: dict[str, list] = {}
    for row in ds.get("data") or []:
        col = _ITEM_MAP.get((row.get("NAME") or "").strip())
        if not col:
            continue
        vals = [_num(row.get(k)) for k in val_keys]
        old = metrics.get(col)
        if old is None or sum(v is not None for v in vals) > sum(v is not None for v in old):
            metrics[col] = vals                      # 값이 더 많은 행 채택(확정+추정 우선)
    # 영업이익률·당기순이익률(%) — 매출액 대비 1회 계산(발표기준 op·total ni 기준·렌더마다 재계산 X).
    rev = metrics.get("rev")
    if rev:
        def _margin(num_key):
            num = metrics.get(num_key)
            if not num:
                return None
            return [round(n / d * 100, 1) if (n is not None and d) else None
                    for n, d in zip(num, rev)]
        if (m := _margin("op")):
            metrics["op_margin"] = m
        if (m := _margin("ni")):
            metrics["net_margin"] = m
    return {"years": years, "is_estimate": is_est, "metrics": metrics}


def to_frame(parsed: dict) -> pd.DataFrame:
    """parse_financial 결과 → DataFrame(index=회계연도 'YYYY/MM', 컬럼=지표 + is_estimate)."""
    years = parsed.get("years") or []
    if not years:
        return pd.DataFrame()
    data = {c: parsed["metrics"].get(c, [None] * len(years)) for c in METRIC_COLS
            if c in parsed.get("metrics", {})}
    df = pd.DataFrame(data, index=pd.Index(years, name="fiscal"))
    df["is_estimate"] = parsed.get("is_estimate") or [False] * len(years)
    return df


def fetch_one(code: str, timeout: int = 20) -> pd.DataFrame:
    """종목 1개의 추정실적 프레임 fetch(네트워크). 실패·무데이터면 빈 DataFrame.

    연결(C) 우선 조회 → data가 비면(연결 미작성 법인) 별도(I) 폴백.
    """
    import requests

    def _snp(consol: str) -> dict:
        r = requests.get(_URL, params={"cmp_cd": code, "consol_typ": consol,
                                       "freq_typ": _FREQ_TYPE}, headers=_UA, timeout=timeout)
        r.raise_for_status()
        return r.json() or {}

    try:
        payload = _snp(_CONSOL_ORDER[0])
        if not ((payload.get("dataset") or {}).get("data") or []):
            payload = _snp(_CONSOL_ORDER[1])
    except Exception:                            # noqa: BLE001 — transient(가짜 적재 금지)
        return pd.DataFrame()
    return to_frame(parse_financial(payload))


def _path(code: str):
    return default_manifest_path().parent / "estimates" / f"{code.replace('/', '_')}.parquet"


def load(code: str) -> pd.DataFrame | None:
    """저장된 추정실적 프레임(없으면 None). 손상 파일은 read_parquet_safe가 격리."""
    p = _path(code)
    return read_parquet_safe(p) if p.exists() else None


def refresh(code: str) -> pd.DataFrame:
    """fetch 후 parquet 덮어쓰기(스냅샷 1개). fetched_at 메타를 attrs로 보존. 빈결과면 미기록."""
    df = fetch_one(code)
    if df is not None and not df.empty:
        df.attrs["fetched_at"] = date.today().isoformat()
        write_parquet_atomic(df, _path(code))
    return df


def get(code: str, fresh_days: int = 7) -> pd.DataFrame | None:
    """on-demand 심볼뷰 — 신선한 저장본이 있으면 반환, 없거나 오래되면 fetch→저장→반환.

    data_engine 설계의 `symbol` 뷰("없으면 즉시 수집→적재→서빙") 패턴. describe 등 단일종목
    소비가 호출 — 본 종목만 lazy 수집(전종목 bulk cron 불필요). 추정은 분기 갱신이라 7일 신선도면
    충분. fetch 실패면 기존 저장본(있으면)으로 graceful — 외부(FnGuide) 일시 장애가 결과를 비우지
    않게(가짜 0은 여전히 금지: 저장본도 없으면 None).
    """
    p = _path(code)
    if p.exists() and (time.time() - p.stat().st_mtime) < fresh_days * 86400:
        return load(code)
    df = refresh(code)                       # fetch + 저장(빈결과면 미기록)
    if df is not None and not df.empty:
        return df
    return load(code)                        # fetch 실패 → 기존(stale) 저장본 fallback


def forward_view(df: pd.DataFrame | None, last_price: float | None = None) -> dict:
    """저장 프레임 → describe/웹용 compact forward 요약.

    최근 *확정* 연도 대비 가장 가까운 *추정* 연도의 매출/영업이익/순이익 성장률 + 추정 EPS·
    forward PER(현재가/추정EPS)·forward 연도 라벨. 데이터 없으면 빈 dict(가짜 0 금지).
    """
    if df is None or df.empty or "is_estimate" not in df.columns:
        return {}
    actual = df[~df["is_estimate"].astype(bool)]
    est = df[df["is_estimate"].astype(bool)]
    if actual.empty or est.empty:
        return {}
    base, fwd = actual.iloc[-1], est.iloc[0]      # 최근 확정 vs 가장 가까운 추정

    def _g(col):
        b, f = base.get(col), fwd.get(col)
        if b in (None, 0) or f is None or pd.isna(b) or pd.isna(f):
            return None
        return round((f - b) / abs(b) * 100, 1)

    def _v(row, col):
        v = row.get(col)
        return None if (v is None or pd.isna(v)) else float(v)

    eps_fwd = _v(fwd, "eps")
    fwd_pe = (round(last_price / eps_fwd, 2)
              if (eps_fwd not in (None, 0) and last_price) else None)
    out = {
        "fiscal_actual": str(base.name), "fiscal_forward": str(fwd.name),
        "rev_growth": _g("rev"), "op_growth": _g("op"), "ni_growth": _g("ni"),
        "eps_forward": eps_fwd, "forward_pe": fwd_pe,
        "op_margin_forward": _v(fwd, "op_margin"), "roe_forward": _v(fwd, "roe"),
    }
    return {k: v for k, v in out.items() if v is not None}


def annual_view(df: pd.DataFrame | None, max_years: int = 6) -> dict:
    """저장 프레임 → 웹/챗 다기간 미니표(최근 max_years). years·is_estimate + 핵심 손익(억원)·EPS.

    확정/추정을 한 표에 — 소비자가 is_estimate로 (E) 구분 렌더. 없는 지표 컬럼은 생략.
    """
    if df is None or df.empty:
        return {}
    d = df.tail(max_years)

    def _col(c):
        if c not in d.columns:
            return None
        return [None if pd.isna(v) else float(v) for v in d[c]]

    out = {"years": [str(i) for i in d.index],
           "is_estimate": [bool(x) for x in d["is_estimate"]] if "is_estimate" in d.columns else [],
           "rev": _col("rev"), "op": _col("op"), "ni": _col("ni"),
           "eps": _col("eps"), "op_margin": _col("op_margin")}
    return {k: v for k, v in out.items() if v is not None}


def estimate_block(code: str, last_price: float | None = None) -> dict:
    """종목 코드 → describe/웹 소비용 추정실적 블록(forward 요약 + 다기간 미니표 + 출처).

    on-demand 심볼뷰(get=load-or-fetch)로 스냅샷을 얻어 조립 — 서버 엣지(챗 context·ir
    라우터)가 공유 호출(DRY). forward 데이터 없으면 빈 dict(가짜 0 금지·미부착 신호).
    """
    df = get(code)
    fwd = forward_view(df, last_price=last_price)
    if not fwd:
        return {}
    return {"source": "FnGuide 컨센서스", "forward": fwd, "annual": annual_view(df)}
