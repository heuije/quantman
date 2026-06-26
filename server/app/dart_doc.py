"""DART 전자공시 원문(보고서) 재무제표 파서 — 표시순서·계정명·값을 보고서 그대로.

OpenAPI(fnlttSinglAcntAll)는 손익을 XBRL 요소순으로 줘 표시순서가 깨진다(매출액이 맨 뒤 등).
보고서 viewer와 동일한 순서를 얻으려면 원문 문서(document.xml ZIP 안 HTML 표)를 파싱한다.

연결/별도 구분: OpenAPI 연결값(latest년 매출액·자산총계·영업CF)과 표의 시그니처 행 값을 대조해
일치하는 표를 연결 재무제표로 확정(가장 신뢰). 연도매핑: '제 NN 기 … YYYY.MM.DD' 앵커로 base=연도−기수.
값 단위: 원 → 억원(/1e8)으로 환산(기존 financials 구조와 통일).
"""
from __future__ import annotations

import io
import logging
import re
import warnings
import zipfile

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .config import settings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
_log = logging.getLogger("app.dart_doc")
_BASE = "https://opendart.fss.or.kr/api"
_UA = {"User-Agent": "Mozilla/5.0"}

_SIG = {"PL": ("매출액", "영업수익", "수익(매출액)"), "BS": ("자산총계", "자산합계"),
        "CF": ("영업활동현금흐름", "영업활동으로인한현금흐름")}


def _has_sig(text: str, sj: str) -> bool:
    nt = re.sub(r"\s", "", text)
    return any(s.replace(" ", "") in nt for s in _SIG[sj])
_BOLD = {s.replace(" ", "") for s in {
    "매출액", "수익(매출액)", "영업수익", "매출총이익", "영업이익", "영업손익", "당기순이익", "당기순손익",
    "법인세비용차감전순이익", "법인세비용차감전순손익", "총포괄손익", "분기총포괄손익", "반기총포괄손익",
    "자산총계", "부채총계", "자본총계", "유동자산", "비유동자산", "유동부채", "비유동부채", "자본금",
    "부채와자본총계", "부채및자본총계", "자본과부채총계",
    "영업활동현금흐름", "투자활동현금흐름", "재무활동현금흐름",
    "기말의현금및현금성자산", "기초의현금및현금성자산", "기말현금및현금성자산", "기초현금및현금성자산",
}}


def _key() -> str:
    return settings.OPENDART_API_KEY


def _num(s):
    s = (s or "").strip()
    neg = (s.startswith("(") and s.endswith(")")) or bool(re.match(r"[△▲\-−–]", s))  # 괄호·삼각형·마이너스 음수
    s = re.sub(r"[^0-9.]", "", s)
    if not s or s == ".":
        return None
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\(\s*주[\s\d,.]*\)", "", s)                               # 주석참조 (주4,29)
    s = re.sub(r"\s*[-–]\s*(?:[-–]\s*)+$", "", s)                          # 끝 점선
    s = re.sub(r"^[\(\[]?\s*[IVXLCⅠ-Ⅻⅰ-ⅻ0-9]{1,4}\s*[\)\].]\s*", "", s).strip()  # 접두번호
    return s.strip()


def _doc_zip(rcept_no: str):
    try:
        r = requests.get(f"{_BASE}/document.xml", params={"crtfc_key": _key(), "rcept_no": rcept_no},
                         headers=_UA, timeout=90)
        return zipfile.ZipFile(io.BytesIO(r.content))
    except Exception as e:  # noqa: BLE001
        _log.warning("document.xml 실패 %s: %s", rcept_no, e)
        return None


