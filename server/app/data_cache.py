"""데이터셋 메모리 캐시.

parquet 로딩 + 지표 계산은 비용이 크므로 프로세스 수명 동안 1회만 수행한다.
데이터는 하루 1회 갱신되므로 프로세스 캐시로 충분하다.

**캐시 일관성**: 캐시는 빌드 시점의 데이터 세대(data_fetcher.data_generation())를
기억하고, 읽기 시 throttled로 디스크 세대를 확인해 바뀌었으면 리로드한다. cron은
in-process라 invalidate()로 즉시 무효화되지만, manage·백필 등 **별도 프로세스**가
데이터를 바꾸면 세대 토큰(공유 파일)으로 라이브 서버가 자가 감지한다. 우회한 수동
변경은 admin invalidate 엔드포인트로 강제.
"""

from __future__ import annotations

import logging
import threading
import time

import pandas as pd
import quant_core as qc
from quant_core import data_fetcher
from quant_core.indicators import (FUND_INDICATOR_COLS, CONSENSUS_INDICATOR_COLS,
                                    FLOW_INDICATOR_COLS, compute_columns)

_log = logging.getLogger("app.data_cache")
_lock = threading.Lock()
_dataset: dict[str, pd.DataFrame] | None = None
# 원시 OHLCV 캐시(지표 계산 0) — 컬럼 프로젝션의 공유 입력. full _dataset(45컬럼,
# ~9.4GB) 대신 raw(~1.3GB)만 상주시키고 get_projected가 필요 지표만 그때그때 계산한다.
_raw: dict[str, pd.DataFrame] | None = None
_manifest = None        # DataManifest — 실측 메타(무결성 게이트 입력). dataset과 수명 동일.
# 경량 심볼 인덱스(parquet 메타만, 지표계산 없음) — /symbols·참조검증용. 컴퓨티드
# dataset(8.5분)과 분리해 종목 목록 응답이 지표계산에 묶이지 않게 한다.
_symbol_index: dict[str, dict] | None = None
# dataset이 바뀔 때마다 증가. /symbols 등 dataset 파생 응답 캐시의 키로 쓴다.
_version: int = 0
# 캐시를 빌드한 시점의 데이터 세대. 디스크/다른 프로세스가 데이터를 바꾸면 달라진다.
_built_generation: int | None = None
_last_check: float = 0.0
_CHECK_INTERVAL = 10.0          # 세대 확인 최소 간격(초) — 읽기당 파일 stat 1회로 제한


def _clear_locked() -> None:
    """캐시 폐기 + 파생 캐시 키(_version) bump. 호출자가 _lock 보유 가정."""
    global _dataset, _raw, _manifest, _symbol_index, _version, _built_generation
    _dataset = None
    _raw = None
    _manifest = None
    _symbol_index = None
    _version += 1
    _built_generation = None


def _maybe_reload() -> None:
    """디스크 데이터 세대가 빌드 시점과 다르면 캐시 폐기(throttled). manage·백필·cron
    (별도 프로세스)이나 수동 변경이 바꾼 데이터를 라이브 서버가 자가 감지해 리로드한다."""
    global _last_check
    if (_dataset is None and _raw is None and _manifest is None
            and _symbol_index is None):
        return                  # 빌드된 캐시 없음 — 검증 불필요
    now = time.monotonic()
    if now - _last_check < _CHECK_INTERVAL:
        return
    _last_check = now
    if _built_generation is not None and data_fetcher.data_generation() != _built_generation:
        with _lock:
            if (_built_generation is not None
                    and data_fetcher.data_generation() != _built_generation):
                _clear_locked()


def get_dataset() -> dict[str, pd.DataFrame]:
    global _dataset, _built_generation
    _maybe_reload()
    if _dataset is None:
        with _lock:
            if _dataset is None:
                # 로드 시작 시점 세대를 기록 — 로드 중 변경이 있으면 다음 검증에서 재리로드.
                _built_generation = data_fetcher.data_generation()
                _dataset = qc.load_dataset(with_indicators=True)
    return _dataset


def get_raw_dataset() -> dict[str, pd.DataFrame]:
    """원시 OHLCV dict(지표 계산 0) — 컬럼 프로젝션의 공유 입력.

    full compute_all(45컬럼, ~9.4GB) 대신 raw(~1.3GB)만 상주시킨다. 소비처는
    get_projected로 전략이 실제 참조하는 지표만 그때그때 계산한다(메모리 근본 절감).
    dataset과 같은 세대 마커(_built_generation)로 데이터 변경 시 함께 무효화된다.
    """
    global _raw, _built_generation
    _maybe_reload()
    if _raw is None:
        with _lock:
            if _raw is None:
                if _built_generation is None:
                    _built_generation = data_fetcher.data_generation()
                _raw = qc.load_dataset(with_indicators=False)
    return _raw


