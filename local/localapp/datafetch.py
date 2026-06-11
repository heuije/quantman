"""시세 데이터 수집 — Phase 29부터 서버를 단일 진실 공급원으로 사용.

서버가 cron으로 갱신한 dataset parquet을 tar.zst bundle 단일 다운로드로 pull.
외부 소스(yfinance/FDR/FRED) 직접 호출은 서버에서만 수행 — 모든 사용자가
같은 dataset을 공유해 백테스트와 라이브 매매가 동일 데이터로 동작한다.

견고성: 서버 도달 실패·bundle 미준비(410) 시 마지막 성공 bundle 캐시로 진행
(예외 안 던짐). 진입 후보·방향은 서버 preview가 공급하므로 로컬 dataset의
요구 신선도는 "전일 종가"이고, 이는 직전 성공 bundle에 이미 들어 있다.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("localapp.datafetch")

# Fix A — refresh 직렬화 lock. 기동 시 startup `dataset-initial` 스레드와 catch-up
# cycle의 refresh가 동시에 같은 수천 parquet를 같은 디렉터리에 다운로드·추출하던
# race(2026-05-29 실측)를 차단. 후발 호출은 기다리지 않고 즉시 캐시로 진행한다 —
# 어차피 선행 호출이 같은 bundle을 받고 있으므로 대기·중복 모두 무가치.
_REFRESH_LOCK = threading.Lock()


def refresh_market_data() -> bool:
    """서버 dataset bundle을 pull해 사용자 데이터 디렉터리에 저장한다.

    갱신은 한 번에 하나만 — 다른 스레드가 갱신 중이면 **기다리지 않고** 즉시
    False(기존 캐시로 진행). 이전의 timeout 없는 `with _REFRESH_LOCK:`은 갱신
    보유자가 수 시간 걸릴 때 모든 발주 사이클이 무한·무로그 대기하는 락 컨보이를
    만들었다(2026-06-10 무발주 인시던트 RC-1 — py-spy 실측,
    docs/incidents/2026-06-10-autotrading-week-retrospective.md).

    실패해도(네트워크·서버 다운·410 등) 예외를 던지지 않는다 — 기존 로컬 캐시로
    진행. 반환값: True=이번 호출이 갱신 수행(또는 ETag 304 최신 확인), False=갱신
    못 함(캐시 진행).
    """
    if not _REFRESH_LOCK.acquire(blocking=False):
        log.info("dataset 갱신이 이미 진행 중 — 기존 로컬 캐시로 진행")
        return False
    try:
        return _refresh_market_data_locked()
    finally:
        _REFRESH_LOCK.release()


def _refresh_market_data_locked() -> bool:
    try:
        from quant_core import data_fetcher
        from .sync_client import fetch_dataset_bundle
        result = fetch_dataset_bundle(data_fetcher.DATA_DIR)
        log.info("dataset bundle 사용: %s", result)
        return True
    except Exception as e:
        # bundle 미준비(410)·서버 미도달·총시간 초과 등 — 마지막 성공 bundle
        # 캐시로 진행. manifest 종목별 폴백은 제거됨(2026-06-10 인시던트 D1-2):
        # 수만 parquet 직렬 신선도 검사로 시간 단위(실측 7h+)를 소모하며 발주
        # 사이클을 막았고, 410의 서버측 원인(재배포 후 재패키징 공백)을 은폐했다.
        # 410은 서버가 bundle을 이벤트 기반으로 재생성(PR-α)하면 곧 해소되는
        # 일시 상태 — 다음 dataset cron(08:00/08:20/08:40)이 재시도한다.
        log.warning("dataset bundle 갱신 실패 — 기존 로컬 캐시로 진행: %s", e)
        return False
