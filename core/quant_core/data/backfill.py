"""날짜축 역순 cursor 백필 — 단일 문법(대원칙 5: One backfill grammar).

컨센서스(한경)·KRX 공식 API(시장지표/풋콜/ETF/선물패널)가 같은 패턴을 각자 재구현하던
것을 하나로 통일한다: **today→floor 역순으로 window_days씩**, 진행 커서를 텍스트 파일
("%Y%m%d")에 저장, 성공 시에만 전진, floor 도달 시 무비용 no-op.

  - 커서 파일 경로는 호출자가 준다(기존 프로덕션 커서 파일과 100% 호환 — 마이그레이션 0).
  - fetch 자체는 모른다 — 호출자가 next_window()로 창을 받고 성공 시 advance(start).
    실패 시 advance를 안 부르면 같은 창이 다음 청크에 재시도된다(기존 의미 보존).
  - 손상/판독불가 커서는 wedging(영구 재시도 루프) 대신 **fresh(오늘)로 리셋**한다 —
    수집·머지가 멱등이라 재백필은 비용일 뿐 오염이 아니고, 파싱 예외로 청크가 영영
    막히는 것이 더 나쁜 실패다(리셋 사실은 호출자가 로그).

사용:
    bf = DateCursorBackfill(cursor_path=..., floor="20100101", window_days=90)
    win = bf.next_window()
    if win is None: return                    # 백필 완료 — 무비용
    start, end = win                          # date 객체 — 포맷은 호출자 소관
    ok = fetch(start, end)
    if ok: bf.advance(start)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class DateCursorBackfill:
    cursor_path: Path
    floor: str                  # "%Y%m%d" — CORE_FLOOR_COMPACT 또는 소스 자연 floor
    window_days: int

    def _floor_date(self) -> date:
        return datetime.strptime(self.floor, "%Y%m%d").date()

    def read_cursor(self) -> tuple[date, bool]:
        """(커서 날짜, reset 여부). 커서 없음=fresh(오늘). 손상=오늘로 리셋(reset=True)."""
        if self.cursor_path.exists():
            try:
                return (datetime.strptime(self.cursor_path.read_text().strip(),
                                          "%Y%m%d").date(), False)
            except Exception:
                return (date.today(), True)    # 손상 — 멱등 재백필이 wedging보다 낫다
        return (date.today(), False)

    def next_window(self) -> tuple[date, date] | None:
        """다음 역순 창 (start, end). floor 도달이면 None(완료·무비용)."""
        end, _ = self.read_cursor()
        floor = self._floor_date()
        if end <= floor:
            return None
        start = max(floor, end - timedelta(days=self.window_days))
        return (start, end)

    def advance(self, start: date) -> None:
        """성공한 창의 start로 커서 전진 — 실패 시 부르지 않으면 같은 창 재시도."""
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(start.strftime("%Y%m%d"))