def get_projected(columns, symbols=None, recent_days=None) -> dict[str, pd.DataFrame]:
    """요청 지표 컬럼만 계산한 dataset(컬럼 프로젝션). symbols=None이면 전 유니버스.

    raw 공유 캐시에서 compute_columns로 그때그때 계산하며 **상주시키지 않는다**(호출자가
    쓰고 버림 → 피크 메모리 = raw + 진행 중 1건). 반환값의 요청 컬럼은 compute_all(전체)과
    **byte 동일**(quant_core.compute_columns의 순수성 — test_compute_columns·골든 불변식).
    fund 지표가 요청되면 펀더멘털을 1회 로드해 attach(아니면 로드 안 함 → 가벼움).

    recent_days=N이면 각 종목 raw를 **최근 N행으로 잘라** 계산한다 — SELECT(as-of 스냅샷)는
    최신 단면 1행만 쓰므로 전체이력×전종목 프로젝션(전 유니버스 OOM 원인)을 회피한다. N이
    신호 룩백(예: 200일MA·252모멘텀)보다 크면 **최신 행 값은 full 계산과 동일**(룩백 충족).
    ⚠ 잘린 tail의 *앞쪽* 행은 룩백 부족으로 full과 다를 수 있어, 최신 단면만 쓰는 SELECT
    전용이다(백테스트·분포 등 전체이력이 필요한 경로엔 recent_days 금지).
    """
    _t0 = time.monotonic()
    raw = get_raw_dataset()
    _t_raw = time.monotonic()
    cols = set(columns)
    want_fund = bool(cols & set(FUND_INDICATOR_COLS))
    want_cons = bool(cols & set(CONSENSUS_INDICATOR_COLS))
    want_flow = bool(cols & set(FLOW_INDICATOR_COLS))
    funds = data_fetcher.load_fund_all() if want_fund else {}
    cons = data_fetcher.load_consensus_all() if want_cons else {}
    flow = data_fetcher.load_flow_all() if want_flow else {}
    _t_aux = time.monotonic()
    keys = list(raw.keys()) if symbols is None else [s for s in symbols if s in raw]
    out: dict[str, pd.DataFrame] = {}
    for s in keys:
        df = raw[s]
        if df is None or df.empty:
            continue
        if recent_days is not None and len(df) > recent_days:
            df = df.tail(int(recent_days))
        fd = funds.get(s) if want_fund else None
        cd = cons.get(s) if want_cons else None
        fl = flow.get(s) if want_flow else None
        out[s] = compute_columns(df, cols,
                                 fd if (fd is not None and not fd.empty) else None,
                                 cd if (cd is not None and not cd.empty) else None,
                                 fl if (fl is not None and not fl.empty) else None)
    # 계측(뿌리④) — 스크리닝 3-10분 병목이 raw 콜드로드/보조데이터/compute 중 어디인지 분해.
    _end = time.monotonic()
    _log.info("get_projected: n=%d cols=%d recent_days=%s raw=%.2fs aux=%.2fs compute=%.2fs total=%.2fs",
              len(out), len(cols), recent_days, _t_raw - _t0, _t_aux - _t_raw, _end - _t_aux, _end - _t0)
    return out


def get_symbol_index() -> dict[str, dict]:
    """경량 심볼 인덱스 {sym: {rows, has_ohlc}} — parquet 메타만, 지표계산 없음.

    종목 목록(/symbols)·참조 검증(/ir/validate)이 컴퓨티드 dataset(8.5분)에 묶이지
    않게 분리. dataset과 같은 세대 마커로 일관성 유지(데이터 변경 시 함께 무효화)."""
    global _symbol_index, _built_generation
    _maybe_reload()
    if _symbol_index is None:
        with _lock:
            if _symbol_index is None:
                if _built_generation is None:
                    _built_generation = data_fetcher.data_generation()
                _symbol_index = data_fetcher.dataset_symbol_index()
    return _symbol_index


def get_version() -> int:
    """현재 dataset 버전. invalidate()·세대 변경 리로드마다 증가하므로 파생 캐시 무효화에 쓴다."""
    return _version


def invalidate() -> None:
    """캐시된 dataset·매니페스트를 비우고 데이터 세대 마커를 bump한다.

    다음 호출 시 parquet에서 재로드·재빌드한다. 마커 bump로 **다른 프로세스/레플리카**의
    캐시도 다음 읽기에서 자가 무효화된다(이 프로세스만이 아니라 시스템 전체 일관성)."""
    with _lock:
        _clear_locked()
    data_fetcher.mark_data_dirty()
