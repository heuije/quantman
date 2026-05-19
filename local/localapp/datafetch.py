"""시세 데이터 수집 — 로컬앱이 직접 최신 데이터를 받아온다.

번들된(낡은) parquet를 쓰지 않고 매 사이클 직접 수집한다.
각 사용자의 앱이 스스로 수집하므로 데이터 재배포 문제도 없다.
"""

from __future__ import annotations

import logging

log = logging.getLogger("localapp.datafetch")


def refresh_market_data() -> bool:
    """최신 시세를 수집해 사용자 데이터 디렉터리에 저장한다.

    실패해도(네트워크 등) 예외를 던지지 않는다 — 기존에 받아둔 데이터로 진행한다.
    """
    try:
        from quant_core import data_fetcher
        data_fetcher.fetch_all(verbose=False)
        log.info("시세 데이터 갱신 완료")
        return True
    except Exception as e:
        log.warning("시세 데이터 갱신 실패 — 기존 데이터로 진행: %s", e)
        return False
