"""로드맵 C — preview payload의 data_as_of(시장별 데이터 기준 거래일) 도장.

로컬앱이 후보 사용 직전 "직전 거래일 이상인가"를 검증하는 하한값.
max(그 시장 마지막 봉) 의미 — laggard 종목으로 과차단하지 않고,
매크로(^)·암호화폐(-USD, 24/7)는 시장 세션 기준일과 무관해 제외.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


def _df(*dates):
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)


def test_dataset_as_of_per_market_max():
    from app.preview_engine import _dataset_as_of

    dataset = {
        "005930": _df("2026-07-16", "2026-07-17"),   # KR 최신
        "000660": _df("2026-07-15", "2026-07-16"),   # KR laggard — max에 묻힘
        "NVDA": _df("2026-07-17"),                    # US
        "^VIX": _df("2026-07-18"),                    # 매크로 — 제외
        "BTC-USD": _df("2026-07-18"),                 # 암호화폐(24/7) — 제외
        "EMPTY": _df()[0:0] if False else pd.DataFrame(),  # 빈 df — 무시
    }
    out = _dataset_as_of(dataset)
    assert out == {"KR": "2026-07-17", "US": "2026-07-17"}


def test_dataset_as_of_empty_dataset():
    from app.preview_engine import _dataset_as_of
    assert _dataset_as_of({}) == {}
