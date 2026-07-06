"""reports.kr 피드 — 네이버 금융 리서치 개별 애널 리포트 스트림 (한국 애널 리포트 *유일* 소스).

HOME 리포트 목록 **AND** 컨센서스 지표(목표가·투자의견)를 모두 이 피드가 공급한다 —
옛 한경 `consensus_kr`은 은퇴했다(2026-07-06). 은퇴 이유(실측): 한경은 목표가가 목록에 인라인이라
값싸게 깊은 이력을 주지만 "대표 표본"이라 커버리지가 절반뿐(삼성/미래에셋/신한/하나/키움 등 대형
증권사 누락)·이력 2015까지. 네이버는 커버리지 2배·목표가/투자의견도 상세페이지서 정확 추출·이력
2007까지 — 전 축에서 우위. 유일 대가는 목표가가 목록에 없어 상세 fetch가 필요(그래서 백필 분산).

크로스종목 목록 HTML(finance.naver.com/research/company_list.naver·무로그인·무키)의 컬럼:
  종목명 · 제목 · 증권사 · (PDF첨부) · 작성일(YY.MM.DD) · 조회수
각 행 링크 `company_read.naver?nid=N` 의 nid = 리포트 고유키(dedup). PDF는 행에 직접 링크.

**목표가·투자의견:** 크로스종목 목록엔 없고 리포트 상세 페이지에만 있다(헤더 `.money` 구조필드 =
목표가, 투자의견 텍스트 — 실측 .money와 정규식 100% 일치). `enrich()`가 상세를 종목당 fetch해 붙인다.
상세 fetch는 종목당 크롤이라 비용이 커, 일일 증분(소량)·**chunked 백필(여러 날 분산·재개)**에서만 호출.

**종목코드:** 목록엔 종목명만 있다. ticker_db(FDR KRX 공식명)로 종목명→코드 해결 — 실측 100% 매칭.
미해결(신규상장·개명 등)은 skip → 서버가 라이브로 폴백.

크롤 축 = 페이지(newest-first, 30/page), 저장 축 = 종목별 parquet(2 파생뷰):
  reports/{code}.parquet     원시 리포트 아카이브(nid dedup·append-only·nid·as_of·code·broker·title·url·target·opinion)
  consensus/{code}.parquet   컨센서스 지표 패널(증권사별 standing 180일窓 집계 — load_stock_consensus 소비)

재배포는 개인이용 전제(무로그인 공개 목록).
"""

from __future__ import annotations

import re

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import write_parquet_atomic
from ...ticker_db import load_db          # 모듈 레벨 노출(테스트 monkeypatch 대상)

_LIST_URL = "https://finance.naver.com/research/company_list.naver"
_READ_BASE = "https://finance.naver.com/research/"
_NID_RE = re.compile(r"nid=(\d+)")
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _name_to_code() -> dict:
    """ticker_db → {한국어 종목명: 6자리 코드}. KR(.KS) 항목만."""
    m: dict[str, str] = {}
    for e in load_db():
        t = e.get("t", "")
        k = (e.get("k") or "").strip()
        if t.endswith(".KS") and k:
            code = t[:-3]
            if len(code) == 6 and code.isdigit():
                m.setdefault(k, code)
    return m


def _iso(d: str):
    """네이버 작성일 'YY.MM.DD' → ISO 'YYYY-MM-DD'. 형식 이상은 None."""
    parts = d.split(".")
    if len(parts) != 3:
        return None
    yy, mm, dd = (p.strip() for p in parts)
    if not (yy.isdigit() and mm.isdigit() and dd.isdigit()):
        return None
    return f"20{yy.zfill(2)}-{mm.zfill(2)}-{dd.zfill(2)}"


def _parse_list_page(html: str, name2code: dict) -> list[dict]:
    """크로스종목 목록 HTML 한 페이지 → [{nid, as_of, code, broker, title, url}].

    종목명 미해결(name2code에 없음)·nid 없음·날짜 이상 행은 skip. 같은 nid는 1건 dedup.
    """
    from bs4 import BeautifulSoup              # html.parser 백엔드 — lxml 비의존

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[int] = set()
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=lambda h: h and "company_read" in h)   # 제목→상세 링크 행만
        if a is None:
            continue
        m = _NID_RE.search(a.get("href", ""))
        if not m:
            continue
        nid = int(m.group(1))
        if nid in seen:
            continue
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        name = tds[0].get_text(strip=True)
        code = name2code.get(name)
        if not code:                          # 종목명 미해결 → skip(서버가 라이브 폴백)
            continue
        as_of = _iso(tds[4].get_text(strip=True))
        title = a.get_text(strip=True)
        if not as_of or not title:
            continue
        pdf = tr.find("a", href=lambda h: h and ".pdf" in h.lower())
        url = pdf["href"] if pdf else (_READ_BASE + a["href"])
        seen.add(nid)
        out.append({"nid": nid, "as_of": as_of, "code": code,
                    "broker": tds[2].get_text(strip=True), "title": title, "url": url})
    return out


