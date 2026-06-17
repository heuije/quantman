"""estimate.consensus (KR) 피드 — 한경컨센서스 개별 애널 리포트 스트림.

list HTML(서버렌더 단일 테이블, 무로그인·무키)의 컬럼:
  작성일 · 제목(`종목명(종목코드)` 포함) · 적정가격 · 투자의견 · 작성자 · 제공출처
각 행 링크 `…/analysis/downpdf?report_idx=N` 의 report_idx = 리포트 고유키(dedup).

**적재 모델(설계 확정):** 원시 리포트 전건 영구보관(report_idx dedup·덮어쓰기 없음) →
증권사별 최신 standing(신선도窓 180일 내 1표) 횡단집계로 일별 컨센서스 패널 산출.
새 리포트는 *그 증권사 슬롯만* 갱신 — 다른 증권사 standing은 유지. as_of=작성일=PIT.

크롤 축 = **날짜 윈도우**(한 페이지=여러 종목), 저장 축 = **종목별 parquet**:
  {code}_raw.parquet  원시 전건 아카이브(재파생·360 리포트 facet 소비)
  {code}.parquet       컨센서스 변경점 패널(indicators.add_consensus가 ffill 소비)

재배포는 개인이용 전제(사용자 확정). 전 증권사 아님(대표 표본)·소형주 sparse→해당일 NaN.
"""

from __future__ import annotations

import re

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import write_parquet_atomic

_LIST_URL = "https://consensus.hankyung.com/analysis/list"
_IDX_RE = re.compile(r"report_idx=(\d+)")
_CODE_RE = re.compile(r"\((\d{6})\)")
_WINDOW = pd.Timedelta(days=180)        # standing 신선도窓 — 초과 리포트는 active에서 제외

# 컨센서스 패널 컬럼 — estimate.consensus.provides 와 일치(spec.py 계약).
PANEL_COLS = ["consensus_target", "consensus_target_median", "analyst_count",
              "consensus_opinion", "target_dispersion", "target_revision_pct",
              "days_since_report"]


def _opinion_score(text: str):
    """투자의견 문자열 → +1(매수)/0(중립)/−1(매도). 영·한 혼재 robust. 미상 None.

    한경 CO 리포트 실측: Buy·Hold·Sell·Strong Buy·Trading Buy·Outperform·Marketperform·
    Neutral·Reduce·매수·중립·매도·비중확대/축소 등. 매도계열을 먼저 본다(부분문자열 충돌 회피).
    """
    s = (text or "").strip().lower()
    if not s:
        return None
    if any(k in s for k in ("sell", "under", "reduce", "매도", "축소", "비중축소")):
        return -1
    if any(k in s for k in ("buy", "outperform", "overweight", "add", "accumulate",
                            "매수", "확대", "비중확대")):
        return 1
    if any(k in s for k in ("hold", "neutral", "market", "중립", "유지")):
        return 0
    return None                          # 미상 의견 — 가짜 0 금지(정직)


def _to_int(text: str):
    """적정가격 셀('38,000' / '' / '-') → int 또는 None."""
    s = (text or "").replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_list_page(html: str) -> list[dict]:
    """list HTML 한 페이지 → [{report_idx, as_of, code, broker, target, opinion, title}].

    종목코드 없는 행(산업·전략 리포트)·목표가 없는 행은 컨센 대상이 아니라 skip.
    같은 report_idx가 행 내 복수 링크(제목+아이콘)로 나와도 1건으로 dedup.
    """
    from bs4 import BeautifulSoup              # html.parser 백엔드 — lxml 비의존

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[int] = set()
    for tr in soup.find_all("tr"):
        links = tr.find_all("a", href=_IDX_RE)
        if not links:
            continue
        m = _IDX_RE.search(links[0].get("href", ""))
        if not m:
            continue
        report_idx = int(m.group(1))
        if report_idx in seen:
            continue
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        title = max((a.get_text(" ", strip=True) for a in links), key=len)   # 텍스트 많은 = 제목 링크
        cm = _CODE_RE.search(title)
        if not cm:
            continue                          # 종목코드 없음 → 산업/전략 리포트
        target = _to_int(tds[2].get_text(strip=True))
        if target is None:
            continue                          # 목표가 없는 리포트 → 컨센 대상 아님
        seen.add(report_idx)
        out.append({
            "report_idx": report_idx,
            "as_of": tds[0].get_text(strip=True),
            "code": cm.group(1),
            "broker": tds[5].get_text(strip=True),
            "target": float(target),
            "opinion": _opinion_score(tds[3].get_text(strip=True)),
            "title": title,
        })
    return out


