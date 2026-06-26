"""dart-fss 기반 연결재무제표 추출 — 전자공시 보고서의 계정명·표시순서·값을 XBRL로 '그대로'.

손수 만든 HTML 파서(dart_doc)가 회사별 포맷차(은행 양식·헤더·단위·△음수·주석)에 취약했던 것을
대체. dart-fss는 XBRL 개념(concept) 기반으로 다년도 재무제표를 정렬 추출하므로 은행·지주 포함
전 종목에 강건. 결과를 우리 포맷({periods, rows:[{account,canon,bold,child,values}]})으로 변환.

값 단위: 원 → 억원(/1e8)으로 환산(기존 financials 저장구조와 통일 — 웹은 ×100으로 백만원 표시).
"""
from __future__ import annotations

import logging
import re
import warnings

warnings.filterwarnings("ignore")
_log = logging.getLogger("app.dart_fss_fetch")
_KEY_SET = False
_PER = re.compile(r"^\d{8}(-\d{8})?$")


def _ensure_key() -> bool:
    global _KEY_SET
    if _KEY_SET:
        return True
    from .config import settings
    k = settings.OPENDART_API_KEY
    if not k:
        return False
    import dart_fss as dfs
    dfs.set_api_key(api_key=k)
    _KEY_SET = True
    return True


def _conv(df, n_years: int = 5) -> dict | None:
    """dart-fss FinancialStatement DataFrame → {periods, rows}. 보고서 표시순서·한글계정 그대로."""
    if df is None or getattr(df, "empty", True):
        return None
    from . import dart_doc
    cols = list(df.columns)
    lk = next((c for c in cols if isinstance(c[1], str) and c[1] == "label_ko"), None)
    classcols = [c for c in cols if isinstance(c[1], str) and c[1].startswith("class")]
    pcols = [c for c in cols if _PER.match(str(c[0]))]
    pcols = [c for c in pcols if not str(c[0]).split("-")[-1].endswith("0101")]  # 재작성 기초잔액 컬럼 제외
    if not lk or not pcols:
        return None
    sel = pcols[:n_years]                                   # 최신 우선
    periods = [f"{str(c[0]).split('-')[-1][:4]}/{str(c[0]).split('-')[-1][4:6]}" for c in sel][::-1]
    rows = []
    for _, r in df.iterrows():
        nm = str(r[lk]).strip()
        if nm in ("", "nan"):                               # label_ko 비면 최하위 class
            for c in reversed(classcols):
                v = str(r[c]).strip()
                if v not in ("", "nan"):
                    nm = v
                    break
        if nm in ("", "nan"):
            continue
        vals = [None if str(r[c]) == "nan" or r[c] is None else float(r[c]) / 1e8 for c in sel[::-1]]
        if not any(v is not None for v in vals):
            continue
        ns = nm.replace(" ", "")
        depth = sum(1 for c in classcols if str(r[c]).strip() not in ("", "nan"))
        rows.append({"account": nm, "canon": dart_doc._ckey(nm),
                     "bold": ns in dart_doc._BOLD, "child": depth > 1, "values": vals})
    return {"periods": periods, "rows": rows} if rows else None


def fetch(code: str) -> dict | None:
    """종목 연결재무제표 5개년(원문 XBRL, 보고서 그대로). {annual:{PL,BS,CF}} 또는 None."""
    from datetime import date
    from . import dart
    if not _ensure_key():
        return None
    cc = dart.corp_code(code)
    if not cc:
        return None
    import dart_fss as dfs
    bgn = f"{date.today().year - 6}0101"
    try:
        fs = dfs.fs.extract(corp_code=cc, bgn_de=bgn, separate=False, report_tp="annual", dataset="web")
    except Exception as e:  # noqa: BLE001
        _log.warning("dart-fss extract 실패 %s: %s", code, e)
        return None
    out = {}
    pl = _conv(fs["is"] if fs["is"] is not None else fs["cis"])
    if pl:
        out["PL"] = pl
    bs = _conv(fs["bs"])
    if bs:
        out["BS"] = bs
    cf = _conv(fs["cf"])
    if cf:
        out["CF"] = cf
    return {"annual": out} if out else None
