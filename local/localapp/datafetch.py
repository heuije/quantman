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

    Phase 58-C — 우선 tar.zst bundle 단일 다운로드 시도(~1분). server 미지원
    또는 packaging 미완료(410)면 manifest 종목별 다운로드(~114분)로 폴백.

    실패해도(네트워크·서버 다운 등) 예외를 던지지 않는다 — 기존 로컬 캐시로 진행.
    """
    try:
        from quant_core import data_fetcher
        from .sync_client import fetch_dataset_bundle, sync_dataset
        try:
            result = fetch_dataset_bundle(data_fetcher.DATA_DIR)
            log.info("dataset bundle 사용: %s", result)
            return True
        except ValueError as e:
            # bundle 미준비 — manifest fallback
            log.info("bundle 폴백 (%s) — manifest 경로 사용", e)
            result = sync_dataset(data_fetcher.DATA_DIR)
            log.info("dataset sync 완료: %s", result)
            return True
    except Exception as e:
        log.warning("dataset sync 실패 — 기존 로컬 캐시로 진행: %s", e)
        return False
