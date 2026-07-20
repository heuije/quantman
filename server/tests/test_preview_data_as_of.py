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


def test_dataset_as_of_classifies_domestic_futures_as_kr():
    """N6 — 국내선물(한글 표시심볼)이 US로 오분류돼 KR 키가 아예 안 생기던 결함.

    `_is_kr_symbol`은 '6자리 숫자'만 KR로 인정한다. '코스피200선물'은 isdigit()가
    False라 US 버킷으로 떨어졌고, **선물만 거래하는 유저에게는 data_as_of에 KR 키가
    없어** 로컬의 기준일 게이트(`_preview_stale_reason`)가 fail-open으로 통과했다 —
    안전장치가 구조적으로 무효였다. 같은 부류 버그를 core가 이미
    `instrument_region`으로 한 곳에 모아 뒀는데(exec_defaults docstring이 이 사례를
    명시) `_dataset_as_of`만 그 해결을 안 쓰고 있었다.
    """
    from app.preview_engine import _dataset_as_of

    out = _dataset_as_of({
        "코스피200선물": _df("2026-07-17"),
        "코스닥150선물": _df("2026-07-16"),
        "NVDA": _df("2026-07-15"),
    })
    assert out.get("KR") == "2026-07-17", f"국내선물이 KR로 분류돼야 — 실제 {out}"
    assert out.get("US") == "2026-07-15"


def test_dataset_as_of_futures_only_user_gets_kr_key():
    """선물 전용 유저(주식 0종목) — KR 키가 반드시 생겨야 게이트가 살아난다."""
    from app.preview_engine import _dataset_as_of
    assert _dataset_as_of({"코스피200선물": _df("2026-07-17")}) == {"KR": "2026-07-17"}
