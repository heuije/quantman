#!/usr/bin/env python3
"""최근 24h 다른 세션 브리핑을 출력 — 다중 세션 align (이 PC 전용).

세션간 협업 Layer 2의 '읽기' 도구. UserPromptSubmit 훅이 매 작업 시작(유저 쿼리)
시점에 호출하고, 이 출력이 그 턴의 context에 주입돼 Claude가 다른 세션 현황을 본다.

규칙:
  - 24h 이내 이벤트만
  - 내 브랜치(=내 작업)는 제외
  - 브랜치별 최신 이벤트만 압축 ([진행중] = start / [완료] = done)

설치: 이 파일을 ~/.claude/hooks/read_briefings.py 로 복사. (docs/COLLABORATION.md 참조)
"""
from __future__ import annotations

import datetime
import json
import pathlib
import subprocess
import sys

# Windows 기본 콘솔(cp949)에서도 UTF-8 출력 — Claude Code가 UTF-8로 캡처한다.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

WINDOW_H = 24
LOG = pathlib.Path.home() / ".claude" / "session-briefings.jsonl"


def _branch() -> "str | None":
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def main() -> None:
    if not LOG.exists():
        return
    now = datetime.datetime.now().astimezone()
    cutoff = now - datetime.timedelta(hours=WINDOW_H)
    mine = _branch()

    latest: "dict[str, dict]" = {}
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            t = datetime.datetime.fromisoformat(r["ts"])
        except Exception:
            continue  # 손상 줄은 건너뜀
        if t < cutoff:
            continue
        if mine and r.get("branch") == mine:
            continue
        prev = latest.get(r["branch"])
        if prev is None or r["ts"] >= prev["ts"]:
            latest[r["branch"]] = r  # 같은 브랜치는 최신 이벤트로 (start->done)

    if not latest:
        return

    print("=== 다른 세션 작업 현황 (최근 24h) ===")
    for r in sorted(latest.values(), key=lambda x: x["ts"], reverse=True):
        when = r["ts"][11:16]
        if r.get("event") == "done":
            head = f"  [완료] [{when}] {r['branch']}: {r.get('intent', '')}"
            if r.get("outcome"):
                head += f" -> {r['outcome']}"
            print(head)
            if r.get("impl"):
                print(f"         구현: {r['impl']}")
            if r.get("files"):
                print(f"         파일: {r['files']}")
        else:
            print(f"  [진행중] [{when}] {r['branch']}: {r.get('intent', '')}")
            if r.get("plan"):
                print(f"           계획: {r['plan']}")
            if r.get("files"):
                print(f"           파일: {r['files']}")


if __name__ == "__main__":
    main()
