"""KOMIS(한국광해광업공단 자원정보) 배터리·핵심광물 가격 — 무료, 키 불필요.

니켈·탄산리튬·코발트 등은 LME/중국 현물 계약이라 Yahoo/FDR 선물 피드에 없다(ZN=F=국채선물 등
오매핑 함정). KOMIS 공개 ajax로 가져온다 — 세션·CSRF 불필요(cold 호출 검증됨):
  - getMnrlPrcByMnrkndUnqCd : 현재가(cmercPrc)·전일/전월 등락(flctn)·단위
  - getChartData            : 월별 시계열(클릭 차트용)
광종/가격기준 코드는 KOMIS가 부여한 안정 코드(/ajax/common/getMnrlKndInfoCodeList로 확인).
일/월별 데이터라 일중 변동은 없고 전일(DAY) 또는 전월(MONTH) 대비 등락을 쓴다.
"""
from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache

import requests

_log = logging.getLogger("app.komis")
_BASE = "https://www.komis.or.kr"
_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      "X-Requested-With": "XMLHttpRequest",
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      "Referer": _BASE + "/Komis/RsrcPrice/BaseMetals"}

# (표시명, HP000 카테고리, 광종코드, 가격기준코드). HP001=비철금속 / HP002=희소금속.
METALS = [
    ("탄산리튬", "HP002", "MNRL0001", "773"),   # Li2CO3 99.5% CIF China, USD/kg
    ("수산화리튬", "HP002", "MNRL0001", "772"),  # LiOH 56.5%, USD/kg
    ("니켈", "HP001", "MNRL0002", "502"),        # LME CASH, USD/ton
    ("코발트", "HP002", "MNRL0003", "791"),      # 99.8% Rotterdam, USD/kg
    ("망간", "HP002", "MNRL0004", "815"),        # Mn 75% EXW China, USD/mt
]


def _post(ep: str, data: dict) -> dict:
    r = requests.post(f"{_BASE}/Komis/RsrcPrice/ajax/{ep}", data=data, headers=_H, timeout=15)
    r.raise_for_status()
    return r.json()


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _params(hp: str, code: str, crtr: str, avg: str = "MONTH") -> dict:
    return {"HP000": hp, "srchMnrkndUnqCd": code, "srchPrcCrtr": crtr, "spcfct": "",
            "srchAvgOpt": avg, "srchStartDate": "2010-01-01",
            "srchEndDate": date.today().isoformat(),
            "srchCompareMnrkndUnqCd": "", "srchComparePrcCrtr": "", "lmeInvt": "N"}


@lru_cache(maxsize=2)
def _battery(_day: str) -> dict:
    """배터리·핵심광물 표 — 현재가·전일(없으면 전월) 대비 등락·단위."""
    from concurrent.futures import ThreadPoolExecutor

    def one(m):
        nm, hp, code, crtr = m
        try:
            d = _post("getMnrlPrcByMnrkndUnqCd", _params(hp, code, crtr))
            std = d["dataAvg"]["stdMap"]
            info = std.get("INFO", {})
            day, mon, crt = std.get("DAY") or {}, std.get("MONTH") or {}, std.get("CRTRYMD") or {}
            last = _num(day.get("cmercPrc") if day.get("cmercPrc") is not None else crt.get("cmercPrc"))
            chg = _num(day.get("flctnPrc") if day.get("flctnPrc") is not None else mon.get("flctnPrc"))
            chg_pct = _num(day.get("flctnPrcnt") if day.get("flctnPrcnt") is not None else mon.get("flctnPrcnt"))
            unit = f"{info.get('prcUnitCdNm', 'USD')}/{info.get('weigUnitCd', '')}"
            return {"name": nm, "code": code, "crtr": crtr, "hp": hp,
                    "last": last, "change": chg, "chg_pct": chg_pct, "unit": unit,
                    "basis": info.get("prcCrtr", ""), "date": day.get("crtrYmd") or crt.get("crtrYmd", "")}
        except Exception as e:
            _log.warning("KOMIS %s fetch 실패: %s", nm, e)
            return {"name": nm, "code": code, "crtr": crtr, "hp": hp,
                    "last": None, "change": None, "chg_pct": None, "unit": "", "basis": "", "date": ""}

    with ThreadPoolExecutor(max_workers=len(METALS)) as ex:
        return {"items": list(ex.map(one, METALS))}


@lru_cache(maxsize=64)
def _chart(code: str, crtr: str, hp: str, _day: str) -> dict:
    """선택 광종 월별 시계열(클릭 차트용)."""
    d = _post("getChartData", _params(hp, code, crtr))["data"]
    xs = d.get("xaxis", [])
    prices = (d.get("series") or [{}])[0].get("data", [])
    info = d.get("mnrlInfo", {})
    # MONTH 축 'YYYY.MM' → 'YYYY-MM-01'(풀 날짜) — 프론트 기간 필터/날짜입력과 정합.
    series = [{"date": x.replace(".", "-") + "-01", "close": _num(p)}
              for x, p in zip(xs, prices) if _num(p) is not None]
    return {"series": series, "name": info.get("mnrkndKornNm", ""),
            "unit": f"{info.get('prcUnitCdNm', 'USD')}/{info.get('weigUnitCd', '')}",
            "basis": info.get("prcCrtr", "")}


def battery() -> dict:
    try:
        return _battery(date.today().isoformat())
    except Exception as e:
        _log.warning("KOMIS 배터리금속 fetch 실패: %s", e)
        return {"items": []}


# 광종/가격기준 코드는 화이트리스트(METALS)만 허용 — 임의 입력 차단.
_ALLOWED = {(code, crtr) for _, _, code, crtr in METALS}


def battery_chart(code: str, crtr: str, hp: str) -> dict:
    if (code, crtr) not in _ALLOWED:
        return {"series": [], "name": "", "unit": "", "basis": ""}
    try:
        return _chart(code, crtr, hp, date.today().isoformat())
    except Exception as e:
        _log.warning("KOMIS 차트 fetch 실패 %s/%s: %s", code, crtr, e)
        return {"series": [], "name": "", "unit": "", "basis": ""}
