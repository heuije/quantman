"""챗봇 LLM 무료 평가 하니스 — CLI 진입점.

전제: `claude setup-token`으로 발급한 구독 토큰을 CLAUDE_CODE_OAUTH_TOKEN 환경변수(또는
~/.claude/oauth_token.txt)에 둔다. API 비용 0(Pro/Max 구독 쿼터). 설계서: docs/REDESIGN/chat-llm-eval-harness.md

  python scripts/chat_eval.py                 # run + grade (기본)
  python scripts/chat_eval.py run  [--only X]  # 시나리오 실행 → 트랜스크립트(chat_eval_out/)
  python scripts/chat_eval.py grade [--only X] [--no-judge]   # 채점 → REPORT.md
  python scripts/chat_eval.py --smoke "질문"   # 단일 쿼리 스모크(코퍼스·채점 불요)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "chat_eval"))


def main() -> int:
    ap = argparse.ArgumentParser(description="챗봇 LLM 평가 하니스")
    ap.add_argument("cmd", nargs="?", default="all", choices=["run", "grade", "all"])
    ap.add_argument("--only", help="시나리오 이름 부분일치 필터")
    ap.add_argument("--no-judge", action="store_true", help="LLM-심판 생략(결정적 채점만)")
    ap.add_argument("--smoke", help="단일 쿼리 스모크 실행(코퍼스 우회)")
    args = ap.parse_args()

    import run as runner
    if args.smoke:
        runner.run_all([{"name": "smoke", "turns": [args.smoke], "expect": {}}])
        return 0
    if args.cmd in ("run", "all"):
        runner.run_all(runner.load_corpus(args.only))
    if args.cmd in ("grade", "all"):
        import grade
        grade.grade_all(only=args.only, judge=not args.no_judge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
