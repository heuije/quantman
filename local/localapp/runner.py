"""모의투자 사이클 오케스트레이션 — 전략 풀 → 평가·매매 → 스냅샷 푸시.

견고성: 플랫폼 연결이 끊겨도 매매는 로컬에서 완료한다.
  - 전략 풀 실패 → 신규 진입 없이 기존 보유분 청산만 평가
  - 스냅샷 푸시 실패 → 보류 큐에 저장, 다음 사이클에 재전송
"""

from __future__ import annotations

import json
import logging

import quant_core as qc

from .broker import Broker, MockBroker
from .config import PENDING_PATH
from .logging_setup import setup_logging
from .sync_client import pull_strategies, push_snapshot
from .trader import Trader

log = logging.getLogger("localapp.runner")


def _price_fn(dataset: dict):
    def fn(symbol: str) -> float:
        df = dataset.get(symbol)
        if df is None or df.empty or "Close" not in df.columns:
            return 0.0
        return float(df["Close"].iloc[-1])
    return fn


def make_broker(use_mock: bool, mock_cash: float = 10_000_000.0,
                dataset: dict | None = None) -> Broker:
    if use_mock:
        return MockBroker(mock_cash, _price_fn(dataset or {}))
    from .kis_broker import KisBroker          # KIS 자격증명 필요 시에만 import
    return KisBroker()


def _flush_pending() -> None:
    """이전 사이클에서 전송 실패한 스냅샷이 있으면 재전송한다."""
    if not PENDING_PATH.exists():
        return
    try:
        payload = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        push_snapshot(payload)
        PENDING_PATH.unlink()
        log.info("보류된 스냅샷 재전송 완료")
    except Exception as e:
        log.warning("보류 스냅샷 재전송 실패 (다음 사이클 재시도): %s", e)


def run_cycle(use_mock: bool = False) -> dict:
    """1회 모의투자 사이클을 실행하고 동기화 스냅샷을 반환한다."""
    setup_logging()
    _flush_pending()

    try:
        strategies = pull_strategies()
        log.info("배정된 전략 %d개", len(strategies))
    except Exception as e:
        log.warning("전략 풀 실패 — 신규 진입 없이 보유분 청산만 평가: %s", e)
        strategies = []

    from .datafetch import refresh_market_data
    refresh_market_data()
    dataset = qc.load_dataset(with_indicators=True)
    broker = make_broker(use_mock, dataset=dataset)
    trader = Trader(broker)
    payload = trader.cycle(strategies, dataset)

    try:
        push_snapshot(payload)
        log.info("동기화 완료 — 평가금액 %s원", f"{payload['balance']['total_eval']:,}")
    except Exception as e:
        PENDING_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                                encoding="utf-8")
        log.warning("동기화 실패 — 보류 큐 저장 (다음 사이클 재전송): %s", e)

    return payload
