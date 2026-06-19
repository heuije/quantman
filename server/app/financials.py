"""재무제표(Financials) — FnGuide(전자공시 집계) 연결 기준 PL·BS·CF.

연간(YoY%)·분기(QoQ%)를 1회 계산해 디스크에 저장하고, Financials 탭은 저장본을 즉시 서빙한다
(로딩 없음). 실시간 갱신 불필요 — 분기/사업보고서 제출 마감일 저녁 cron(main.py)이 일괄 갱신.

데이터 출처: FnGuide SVD_Finance(키리스). DART OpenAPI(OPENDART_API_KEY)는 로컬 미설정이라
키리스 FnGuide로 통일(EBITDA·추정실적과 동일 정책). 서버에 키가 있으면 향후 DART로 5개년 확장 가능.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from functools import lru_cache
from io import StringIO

import pandas as pd  # noqa: F401  (bs4가 주 파서지만 환경 일치 위해 유지)
import requests
from bs4 import BeautifulSoup

_log = logging.getLogger("app.financials")
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "financials")
_FRESH_DAYS = 80          # 분기 주기(≈91일)보다 짧게 — 마감일 cron이 우선 갱신
_UA = {"User-Agent": "Mozilla/5.0"}

# 재무제표 3종 → FnGuide div id (연간, 분기)
_STMTS = [
    ("PL", "손익계산서", "divSonikY", "divSonikQ"),
    ("BS", "재무상태표", "divDaechaY", "divDaechaQ"),
    ("CF", "현금흐름표", "divCashY", "divCashQ"),
]


def _num(s: str):
    s = (s or "").strip().replace(",", "")
    if not s or s in {"-", "N/A", "n/a"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _pct(cur, prev):
    """전기 대비 증감률(%). 전기가 0/None이거나 부호가 바뀌면 None(왜곡 방지)."""
    if cur is None or prev is None or prev == 0:
        return None
    if (cur < 0) != (prev < 0):   # 적자전환/흑자전환은 % 무의미
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _parse_div(soup: BeautifulSoup, div_id: str, annual: bool) -> dict:
    """FnGuide 재무제표 table 1개 파싱 → {periods:[yyyy/mm], rows:[{account,bold,values:[]}]}."""
    div = soup.find(id=div_id)
    if div is None:
        return {"periods": [], "rows": []}
    table = div.find("table")
    if table is None:
        return {"periods": [], "rows": []}
    head = table.find("thead")
    cols = [th.get_text(strip=True) for th in head.find_all("th")] if head else []
    # 기간 컬럼만 — 연간은 결산월(/12 등 사업연도말)만, 분기는 모든 분기 컬럼
    period_idx, periods = [], []
    for i, c in enumerate(cols[1:]):   # cols[0]=IFRS(연결)
        if re.match(r"^\d{4}/\d{2}$", c):
            if annual and not c.endswith("/12"):
                continue              # 연간 표의 누적분기(예: 2026/03) 제외
            period_idx.append(i)
            periods.append(c)
    body = table.find("tbody")
    rows = []
    gid, cur_group = 0, None
    for tr in body.find_all("tr"):
        th = tr.find("th")
        if th is None:
            continue
        cls = tr.get("class") or []
        # FnGuide UI 토글 문구 제거 → 순수 계정명
        account = re.sub(r"\s*계산에 참여한 계정\s*(펼치기|접기)\s*$", "",
                         th.get_text(" ", strip=True)).strip()
        if not account:
            continue
        is_parent = "acd_dep_start_close" in cls or "acd_dep_start_open" in cls   # 펼침 가능 부모
        is_child = "acd_dep2_sub" in cls                                          # 기본 숨김 상세
        bold = "rowBold" in cls
        if is_parent:
            gid += 1; cur_group = gid; group = gid
        elif is_child:
            group = cur_group
        else:
            cur_group = None; group = None
        tds = tr.find_all("td")
        values = [(_num(tds[i].get_text(strip=True)) if i < len(tds) else None) for i in period_idx]
        # 전 기간 공란인 비주요 계정(해당사항 없음)은 노이즈 → 생략
        if not bold and not is_parent and all(v is None for v in values):
            continue
        rows.append({"account": account, "bold": bold, "parent": is_parent,
                     "child": is_child, "group": group, "values": values})
    return {"periods": periods, "rows": rows}


def _with_change(parsed: dict) -> dict:
    """각 계정에 기간별 증감률(YoY 또는 QoQ) 배열 추가. change[0]=None(기준기)."""
    for row in parsed["rows"]:
        vals = row["values"]
        row["change"] = [None] + [_pct(vals[i], vals[i - 1]) for i in range(1, len(vals))]
    return parsed


def _add_pl_metrics(period: dict) -> None:
    """손익계산서(PL)에 이익률(%) + EBITDA·EBITDA Margin(%) 하위행을 1회 계산해 삽입.

    - 영업이익률/당기순이익률/매출총이익률 = 항목 / 매출액 × 100 (해당 항목 바로 아래).
    - EBITDA = 영업이익 + 감가상각비 + 무형자산상각비(현금흐름표) → EBITDA Margin = EBITDA / 매출액.
    파생행은 derived=True(들여쓰기 표시), 비율행은 pct=True(% 포맷). 저장본에 들어가 매번 재계산 X.
    """
    pl, cf = period.get("PL"), period.get("CF")
    if not pl or not pl.get("rows"):
        return
    norm = lambda r: r["account"].replace(" ", "")

    def find(stmt, pred):
        if not stmt:
            return None
        return next((r for r in stmt["rows"] if pred(norm(r))), None)

    rev = find(pl, lambda a: a == "매출액")
    if not rev:
        return
    rv = rev["values"]

    def ratio(num):
        return [(round(x / d * 100, 1) if (x is not None and d) else None) for x, d in zip(num, rv)]

    def mk_pct(label, vals):
        return {"account": label, "values": vals, "change": [None] * len(vals),
                "bold": False, "parent": False, "child": False, "group": None,
                "pct": True, "derived": True}

    # EBITDA = 영업이익 + 감가상각비 + 무형자산상각비(CF, 대손/사채상각 제외)
    op = find(pl, lambda a: a == "영업이익")
    dep = find(cf, lambda a: "감가상각" in a and "대손" not in a)
    amo = find(cf, lambda a: "무형자산" in a and "상각" in a and "대손" not in a)
    ebitda = None
    if op:
        def at(row, i):
            return row["values"][i] if (row and i < len(row["values"])) else None
        ev = []
        for i, o in enumerate(op["values"]):
            ev.append(None if o is None else o + (at(dep, i) or 0) + (at(amo, i) or 0))
        if any(v is not None for v in ev):
            ebitda = ev

    new = []
    for r in pl["rows"]:
        new.append(r)
        a = norm(r)
        if a == "매출총이익":
            new.append(mk_pct("매출총이익률(%)", ratio(r["values"])))
        elif a == "영업이익":
            new.append(mk_pct("영업이익률(%)", ratio(r["values"])))
            if ebitda:
                new.append({"account": "EBITDA", "values": ebitda, "bold": False,
                            "parent": False, "child": False, "group": None, "derived": True,
                            "change": [None] + [_pct(ebitda[i], ebitda[i - 1]) for i in range(1, len(ebitda))]})
                new.append(mk_pct("EBITDA Margin(%)", ratio(ebitda)))
        elif a == "당기순이익":
            new.append(mk_pct("당기순이익률(%)", ratio(r["values"])))
    pl["rows"] = new


def _fetch(code: str) -> dict:
    r = requests.get("https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp",
                     params={"pGB": 1, "gicode": f"A{code}"}, headers=_UA, timeout=20)
    if r.encoding in (None, "ISO-8859-1"):
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    annual, quarterly = {}, {}
    for key, _label, div_y, div_q in _STMTS:
        annual[key] = _with_change(_parse_div(soup, div_y, annual=True))
        quarterly[key] = _with_change(_parse_div(soup, div_q, annual=False))
    _add_pl_metrics(annual)      # 이익률·EBITDA 하위행 1회 계산·삽입(저장본에 포함)
    _add_pl_metrics(quarterly)
    return {"fetched": date.today().isoformat(), "annual": annual, "quarterly": quarterly}


def _path(code: str) -> str:
    return os.path.join(_DIR, f"{code}.json")


def _save(code: str, data: dict) -> None:
    os.makedirs(_DIR, exist_ok=True)
    tmp = _path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _path(code))


def refresh(code: str) -> dict:
    """FnGuide에서 재무제표를 받아 증감률 계산·저장. 반환: 저장 데이터."""
    data = _fetch(code)
    try:
        _save(code, data)
    except Exception:
        _log.exception("재무제표 저장 실패 %s", code)
    return data


def _load_or_fetch(code: str) -> dict:
    path = _path(code)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            fetched = cached.get("fetched", "")
            if fetched and (date.today() - date.fromisoformat(fetched)).days < _FRESH_DAYS:
                return cached
    except Exception:
        pass
    try:
        return refresh(code)
    except Exception as e:
        _log.warning("재무제표 조회 실패 %s: %s", code, e)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"fetched": "", "annual": {}, "quarterly": {}}


@lru_cache(maxsize=1024)
def _cached(code: str, _day: str) -> dict:
    return _load_or_fetch(code)


def financials(code: str) -> dict:
    """Financials 탭용 — 메모리 캐시(프로세스 내 즉시) → 디스크 저장본 → 라이브(최후) 순.

    디스크는 Railway 재시작 시 휘발하므로 (code, 오늘) 메모리 lru_cache로 같은 종목 재요청·재마운트를
    즉시 응답한다. 기동 시 prewarm으로 산업 종목을 미리 데워 첫 진입도 빠르게."""
    return _cached(str(code).strip(), date.today().isoformat())


def clear_cache() -> None:
    """장 마감 후 등 캐시 무효화."""
    _cached.cache_clear()


def to_xlsx(code: str) -> bytes:
    """재무제표 전체(연간·분기 × 손익/재무/현금)를 .xlsx 바이트로 — 사용자 다운로드용.

    시트 = '연간_손익계산서' 등 6개. 각 시트: 계정 × 기간 값 표 + 증감률(YoY/QoQ) 행.
    파생/비율 행(이익률·EBITDA 등)도 저장본 그대로 포함. 값 없으면 빈 칸."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    data = financials(code)
    labels = {"PL": "손익계산서", "BS": "재무상태표", "CF": "현금흐름표"}
    wb = Workbook()
    wb.remove(wb.active)
    for pdata, tag in [(data.get("annual") or {}, "연간"), (data.get("quarterly") or {}, "분기")]:
        for key in ("PL", "BS", "CF"):
            st = pdata.get(key) or {}
            periods = st.get("periods") or []
            rows = st.get("rows") or []
            if not periods or not rows:
                continue
            ws = wb.create_sheet(f"{tag}_{labels[key]}"[:31])
            ws.append(["계정"] + list(periods))
            for c in ws[1]:
                c.font = Font(bold=True)
                c.alignment = Alignment(horizontal="center")
            for r in rows:
                is_pct = bool(r.get("pct"))
                vals = r.get("values") or []
                ws.append([r.get("account", "")] + [(v if v is not None else None) for v in vals])
                row_i = ws.max_row
                for j in range(len(vals)):
                    cell = ws.cell(row=row_i, column=2 + j)
                    cell.number_format = "0.0" if is_pct else "#,##0;(#,##0);-"
                    cell.alignment = Alignment(horizontal="right")
            ws.column_dimensions["A"].width = 30
            for i in range(len(periods)):
                ws.column_dimensions[get_column_letter(2 + i)].width = 14
            ws.freeze_panes = "B2"
    if not wb.sheetnames:
        wb.create_sheet("재무제표").append(["재무 데이터가 없습니다."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def prewarm(codes: list[str]) -> int:
    """기동 시 산업 종목 재무제표를 메모리(+디스크)에 미리 적재 — 첫 탭 진입도 즉시."""
    n = 0
    for c in codes:
        try:
            d = financials(c)
            if d.get("annual"):
                n += 1
        except Exception:
            pass
    return n


def refresh_all(codes: list[str]) -> int:
    """분기/사업보고서 마감일 cron이 호출 — 추적 종목 재무제표 일괄 갱신(병렬). 반환: 성공 수."""
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for data in ex.map(lambda c: _safe_refresh(c), codes):
            if data:
                ok += 1
    _log.info("재무제표 %d/%d 갱신·저장", ok, len(codes))
    return ok


def _safe_refresh(code: str):
    try:
        d = refresh(code)
        return d if (d.get("annual") or d.get("quarterly")) else None
    except Exception:
        return None