def _year_map(text: str) -> dict:
    """'제 56기 … 2025년 12월 31일 (까지/현재)' 앵커로 base=연도−기수 → {base, mm}.
    날짜는 'YYYY년 MM월' 또는 'YYYY.MM' 둘 다 허용(원문은 년월일, viewer는 점)."""
    # 종료일(보고기간말) = 까지/현재 앞의 날짜
    date = r"(\d{4})\s*[년.]\s*(\d{1,2})\s*[월.]"
    anchors = []
    for m in re.finditer(rf"제\s*(\d+)\s*기[^제]{{0,160}}?{date}\s*\d{{1,2}}\s*일?\s*(?:까지|현재)", text):
        anchors.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if not anchors:                          # 폴백: 까지/현재 없이 첫 날짜
        for m in re.finditer(rf"제\s*(\d+)\s*기[^제]{{0,160}}?{date}", text):
            anchors.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if not anchors:
        return {}
    gi, yr, mm = max(anchors)
    return {"base": yr - gi, "mm": f"{mm:02d}"}


def _parse_table(tb, ymap, ref_vals):
    """표 1개 파싱. 시그니처 행 값이 ref_vals(억원) 중 하나와 일치해야 채택(연결 확정).
    {periods:[YYYY/MM], rows:[{account,values(억원)}]} 또는 None."""
    trs = tb.find_all("tr")
    pcols, gis = [], []
    for tr in trs:
        cells = [c.get_text() for c in tr.find_all(["td", "th", "te"])]
        # 기간열: '제56(당)기' 또는 '제57기'/'제57기말'(당/전 표기 없는 필러도 허용)
        ks = [i for i, c in enumerate(cells) if re.search(r"제\s*\d+\s*(?:기|\(?\s*[당전])", c)]
        if len(ks) >= 1:
            pcols = ks
            gis = [int(re.search(r"제\s*(\d+)", cells[i]).group(1)) for i in ks]
            break
    if not pcols or "base" not in ymap:
        return None
    ref_vals = [rv for rv in (ref_vals or []) if rv is not None]
    mm = ymap["mm"]
    periods = [f"{ymap['base'] + g}/{mm}" for g in gis]
    raw_rows = []
    for tr in trs:
        cells = [c.get_text() for c in tr.find_all(["td", "th", "te"])]
        if len(cells) <= max(pcols):
            continue
        acc = _clean(re.sub(r"\s+", " ", cells[0]).strip())
        acc_ns = acc.replace(" ", "")
        if (not acc or re.search(r"제\s*\d+\s*기", acc) or "과목" in acc_ns
                or acc_ns in {"구분", "항목", "계정", "계정과목", "주석"}):
            continue
        vals = [_num(cells[i]) for i in pcols]                 # RAW(단위 미보정)
        if not any(v is not None for v in vals):
            continue
        raw_rows.append({"account": acc, "raw": vals})
    if len(raw_rows) < 4:
        return None
    # 단위 자동보정: 회사마다 원/천원/백만원 → 억원 나눗수(원1e8·천원1e5·백만원1e2)를
    # 시그니처 행 raw값이 ref(억원)와 일치하는 것으로 탐색. 동시에 연결 확정(별도·요약은 미일치).
    sig_raws = [v for r in raw_rows if r["account"].replace(" ", "") in _SIG_KEYS
                for v in r["raw"] if v is not None]
    div = None
    if ref_vals:
        for d in (1e8, 1e2, 1e5, 1e6, 1e3, 1.0):
            if any(any(abs(sv / d - rv) <= max(1.0, abs(rv) * 0.005) for rv in ref_vals)
                   for sv in sig_raws):
                div = d
                break
        if div is None:
            return None                                        # 연결 아님/단위 불명 → 기각
    else:
        div = 1e8
    rows = [{"account": r["account"], "values": [None if v is None else v / div for v in r["raw"]]}
            for r in raw_rows]
    return {"periods": periods, "rows": rows}


_SIG_KEYS = {s.replace(" ", "") for v in _SIG.values() for s in v}


