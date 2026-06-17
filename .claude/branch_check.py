#!/usr/bin/env python3
"""SessionStart 브랜치 위생 체크 — stale·이미-머지된 브랜치 위에서 작업 시작을 경고.

머지된 브랜치 재사용·옛 base 작업으로 충돌이 재발한 것을 막는 가드.
SessionStart 훅(.claude/settings.json)에서 `git fetch` 직후 호출된다.
어떤 오류든 조용히 통과(fail-open) — 세션 시작을 절대 막지 않는다.
"""
from __future__ import annotations

import subprocess
import sys

REPO = "MercKR/quantman"


def _run(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return r.stdout.strip()
    except Exception:
        return ""


def main() -> None:
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch in ("main", "HEAD"):
        return  # main 또는 detached → 경고할 것 없음

    warn: list[str] = []

    behind = _run(["git", "rev-list", "--count", "HEAD..origin/main"])
    if behind.isdigit() and int(behind) > 0:
        warn.append(f"⚠ 현재 브랜치 '{branch}'가 origin/main보다 {behind}커밋 뒤처짐 — 충돌 위험.")

    merged = _run(["gh", "pr", "list", "--repo", REPO, "--head", branch,
                   "--state", "merged", "--json", "number", "-q", "length"])
    if merged.isdigit() and int(merged) > 0:
        warn.append(f"⛔ '{branch}'는 이미 머지된 브랜치 — 재사용 금지(새 작업은 새 브랜치).")

    if warn:
        print("=== ⚠ 브랜치 위생 경고 (작업 시작 전 확인) ===")
        for w in warn:
            print("  " + w)
        print("  • 새 작업이면:      git checkout main && git pull && git switch -c feat/<새이름>")
        print("  • 현 브랜치 계속이면:  git merge origin/main 으로 최신화 후 진행")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
