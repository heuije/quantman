"""Phase 2a 회귀 — 라이브 수급·컨센서스 컬럼이 챗봇 reference 단일출처에 표면화되는지.

배경: get_all_indicator_columns()가 BASE+FUND만 반환해, 라이브(main #149) 수급·컨센서스가
챗봇 reference_data·NL 컴파일러 valid-ref에서 누락 → 프롬프트가 "수급 미수급"이라 거짓 고지하던
드리프트(seam #3). 이 테스트가 SSOT 표면화를 잠근다.

    pytest tests/test_indicator_surfacing.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.indicators import (  # noqa: E402
    CONSENSUS_INDICATOR_COLS,
    FLOW_INDICATOR_COLS,
    get_all_indicator_columns,
)


def test_consensus_flow_surfaced_in_reference_ssot():
    cols = set(get_all_indicator_columns())
    for c in FLOW_INDICATOR_COLS:            # 수급(기관·외국인) — 라이브
        assert c in cols, f"수급 컬럼 '{c}'가 reference SSOT에 미표면화"
    for c in CONSENSUS_INDICATOR_COLS:       # 애널 컨센서스
        assert c in cols, f"컨센서스 컬럼 '{c}'가 reference SSOT에 미표면화"
    # 모델이 inspect/screen으로 활용하려면 알아야 하는 대표 컬럼
    assert {"consensus_target", "target_upside",
            "inst_net_buy", "foreign_net_buy"} <= cols
