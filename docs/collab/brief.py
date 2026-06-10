#!/usr/bin/env python3
"""작업 브리핑을 공유 로그에 기록 — 다중 세션 align (이 PC 전용).

세션간 협업 Layer 2의 '쓰기' 도구. 한 작업의 생애 두 시점을 기록한다:
  python brief.py start --intent "..." --plan "..." --files "..."
  python brief.py done  --intent "..." --plan "..." --files "..." --impl "..." --outcome "..."

로그: ~/.claude/session-briefings.jsonl  (한 줄 = 한 이벤트, append-only)
설치: 이 파일을 ~/.claude/hooks/brief.py 로 복사. (docs/COLLABORATION.md 참조)
"""
from __future__ import annotations

import argparse
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

LOG = pathlib.Path.home() / ".claude" / "session-briefings.jsonl"


def _branch() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out or "(no-branch)"
    except Exception:
        return "(no-branch)"


def main() -> None:
    p = argparse.ArgumentParser(description="공유 브리핑 로그 기록 (start/done)")
    p.add_argument("event", choices=["start", "done"])
    p.add_argument("--intent", default="",
                   help="self-contained 핸드오프 2~3문장: 무엇을·왜·배경·범위경계 (한 구절 금지)")
    p.add_argument("--plan", default="", help="어떻게 - 핵심 단계(start) / 실제 한 것(done)")
    p.add_argument("--files", default="", help="건드릴/변경한 파일·영역")
    p.add_argument("--impl", default="", help="[done] 구현·결정 요지")
    p.add_argument("--outcome", default="", help="[done] 어떻게 끝났나(머지/PR/보류/중단)")
    a = p.parse_args()

    rec = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": a.event,
        "branch": _branch(),
        "cwd": pathlib.Path.cwd().name,
        "intent": a.intent,
        "plan": a.plan,
        "files": a.files,
    }
    if a.event == "done":
        rec["impl"] = a.impl
        rec["outcome"] = a.outcome

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[brief:{a.event}] {rec['branch']} - {a.intent}")


if __name__ == "__main__":
    main()
