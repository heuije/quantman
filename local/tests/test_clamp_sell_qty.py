"""REV-②(b) — 매도 수량 클램프 단일 출처(clamp_sell_qty) 계약.

EOD cycle 청산·장중 tick 손절이 같은 규칙으로 KIS 실 보유에 맞춰 매도 수량을
제한하는지(over-sell 방지)를 한 함수로 못박는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def test_clamp_sell_qty_contract():
    from localapp.trader import clamp_sell_qty
    assert clamp_sell_qty(None, 10) is None      # 잔고 미상 → skip(재시도)
    assert clamp_sell_qty(0, 10) == 0            # 외부 매도(보유 0) → skip
    assert clamp_sell_qty(-3, 10) == 0           # 음수도 0 취급
    assert clamp_sell_qty(4, 10) == 4            # 보유 < 원장 → 클램프
    assert clamp_sell_qty(10, 10) == 10          # 동일
    assert clamp_sell_qty(20, 10) == 10          # 보유 > 원장 → 원장(초과 매도 안 함)
