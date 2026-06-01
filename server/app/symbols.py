"""종목 매매가능(tradable) 판정 — 단일 진실원천.

빌더 종목목록(/symbols)과 모의·실전 승격 게이트가 **같은 기준**으로 tradable을
판정해야 UI와 서버가 어긋나지 않는다(사용자가 빌더에서 tradable로 본 종목을
서버가 거부하면 신뢰가 깨진다). 그 기준을 여기 한 곳에 둔다.

tradable 기준(=`_build_symbols_payload`의 `tradable=True`와 동일):
  1) KIS 마스터에 존재(자동매매 주문 가능) — 그리고
  2-a) 데이터 인덱스에 OHLC 보유, **또는**
  2-b) 마스터에만 있고 데이터 없는 종목(라이브 매매만; 단 미국 NAS/NYS/AMS는 제외).

클래스주 심볼로지: 데이터셋은 대시(BRK-B), 마스터는 슬래시(BRK/B)를 쓰므로
정규화 조회(`-`→`/`)로 매칭한다(안 하면 Berkshire 등이 비매매로 오판정).
"""

from __future__ import annotations

from . import data_cache, kis_master_cache

# 미국 시장: 데이터 보유분만 selectable로 노출(데이터 없는 ~1만+ 종목 제외).
# `_build_symbols_payload` §4.8의 마스터-only 분기와 동일 규칙.
_US_MARKETS = ("NAS", "NYS", "AMS")


def tradable_symbols() -> set[str]:
    """자동매매 가능 종목코드 집합. `_build_symbols_payload`의 tradable 기준과 일치.

    마스터·인덱스 캐시에서 빌드(전체 지표계산 없음 → 경량). 마스터가 비어 있으면
    (네트워크 미수신) 빈 집합을 반환한다 — 이 경우 게이트는 모든 종목을 거부하므로
    호출부는 마스터 로드를 보장해야 한다(프로덕션 lifespan에서 refresh됨).
    """
    index = data_cache.get_symbol_index()              # {sym: {rows, has_ohlc}}
    master_by_code = {m["symbol"]: m for m in kis_master_cache.get_master_list()}

    out: set[str] = set()

    # 1) 데이터 인덱스 종목 — 마스터에도 있으면(정규화 조회 포함) tradable.
    for sym, meta in index.items():
        if not meta["has_ohlc"]:
            continue
        if sym in master_by_code or sym.replace("-", "/") in master_by_code:
            out.add(sym)

    # 2) 마스터에만 있고 데이터 없는 종목 — 라이브 매매만(미국 제외).
    for code, m in master_by_code.items():
        if code in out:
            continue
        if m.get("market") in _US_MARKETS:
            continue
        out.add(code)

    return out
