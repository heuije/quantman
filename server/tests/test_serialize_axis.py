"""serialize_ir_result axis 화이트리스트 — 키 유실 부류 가드.

선례: serialize_backtest가 status/verdict를 떨궈 자본부족 verdict가 웹·모델에 미도달(#2).
같은 부류로 axis 경로 화이트리스트가 새 결과 키를 떨구면 조용한 오표기가 된다.
"""
from app.serialize import serialize_ir_result


def test_axis_serialization_keeps_row_axis():
    """WS2 — 코호트 행축(row_axis: 종목/파라미터/조건) 유실 시 웹이 파라미터 격자를
    '종목 코호트'로 오표기한다(실측 — dev-render에서 발견)."""
    res = {"success": True, "axis": "parameter", "shape": "cohort", "row_axis": "파라미터",
           "n_symbols": 2, "windows": ["5"], "buckets": {"a=1": {"name": "a=1", "n_events": 3}}}
    out, kind = serialize_ir_result(res)
    assert kind == "axis" and out["row_axis"] == "파라미터"
