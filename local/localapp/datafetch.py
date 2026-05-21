"""시세 데이터 수집 — Phase 29부터 서버를 단일 진실 공급원으로 사용.

서버가 cron으로 갱신한 dataset parquet을 manifest 비교 후 변경분만 pull.
외부 소스(yfinance/FDR/FRED) 직접 호출은 서버에서만 수행 — 모든 사용자가
같은 dataset을 공유해 백테스트와 라이브 매매가 동일 데이터로 동작한다.

견고성: 서버 도달 실패 시 마지막 로컬 캐시로 진행 (예외 안 던짐).
"""

from __future__ import annotations

import logging

log = logging.getLogger("localapp.datafetch")


def refresh_market_data() -> bool:
    """서버 dataset을 pull해 사용자 데이터 디렉터리에 저장한다.

    실패해도(네트워크·서버 다운 등) 예외를 던지지 않는다 — 기존 로컬 캐시로 진행.
    """
    try:
        from quant_core import data_fetcher
        from .sync_client import sync_dataset
        result = sync_dataset(data_fetcher.DATA_DIR)
        log.info("dataset sync 완료: %s", result)
        return True
    except Exception as e:
        log.warning("dataset sync 실패 — 기존 로컬 캐시로 진행: %s", e)
        return False