def collect_range(sdate: str, edate: str, max_pages: int = 300) -> tuple[list[dict], bool]:
    """[sdate, edate] 기간의 기업 리포트(report_type=CO) 전건을 페이지네이션 수집.

    반환 (reports, ok). ok=False면 네트워크/HTTP 오류(호출자가 마커 금지·재시도). 날짜형식 YYYY-MM-DD.
    한 페이지=80건(pagenum). 빈 페이지를 만나면 종료. 전 종목 혼재(저장 시 종목별로 분배).
    """
    import requests

    reports: list[dict] = []
    for now_page in range(1, max_pages + 1):
        try:
            r = requests.get(_LIST_URL, params={
                "sdate": sdate, "edate": edate, "report_type": "CO",
                "pagenum": "80", "now_page": str(now_page),
            }, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
        except Exception:                     # noqa: BLE001 — 네트워크/HTTP 오류=transient(마커 금지)
            return reports, False
        page = _parse_list_page(r.text)
        if not page:
            break                             # 빈 페이지 = 기간 끝
        reports.extend(page)
    return reports, True


def _raw_path(code: str):
    return default_manifest_path().parent / "consensus" / f"{code.replace('/', '_')}_raw.parquet"


def _panel_path(code: str):
    return default_manifest_path().parent / "consensus" / f"{code.replace('/', '_')}.parquet"


def _build_panel(raw: pd.DataFrame) -> pd.DataFrame:
    """원시 리포트 전건 → 증권사별 최신 standing(180일窓) 횡단집계 변경점 패널.

    값이 변하는 날(=리포트 발표일 ∪ 발표일+180일 만료일)에만 행을 둔다(이후 indicators가 ffill).
    각 변경일 D: active=발표일∈(D−180, D]인 리포트 → 증권사별 최신 1건 → 평균/중앙값/count/의견/표준편차.
    한 증권사 새 리포트는 그 증권사 최신만 교체(다른 증권사 standing 유지=덮어쓰기 없음). PIT(as_of≤D만 사용).
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=PANEL_COLS)
    df = raw.dropna(subset=["target"]).copy()
    if df.empty:
        return pd.DataFrame(columns=PANEL_COLS)
    df["as_of"] = pd.to_datetime(df["as_of"])

    asof = df["as_of"]
    change_dates = pd.DatetimeIndex(sorted(set(asof) | set(asof + _WINDOW)))
    rows: list[dict] = []
    for D in change_dates:
        active = df[(df["as_of"] <= D) & (D - df["as_of"] < _WINDOW)]
        if active.empty:                      # 전 증권사 만료 → 커버리지 소멸(NaN, ffill로 스테일 제거)
            rows.append({"as_of": D, "consensus_target": float("nan"),
                         "consensus_target_median": float("nan"), "analyst_count": 0,
                         "consensus_opinion": float("nan"), "target_dispersion": float("nan"),
                         "days_since_report": float("nan")})
            continue
        # 증권사별 최신 1건(동일 발표일이면 report_idx 큰 것 = 나중 발표)
        latest = active.sort_values(["as_of", "report_idx"]).groupby("broker", as_index=False).tail(1)
        tg = latest["target"].astype(float)
        op = latest["opinion"].dropna()
        rows.append({
            "as_of": D,
            "consensus_target": float(tg.mean()),
            "consensus_target_median": float(tg.median()),
            "analyst_count": int(latest["broker"].nunique()),
            "consensus_opinion": (float(op.mean()) if len(op) else float("nan")),
            "target_dispersion": (float(tg.std(ddof=0)) if len(tg) > 1 else float("nan")),
            "days_since_report": float((D - latest["as_of"].max()).days),
        })
    panel = pd.DataFrame(rows).set_index("as_of").sort_index()
    # fill_method=None: 커버리지 공백(NaN)을 pad로 메우지 않음 — 공백 건너뛴 가짜 리비전 방지.
    panel["target_revision_pct"] = panel["consensus_target"].pct_change(fill_method=None) * 100.0
    return panel[PANEL_COLS]


def ingest(reports: list[dict]) -> dict:
    """수집 리포트를 종목별 원시 아카이브에 append(report_idx dedup·덮어쓰기 없음) + 패널 재산출.

    반환 통계(종목수·신규행수). 원시는 전건 보관(append-only), 패널은 매번 원시에서 재산출(멱등).
    """
    if not reports:
        return {"codes": 0, "new_rows": 0}
    by_code: dict[str, list[dict]] = {}
    for r in reports:
        by_code.setdefault(r["code"], []).append(r)

    n_codes = n_new = 0
    for code, recs in by_code.items():
        new = pd.DataFrame(recs)
        rp = _raw_path(code)
        if rp.exists():
            old = pd.read_parquet(rp)
            before = len(old)
            merged = pd.concat([old, new], ignore_index=True)
        else:
            before = 0
            merged = new
        merged = merged.drop_duplicates(subset=["report_idx"]).sort_values("as_of")
        write_parquet_atomic(merged, rp)
        panel = _build_panel(merged)
        if not panel.empty:
            write_parquet_atomic(panel, _panel_path(code))
        n_codes += 1
        n_new += max(0, len(merged) - before)
    return {"codes": n_codes, "new_rows": n_new}