def parse_doc(rcept_no: str, refs: dict) -> dict:
    """원문 ZIP에서 연결 PL/BS/CF 파싱. refs={'PL':[매출액억원...],'BS':[자산총계...],'CF':[영업CF...]}."""
    zf = _doc_zip(rcept_no)
    if not zf:
        return {}
    cands = {"PL": [], "BS": [], "CF": []}
    for n in [f for f in zf.namelist() if f.endswith(".xml")]:
        try:
            txt = zf.read(n).decode("utf-8", "ignore")
        except Exception:
            continue
        if "연결" not in txt:
            continue
        soup = BeautifulSoup(txt, "html.parser")
        ymap = _year_map(soup.get_text(" "))
        for tb in soup.find_all("table"):
            t = tb.get_text()
            if not re.search(r"\d{1,3}(,\d{3}){2,}", t):
                continue
            # 단일 statement만 채택 — 요약재무정보(PL·BS·CF sig 3개 동시)는 계정이 섞여 제외.
            present = [sj for sj in ("PL", "BS", "CF") if _has_sig(t, sj)]
            if len(present) != 1:
                continue
            sj = present[0]
            st = _parse_table(tb, ymap, refs.get(sj, []))
            if st:
                cands[sj].append(st)
    # 후보 중 행 많은 표 채택 — 요약재무정보(행 적음)가 아닌 전체 statement(전 계정) 우선.
    # (순서 파악용이라 기간수보다 계정수가 중요.)
    return {sj: max(lst, key=lambda s: (len(s["rows"]), len(s["periods"])))
            for sj, lst in cands.items() if lst}


def _rcept(cc: str, fy: int):
    """사업보고서(FY) 접수번호 — FY+1년 1~6월 제출. list.json(정기공시 A001)."""
    fyr = fy + 1
    try:
        r = requests.get(f"{_BASE}/list.json", params={
            "crtfc_key": _key(), "corp_code": cc, "bgn_de": f"{fyr}0101", "end_de": f"{fyr}0630",
            "pblntf_detail_ty": "A001", "page_count": "30"}, headers=_UA, timeout=30)
        for x in r.json().get("list", []):
            if "사업보고서" in (x.get("report_nm") or ""):
                return x.get("rcept_no")
    except Exception as e:  # noqa: BLE001
        _log.warning("list.json 실패 %s/%s: %s", cc, fy, e)
    return None


def _refs(code: str, draw: dict | None = None) -> dict:
    """OpenAPI 연결값(전 연도 매출액·자산총계·영업CF, 억원) 집합 — 원문 연결표 확정용.
    draw(이미 받은 dart.fetch 결과) 주면 재호출 안 함 — 배치 OpenAPI 부하·시간 절반."""
    from . import dart
    d = draw if draw is not None else (dart.fetch(code) or {})
    ann = d.get("annual", {})

    def vals(sec, names):
        out = set()
        for r in ann.get(sec, {}).get("rows", []):
            if (r.get("canon") or r.get("account", "")).replace(" ", "") in names:
                out.update(v for v in r.get("values", []) if v is not None)
        return out
    return {"PL": vals("PL", {"매출액", "영업수익", "수익(매출액)"}),
            "BS": vals("BS", {"자산총계", "자산합계"}),
            "CF": vals("CF", {"영업활동현금흐름", "영업활동으로인한현금흐름"})}


# 보고서마다 계정명이 손익/이익으로 갈림(흑자=영업이익·적자=영업손익) — 병합 정렬용 canon.
_DOC_CANON = {
    "영업손익": "영업이익", "당기순손익": "당기순이익", "분기순손익": "당기순이익", "반기순손익": "당기순이익",
    "법인세비용차감전순손익": "법인세비용차감전순이익", "법인세비용차감전계속영업손익": "법인세비용차감전계속영업이익",
    "계속영업당기순손익": "계속영업당기순이익", "계속영업손익": "계속영업이익", "중단영업손익": "중단영업이익",
    "매출총손익": "매출총이익", "지분법손익": "지분법이익",
}


