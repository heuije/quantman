"""로컬앱 GUI 타임라인 라벨이 서버 타임라인 kind 전체를 덮는지 — 드리프트 가드.

GUI KIND_LABEL는 웹 TradingTimeline.tsx KIND_LABEL와 동일 kind 집합이어야 한다.
누락되면 _row가 raw kind 코드를 그대로 표시한다(2026-06-25 종가청산 3종 누락으로
'krx_close_stock'·'krx_close_futures'·'us_close' raw 노출 — 로컬 맵이 서버 kind에서
드리프트). 서버 trading.py가 emit하는 kind를 GUI가 빠짐없이 라벨링하는지 잠근다.
"""
import inspect

# 서버 trading.py 타임라인이 emit하는 kind 전체 (= 웹 KIND_LABEL 집합).
SERVER_TIMELINE_KINDS = {
    "krx_preview", "krx_cycle", "krx_close_stock", "krx_close_futures",
    "krx_settlement", "us_preview", "us_cycle", "us_close", "us_settlement",
}


def _gui_kind_label() -> dict:
    from localapp import gui
    cls = next(c for _, c in inspect.getmembers(gui, inspect.isclass)
               if "KIND_LABEL" in vars(c))
    return cls.KIND_LABEL


def test_gui_kind_label_covers_all_server_kinds():
    """GUI KIND_LABEL가 서버 kind 전체를 덮는다(누락 시 raw 코드 노출)."""
    missing = SERVER_TIMELINE_KINDS - set(_gui_kind_label())
    assert not missing, f"GUI KIND_LABEL 누락 kind(타임라인서 raw 코드 노출): {missing}"


def test_gui_kind_labels_are_korean_not_raw():
    """라벨이 raw kind 코드가 아니라 한글 표시다(폴백 노출 방지)."""
    m = _gui_kind_label()
    for kind in SERVER_TIMELINE_KINDS:
        assert m.get(kind) and m[kind] != kind, f"{kind} 라벨이 raw/누락: {m.get(kind)!r}"
