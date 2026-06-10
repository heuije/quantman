#!/usr/bin/env python3
"""안 본 것만 catch-up — 세션별 커서 기반 다중 세션 align (이 PC 전용).

세션간 협업 Layer 2의 '읽기' 도구. UserPromptSubmit 훅이 매 작업 시작(유저 쿼리) 시점에
호출하고, 이 출력이 그 턴의 context에 주입돼 Claude가 다른 세션 현황을 본다.

방식: 각 세션(session_id)마다 '마지막으로 본 시각' 커서를 두고, 그 이후에 생긴 다른 세션
브리핑만 보여준다 → 같은 항목 반복 없음 + 나이 무관 절대 안 놓침(catch-up).
  - 세션 식별: UserPromptSubmit 훅이 stdin으로 주는 JSON의 session_id
  - 커서 저장: ~/.claude/briefing-cursors/<session_id>
  - 새 세션(커서 없음): 전체 미열람으로 간주 → 브랜치별 최신 1건 전부 catch-up
  - 내 브랜치(=내 작업)는 제외 / 브랜치별 최신 이벤트로 압축([진행중]=start, [완료]=done)
  - 표시 후 커서를 로그 최신 시각으로 전진 → 다음 턴엔 새 것만

설치: 이 파일을 ~/.claude/hooks/read_briefings.py 로 복사. (docs/COLLABORATION.md 참조)
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
import subprocess
import sys

# Windows 기본 콘솔(cp949)에서도 UTF-8 출력 — Claude Code가 UTF-8로 캡처한다.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

HOME = pathlib.Path.home()
LOG = HOME / ".claude" / "session-briefings.jsonl"
CURSORS = HOME / ".claude" / "briefing-cursors"
MAX_SHOW = 25  # 한 번에 표시할 브랜치 상한(폭주 방지)


def _branch() -> "str | None":
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _session_id() -> str:
    # UserPromptSubmit 훅은 stdin으로 JSON(session_id 포함)을 준다. 수동 실행(tty)이면 스킵.
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                sid = json.loads(raw).get("session_id")
                if sid:
                    return str(sid)
    except Exception:
        pass
    return "_default"


def main() -> None:
    if not LOG.exists():
        return
    sid = _session_id()
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid)[:80] or "_default"
    cursor_file = CURSORS / safe

    cursor = None
    if cursor_file.exists():
        try:
            cursor = datetime.datetime.fromisoformat(cursor_file.read_text(encoding="utf-8").strip())
        except Exception:
            cursor = None

    mine = _branch()
    max_ts = None
    unseen = []  # (ts, record)
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            t = datetime.datetime.fromisoformat(r["ts"])
        except Exception:
            continue  # 손상 줄은 건너뜀
        if max_ts is None or t > max_ts:
            max_ts = t
        if cursor is not None and t <= cursor:
            continue  # 이미 본 것
        if mine and r.get("branch") == mine:
            continue  # 내 작업
        unseen.append((t, r))

    # 커서 전진: 로그 최신 시각으로(표시 여부와 무관) → 다음 턴엔 그 이후만
    CURSORS.mkdir(parents=True, exist_ok=True)
    if max_ts is not None:
        cursor_file.write_text(max_ts.isoformat(), encoding="utf-8")

    if not unseen:
        return

    # 브랜치별 최신 이벤트로 압축(브랜치 수가 자연 상한)
    latest: "dict[str, tuple]" = {}
    for t, r in unseen:
        b = r.get("branch", "")
        if b not in latest or t >= latest[b][0]:
            latest[b] = (t, r)
    rows = sorted(latest.values(), key=lambda x: x[0], reverse=True)
    extra = len(rows) - MAX_SHOW
    rows = rows[:MAX_SHOW]

    label = "전체 catch-up" if cursor is None else "새 소식"
    print(f"=== 다른 세션 작업 현황 ({label}) ===")
    for t, r in rows:
        when = r["ts"][5:10] + " " + r["ts"][11:16]  # MM-DD HH:MM (오래된 항목도 떠 날짜 표기)
        if r.get("event") == "done":
            head = f"  [완료] [{when}] {r.get('branch', '')}: {r.get('intent', '')}"
            if r.get("outcome"):
                head += f" -> {r['outcome']}"
            print(head)
            if r.get("impl"):
                print(f"         구현: {r['impl']}")
            if r.get("files"):
                print(f"         파일: {r['files']}")
        else:
            print(f"  [진행중] [{when}] {r.get('branch', '')}: {r.get('intent', '')}")
            if r.get("plan"):
                print(f"           계획: {r['plan']}")
            if r.get("files"):
                print(f"           파일: {r['files']}")
    if extra > 0:
        print(f"  (외 오래된 {extra}개 브랜치 생략)")


if __name__ == "__main__":
    main()