def _ckey(name: str) -> str:
    """병합 정렬 키 — 손익↔이익 변이 정규화(회사 보고서 간 계정 매칭)."""
    n = re.sub(r"\s", "", name or "")
    return _DOC_CANON.get(n, n)


def _merge(filings: list) -> dict:
    """사업보고서 여러 건(각 2개년)을 5개년으로 병합. 순서·표시계정명은 최신 보고서,
    값 정렬은 canon(손익↔이익 변이 흡수)으로 — 옛 연도 계정명이 달라도 같은 줄에 채움."""
    out = {}
    for sj in ("PL", "BS", "CF"):
        sts = [f[sj] for f in filings if sj in f]
        if not sts:
            continue
        periods = sorted({p for st in sts for p in st["periods"]})[-5:]
        vmap = {}
        for st in sts:                                  # 최신 먼저 → setdefault로 최신 우선
            for r in st["rows"]:
                ck = _ckey(r["account"])
                for p, v in zip(st["periods"], r["values"]):
                    if v is not None:
                        vmap.setdefault((ck, p), v)
        rows = []
        for r in sts[0]["rows"]:                         # 최신 보고서의 순서·표시명
            ck = _ckey(r["account"])
            ns = r["account"].replace(" ", "")
            rows.append({"account": r["account"], "canon": ck, "bold": ns in _BOLD,
                         "child": ns not in _BOLD, "values": [vmap.get((ck, p)) for p in periods]})
        if rows:
            out[sj] = {"periods": periods, "rows": rows}
    return out


def fetch_order(code: str, draw: dict | None = None) -> dict:
    """최신 사업보고서 원문에서 PL/BS/CF의 '표시순서 + 계정별 표기부호'를 추출 → {sj:[(ckey,sign)...]}.
    OpenAPI(값·계정명 정확)를 이 순서로 재정렬하는 용도. 문서 1건만 받아 가볍다. 실패 시 {}.
    draw(이미 받은 dart.fetch 결과) 주면 refs용 재호출 생략(배치 가속)."""
    from datetime import date
    from . import dart
    cc = dart.corp_code(code)
    if not cc or not _key():
        return {}
    refs = _refs(code, draw)
    if not any(refs.values()):
        return {}
    y_hi = date.today().year - 1
    for fy in (y_hi, y_hi - 1):              # 최신 사업보고서(당해 미제출이면 직전연도)
        rc = _rcept(cc, fy)
        if not rc:
            continue
        st = parse_doc(rc, refs)
        if st:
            out = {}
            for sj, v in st.items():
                lay = []
                for r in v.get("rows", []):
                    nz = [x for x in r["values"] if x is not None]
                    sign = -1 if (nz and nz[-1] < 0) else (1 if nz else None)  # 보고서 표기 부호(비용=음수)
                    lay.append((_ckey(r["account"]), sign))
                if lay:
                    out[sj] = lay
            if out:
                return out
    return {}


def fetch_doc(code: str, draw: dict | None = None) -> dict | None:
    """종목 5개년 연결재무제표(원문, 보고서 표시순서·계정명·값 그대로). 사업보고서 3건(FY,FY-2,FY-4) 병합.
    draw(이미 받은 dart.fetch)로 refs 재호출 생략. 행에 canon/bold/child 포함(차트·소계·표시용)."""
    from datetime import date
    from . import dart
    cc = dart.corp_code(code)
    if not cc or not _key():
        return None
    refs = _refs(code, draw)
    if not any(refs.values()):
        return None
    y_hi = date.today().year - 1
    filings = []
    for fy in (y_hi, y_hi - 2, y_hi - 4):
        rc = _rcept(cc, fy)
        if rc:
            st = parse_doc(rc, refs)
            if st:
                filings.append(st)
    if not filings:
        return None
    return {"fetched": date.today().isoformat(), "annual": _merge(filings), "quarterly": {}}

