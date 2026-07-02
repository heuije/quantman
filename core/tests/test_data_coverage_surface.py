"""구조 가드 — 데이터 커버리지 표면(챗봇 인지 계층)이 데이터 엔진 SSOT와 정합한가.

챗봇(챗 LLM·NL→IR 컴파일러)은 data_type_symbols()·data_spec()를 통해 "무슨 데이터를 얼마나
갖고 있나"를 안다. 데이터 엔진에 새 매크로 심볼(MACRO_KRX_SYMBOLS 등)이 추가됐는데 이 매핑 표에
안 실리면, 그 데이터는 챗봇에게 *보이지 않아* 크로스에셋 참조·커버리지 안내가 불가능해진다
(실측 갭: P2-B macro.krx 8종 공급 후 챗 지식계층 미배선). 이 테스트가 그 표류를 영구 차단한다:

  - 모든 MACRO_SYMBOLS가 data_type_symbols()의 어떤 spec_key에 매핑되는가.
  - data_type_symbols()가 참조하는 모든 spec_key가 data_spec() REGISTRY에 실재하는가.
  - data_type_symbols()의 심볼 union이 ALL_SYMBOLS(내장 심볼) 안에 있는가(오타·유령 심볼 차단).

새 심볼군을 추가하면 data_type_symbols()가 그것을 기술할 때까지 이 테스트가 실패한다.

    cd platform/core && python -m pytest tests/test_data_coverage_surface.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from quant_core import data_fetcher as df
from quant_core.data import data_spec


def test_every_macro_symbol_mapped_to_a_data_type():
    """모든 매크로 심볼이 data_type_symbols()의 어떤 유형키에 매핑돼야 — 챗봇이 그 심볼을
    크로스에셋 참조 가능 심볼로 인지·커버리지 안내할 수 있게(미배선이면 보이지 않는다)."""
    mapped: set[str] = set()
    for syms in df.data_type_symbols().values():
        mapped.update(syms)
    unmapped = set(df.MACRO_SYMBOLS) - mapped
    assert not unmapped, (
        "MACRO_SYMBOLS 중 data_type_symbols() 어느 유형에도 매핑 안 된 심볼 "
        f"— 챗봇에게 보이지 않음:\n  {sorted(unmapped)}\n"
        "→ data_fetcher.data_type_symbols()의 해당 macro.* 유형에 그룹 상수를 추가하세요."
    )


def test_every_asset_symbol_mapped_to_a_data_type():
    """내장 자산 심볼(선물·지수·크립토)도 가격 유형키에 매핑돼야 — 커버리지 인벤토리 누락 방지."""
    mapped: set[str] = set()
    for syms in df.data_type_symbols().values():
        mapped.update(syms)
    # PRICE_ALIAS(미니→정규)는 가격 시리즈를 공유하므로 별도 유형 심볼로 열거하지 않는다(정본=정규).
    unmapped = set(df.ASSET_SYMBOLS) - mapped
    assert not unmapped, (
        f"ASSET_SYMBOLS 중 data_type_symbols() 미매핑:\n  {sorted(unmapped)}"
    )


def test_data_type_keys_exist_in_data_spec():
    """data_type_symbols()가 쓰는 모든 유형키가 data_spec() REGISTRY에 실재해야 — 라벨·소스를
    인벤토리가 spec에서 끌어오므로(유령 키면 KeyError·라벨 결손)."""
    spec_keys = {s["key"] for s in data_spec()}
    used = set(df.data_type_symbols().keys())
    missing = used - spec_keys
    assert not missing, (
        f"data_type_symbols()가 참조하나 data_spec()에 없는 유형키:\n  {sorted(missing)}\n"
        "→ data/spec.py에 등록하거나 매핑 키를 정정하세요."
    )


def test_mapped_symbols_are_real_builtin_symbols():
    """data_type_symbols()의 모든 심볼이 ALL_SYMBOLS(내장 심볼)에 실재 — 오타·유령 심볼 차단."""
    all_syms = set(df.ALL_SYMBOLS)
    for key, syms in df.data_type_symbols().items():
        ghost = set(syms) - all_syms
        assert not ghost, f"{key} 매핑에 ALL_SYMBOLS 밖 심볼(오타?):\n  {sorted(ghost)}"


def test_no_symbol_mapped_to_two_types():
    """한 심볼이 두 유형키에 중복 매핑되지 않아야 — 커버리지 이중집계·라벨 모호 방지."""
    seen: dict[str, str] = {}
    for key, syms in df.data_type_symbols().items():
        for s in syms:
            assert s not in seen, f"심볼 {s!r}가 {seen[s]}·{key} 두 유형에 중복 매핑"
            seen[s] = key


# ── Core floor 정책(2010 통일) 가드 — 대원칙 3(일관 깊이) ─────────────────────

def test_core_specs_declare_core_floor():
    """Core 유형(가격·KR시장지표)의 spec.floor == CORE_FLOOR — 자산군별 깊이 편차가
    백테스트 비교 불공정으로 이어지는 부류를 스펙 계층에서 잠근다."""
    from quant_core.data.policy import CORE_FLOOR
    from quant_core.data import get as spec_get
    for key in ("ohlcv.kr", "ohlcv.us", "ohlcv.futures", "macro.krx"):
        s = spec_get(key)
        assert s is not None, f"{key} spec 미등록"
        assert s.floor == CORE_FLOOR, (
            f"{key}.floor={s.floor!r} ≠ CORE_FLOOR({CORE_FLOOR}) — Core 유형은 통일 floor")


def test_fetch_defaults_use_core_floor():
    """수집 경로의 기본 start/floor가 CORE_FLOOR와 정합 — 하드코딩 리터럴 재유입 차단
    (실측 결함: US backfill_start=2015 → 2010 도달 3%뿐이던 드리프트의 부류)."""
    import inspect
    from quant_core.data.policy import CORE_FLOOR

    def _default(fn, param):
        return inspect.signature(fn).parameters[param].default

    assert _default(df.fetch_yfinance, "start") == CORE_FLOOR
    assert _default(df.fetch_fdr, "start") == CORE_FLOOR
    assert _default(df.fetch_korean_stocks, "start") == CORE_FLOOR
    assert _default(df.fetch_fred, "start") == CORE_FLOOR
    assert _default(df.fetch_managed_overseas, "backfill_start") == CORE_FLOOR
    assert _default(df.backfill_korean_stocks_depth, "floor") == CORE_FLOOR
    assert _default(df.backfill_overseas_depth, "floor") == CORE_FLOOR