def collect_pages(start_page: int = 1, max_pages: int = 400,
                  floor: str | None = None) -> tuple[list[dict], bool, bool]:
    """목록 페이지 [start_page, start_page+max_pages)를 newest-first 수집(list-only·상세 제외).

    반환 (reports, ok, done):
      ok=False   네트워크/HTTP 오류(transient) — 호출자가 커서 유지·재시도.
      done=True  floor(ISO 'YYYY-MM-DD')보다 오래된 리포트 도달 또는 빈 페이지(피드 끝) — 백필 완료 신호.
    목표가·투자의견은 여기서 안 붙인다(목록엔 없음) — enrich()가 상세에서 부착.
    """
    import requests

    name2code = _name_to_code()
    reports: list[dict] = []
    seen: set[int] = set()
    ok = True
    done = False
    for now_page in range(start_page, start_page + max_pages):
        try:
            r = requests.get(_LIST_URL, params={"page": now_page}, headers=_UA, timeout=20)
            r.encoding = "euc-kr"
            r.raise_for_status()
        except Exception:                     # noqa: BLE001 — 네트워크/HTTP 오류=transient(마커 금지)
            ok = False
            break
        page = _parse_list_page(r.text, name2code)
        if not page:
            done = True                       # 빈 페이지 = 피드 끝
            break
        hit_floor = False
        added = 0
        for rec in page:
            if rec["nid"] in seen:
                continue
            if floor and rec["as_of"] < floor:     # newest-first → floor 도달 시 종료
                hit_floor = True
                break
            seen.add(rec["nid"])
            reports.append(rec)
            added += 1
        if hit_floor or added == 0:           # floor 도달 또는 신규 0건(상한 loop 방지) = 끝
            done = True
            break
    return reports, ok, done


# ── 목표가·투자의견: 리포트 상세 페이지에서 부착(enrich) ────────────────────────
_OPI_RE = re.compile(r"투자의견[^가-힣A-Za-z]{0,8}([A-Za-z가-힣.]{2,12})")


def _opinion_score(text: str):
    """투자의견 문자열 → +1(매수)/0(중립)/−1(매도)/None(미상). 영·한 혼재 robust(매도계열 먼저)."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if any(k in s for k in ("sell", "under", "reduce", "매도", "축소", "비중축소")):
        return -1
    if any(k in s for k in ("buy", "outperform", "overweight", "add", "accumulate", "매수", "확대", "비중확대")):
        return 1
    if any(k in s for k in ("hold", "neutral", "market", "중립", "유지")):
        return 0
    return None                              # 미상 — 가짜 0 금지(정직)


def _parse_detail(html: str) -> tuple:
    """상세 페이지 HTML → (target, opinion). 목표가=헤더 `.money`(>0), 투자의견=텍스트→점수.

    Not Rated·목표가 0(미제시)은 target=None(가짜 0 금지). 실측: `.money`와 '목표가 N' 헤더 정규식 100% 일치.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    money = soup.select_one(".money")
    tv = money.get_text(strip=True).replace(",", "") if money else ""
    target = int(tv) if tv.isdigit() and int(tv) > 0 else None
    m = _OPI_RE.search(soup.get_text(" ", strip=True))
    opinion = _opinion_score(m.group(1)) if m else None
    return target, opinion


def _fetch_detail(nid) -> tuple:
    """리포트 상세 페이지 fetch → (target, opinion). fetch 실패는 (None, None)(list는 유효·정직)."""
    import requests
    try:
        r = requests.get(f"https://finance.naver.com/research/company_read.naver?nid={nid}",
                         headers=_UA, timeout=12)
        r.encoding = "euc-kr"
    except Exception:                        # noqa: BLE001 — 상세 fetch 실패는 target 없음(list는 유효)
        return None, None
    return _parse_detail(r.text)


def enrich(reports: list[dict], max_workers: int = 8) -> list[dict]:
    """각 리포트에 목표가·투자의견을 상세 페이지에서 병렬 부착(in-place·반환).

    ⚠ 상세 fetch는 종목당 크롤이라 비용이 크다 — 일일 증분(소량)·chunked 백필(여러 날 분산)에서만.
    """
    if not reports:
        return reports
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        details = list(ex.map(lambda rec: _fetch_detail(rec["nid"]), reports))
    for rec, (target, opinion) in zip(reports, details):
        rec["target"] = target
        rec["opinion"] = opinion
    return reports


