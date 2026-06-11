"""static.classification 피드 — KR 섹터·업종 (FinanceDataReader KRX-DESC).

fdr.StockListing('KRX-DESC')의 Sector(소속부)·Industry(업종)를 종목코드별 사이드카로 저장한다.
그룹 블록(get_symbol_group)이 하드코딩 휴리스틱 대신 이 사이드카를 읽는다 — 그룹 기본축은 Industry.
US 섹터는 후속(yfinance .info) — 현재 KR만. 미수급 종목은 소비자가 폴백.

사이드카: 가격 parquet·_manifest.json과 같은 디렉터리(_classification.json).
load 의존: json·pathlib뿐. fetch만 FinanceDataReader를 지연 import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..manifest import default_manifest_path

_SIDECAR = "_classification.json"
_cache: Optional[dict] = None
_cache_mtime: Optional[float] = None


def _path() -> Path:
    return default_manifest_path().parent / _SIDECAR


def fetch() -> dict:
    """FDR KRX-DESC에서 KR 종목 Sector·Industry 수급 → 사이드카 저장. 반환 {code: {Sector?, Industry?}}."""
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX-DESC")
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        code = str(r.get("Code") or "").strip()
        if not code:
            continue
        rec: dict = {}
        for col in ("Sector", "Industry"):
            v = r.get(col)
            if isinstance(v, str) and v.strip():     # NaN(float)·빈값 제외
                rec[col] = v.strip()
        if rec:
            out[code] = rec
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def load() -> dict:
    """사이드카 로드(mtime 캐시). cron 갱신 시 자동 재로드. 미수급이면 빈 dict."""
    global _cache, _cache_mtime
    p = _path()
    if not p.exists():
        return {}
    mtime = p.stat().st_mtime
    if _cache is None or mtime != _cache_mtime:
        _cache = json.loads(p.read_text(encoding="utf-8"))
        _cache_mtime = mtime
    return _cache


def symbol_group(sym: str, group_type: str = "Industry") -> Optional[str]:
    """심볼의 산업 분류명. 미수급이면 None.

    KRX-DESC "Sector" 컬럼은 *소속부*(우량기업부·벤처기업부 등 시장구분)지 산업 섹터가
    아니므로 분류로 쓰지 않는다 — 섹터/업종 모두 Industry(KSIC 업종)를 진실원천으로 답한다.
    group_type은 호환을 위해 받되 현재 둘 다 Industry. 표준 섹터("반도체 제조업"→"반도체")
    정규화는 후속(Industry→표준섹터 매핑)에서 group_type="Sector" 분기로 추가한다.
    """
    rec = load().get(sym.split(".")[0])
    if not rec:
        return None
    return rec.get("Industry")
