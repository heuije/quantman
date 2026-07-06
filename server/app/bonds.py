"""국가별 국채 금리 서빙 — 데이터엔진 bonds 피드(볼륨) SSOT + 라이브 폴백 (서빙 일원화 Phase 6a).

수집·fetch는 core `quant_core.data.feeds.bonds`가 단독 소유(재배포 warmup·요청당 중복 크롤 제거).
이 모듈은 볼륨 커브를 **표시용으로 조립**(최신 커브·bp 변동·최근 10년 슬라이스)만 한다.
볼륨 미수집(신규 배포 직후·수집 실패) 시에만 피드 fetch로 라이브 폴백(원칙6) — fetch 로직 재사용(중복 없음).
"""
from __future__ import annotations

import io
import logging
from datetime import date

import pandas as pd

from quant_core.data.feeds import bonds as _feed

_log = logging.getLogger("app.bonds")
_YEARS = 10          # 화면 시계열 범위(엑셀은 전체)

COUNTRIES = _feed.COUNTRIES


def _curve(cc: str) -> pd.DataFrame | None:
    """볼륨 우선(1일 신선) → 없거나 오래되면 피드 fetch+저장(self-heal), 실패 시 stale 저장본.

    일일 cron(_feed.refresh_all)이 선제 갱신하고, 미스 시 첫 요청이 self-heal한다(estimate_kr 패턴).
    fetch 로직은 core 피드가 단독 소유 — 서버는 조립만(중복 크롤 제거).
    """
    return _feed.get(cc)


def _empty(cc: str) -> dict:
    name, freq = COUNTRIES.get(cc, (cc, ""))
    return {"country": cc, "name": name, "freq": freq, "maturities": _feed.maturities(cc),
            "series": [], "latest": {}, "asof": None}


def _assemble(cc: str, df: pd.DataFrame | None) -> dict:
    if df is None or df.empty:
        return _empty(cc)
    cutoff = pd.Timestamp(f"{date.today().year - _YEARS}-01-01")   # 옛 서빙과 동일(연초 기준)
    df = df[df.index >= cutoff]
    if df.empty:
        return _empty(cc)
    mats = _feed.maturities(cc)
    series = []
    for ts, row in df.iterrows():
        d = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
        pt = {"date": d}
        for m in mats:
            v = row.get(m)
            if v is not None and not pd.isna(v):
                pt[m] = round(float(v), 4)
        series.append(pt)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    latest = {}
    for m in mats:
        cur, prv = last.get(m), prev.get(m)
        cur = None if (cur is None or pd.isna(cur)) else float(cur)
        prv = None if (prv is None or pd.isna(prv)) else float(prv)
        latest[m] = {"yield": cur,
                     "chg_bp": round((cur - prv) * 100, 1) if (cur is not None and prv is not None) else None}
    name, freq = COUNTRIES[cc]
    asof = df.index[-1].date().isoformat() if hasattr(df.index[-1], "date") else None
    return {"country": cc, "name": name, "freq": freq, "maturities": mats,
            "series": series, "latest": latest, "asof": asof}


def country(cc: str) -> dict:
    cc = cc.upper()
    if cc not in COUNTRIES:
        return _empty(cc)
    try:
        return _assemble(cc, _curve(cc))
    except Exception as e:                        # noqa: BLE001
        _log.warning("국채금리 서빙 실패 %s: %s", cc, e)
        return _empty(cc)


def to_xlsx() -> bytes:
    """전체 국가·전체 기간 국채금리 .xlsx — 국가별 시트(날짜 × 만기)."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for cc in COUNTRIES:
        try:
            df = _curve(cc)
        except Exception as e:                    # noqa: BLE001
            _log.warning("국채 엑셀 %s 실패: %s", cc, e)
            continue
        if df is None or df.empty:
            continue
        mats = _feed.maturities(cc)
        ws = wb.create_sheet(f"{COUNTRIES[cc][0]}({cc})"[:28])
        ws.append(["Date", *mats])
        for ts, row in df.iterrows():
            d = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
            ws.append([d, *[(None if pd.isna(row.get(m)) else float(row.get(m))) for m in mats]])
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