def _reports_path(code: str):
    return default_manifest_path().parent / "reports" / f"{code.replace('/', '_')}.parquet"


def _panel_path(code: str):
    # 컨센서스 지표 패널 — load_stock_consensus가 consensus/{code}.parquet를 소비(consensus_kr[한경] 은퇴).
    return default_manifest_path().parent / "consensus" / f"{code.replace('/', '_')}.parquet"


# ── 컨센서스 지표 패널 — 원시 리포트에서 파생(한경 consensus_kr 은퇴·이 피드가 유일 소스) ──────────
# 패널 컬럼 = estimate.consensus.provides (spec.py 계약). 소스는 목록·상세 무관하게 {as_of,broker,target,opinion}.
PANEL_COLS = ["consensus_target", "consensus_target_median", "analyst_count",
              "consensus_opinion", "target_dispersion", "target_revision_pct",
              "days_since_report"]
_WINDOW = pd.Timedelta(days=180)        # standing 신선도窓 — 초과 리포트는 active에서 제외


def _build_panel(raw: pd.DataFrame) -> pd.DataFrame:
    """원시 리포트 전건 → 증권사별 최신 standing(180일窓) 횡단집계 변경점 패널.

    값이 변하는 날(=리포트 발표일 ∪ 발표일+180일 만료일)에만 행을 둔다(이후 indicators가 ffill).
    각 변경일 D: active=발표일∈(D−180, D]인 리포트 → 증권사별 최신 1건 → 평균/중앙값/count/의견/표준편차.
    한 증권사 새 리포트는 그 증권사 최신만 교체(다른 증권사 standing 유지=덮어쓰기 없음). PIT(as_of≤D만 사용).
    """
    if raw is None or raw.empty or "target" not in raw.columns:
        return pd.DataFrame(columns=PANEL_COLS)     # enrich 전(목표가 미부착) 원시 → 컨센 없음
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
        # 증권사별 최신 1건(동일 발표일이면 nid 큰 것 = 나중 발표)
        latest = active.sort_values(["as_of", "nid"]).groupby("broker", as_index=False).tail(1)
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
    """수집 리포트를 종목별 원시 아카이브에 append(nid dedup) + 컨센서스 지표 패널 재산출.

    반환 통계(종목수·신규행수). 원시(reports/{code}.parquet) 전건 보관(append-only·nid dedup·as_of 정렬),
    패널(consensus/{code}.parquet)은 매번 원시에서 재산출(멱등) — load_stock_consensus가 소비.
    """
    if not reports:
        return {"codes": 0, "new_rows": 0}
    by_code: dict[str, list[dict]] = {}
    for r in reports:
        by_code.setdefault(r["code"], []).append(r)

    n_codes = n_new = 0
    for code, recs in by_code.items():
        new = pd.DataFrame(recs)
        rp = _reports_path(code)
        if rp.exists():
            old = pd.read_parquet(rp)
            before = len(old)
            merged = pd.concat([old, new], ignore_index=True)
        else:
            before = 0
            merged = new
        merged = merged.drop_duplicates(subset=["nid"]).sort_values("as_of")
        write_parquet_atomic(merged, rp)
        panel = _build_panel(merged)                       # 컨센서스 지표 재산출(멱등)
        if not panel.empty:
            write_parquet_atomic(panel, _panel_path(code))
        n_codes += 1
        n_new += max(0, len(merged) - before)
    return {"codes": n_codes, "new_rows": n_new}


def rebuild_all_panels() -> int:
    """기존 원시(reports/{code}.parquet) 전부에서 컨센서스 패널 재산출. 반환 재산출 종목 수.

    한경→네이버 컨센서스 컷오버용(백필로 이미 수집된 원시에 패널이 없던 종목을 1회로 채움).
    **재fetch 없음**(저장된 원시만 사용)·멱등. 서버 startup에서 1회 호출(main._initial_reports_refresh).
    """
    import glob
    import os

    base = default_manifest_path().parent / "reports"
    n = 0
    for f in glob.glob(str(base / "*.parquet")):
        code = os.path.basename(f)[:-len(".parquet")]
        raw = pd.read_parquet(f)
        panel = _build_panel(raw)
        if not panel.empty:
            write_parquet_atomic(panel, _panel_path(code))
            n += 1
    return n
