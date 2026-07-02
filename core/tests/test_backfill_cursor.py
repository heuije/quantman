"""DateCursorBackfill — 날짜축 역순 cursor 백필 단일 문법 단위 테스트.

컨센서스(한경)·KRX 공식 API 백필이 공유하는 커서 규약: today→floor 역순 window,
성공 시만 전진, floor 도달 시 None(무비용), 손상 커서는 fresh 리셋(wedging 방지).
"""

from __future__ import annotations

from datetime import date, timedelta

from quant_core.data.backfill import DateCursorBackfill


def _bf(tmp_path, floor="20100101", window_days=90):
    return DateCursorBackfill(cursor_path=tmp_path / "x.cursor",
                              floor=floor, window_days=window_days)


def test_fresh_cursor_starts_today(tmp_path):
    bf = _bf(tmp_path)
    start, end = bf.next_window()
    assert end == date.today()
    assert start == end - timedelta(days=90)


def test_advance_persists_and_resumes(tmp_path):
    bf = _bf(tmp_path)
    start, _ = bf.next_window()
    bf.advance(start)
    # 새 인스턴스(재배포 시뮬레이션)가 커서에서 이어받는다.
    bf2 = _bf(tmp_path)
    start2, end2 = bf2.next_window()
    assert end2 == start                     # 이전 창의 start = 다음 창의 end
    assert start2 == start - timedelta(days=90)


def test_floor_reached_returns_none(tmp_path):
    bf = _bf(tmp_path)
    bf.advance(date(2010, 1, 1))             # floor 도달
    assert bf.next_window() is None          # 완료 — 무비용


def test_window_clamped_to_floor(tmp_path):
    bf = _bf(tmp_path)
    bf.advance(date(2010, 2, 1))             # floor까지 31일 남음 < window 90일
    start, end = bf.next_window()
    assert start == date(2010, 1, 1)         # floor로 클램프
    assert end == date(2010, 2, 1)


def test_corrupted_cursor_resets_fresh(tmp_path):
    """손상 커서 = fresh(오늘) 리셋 — 멱등 재백필이 파싱 예외로 영영 막히는 것보다 낫다."""
    bf = _bf(tmp_path)
    bf.cursor_path.write_text("not-a-date")
    cur, reset = bf.read_cursor()
    assert reset is True and cur == date.today()
    start, end = bf.next_window()            # 예외 없이 진행
    assert end == date.today()
