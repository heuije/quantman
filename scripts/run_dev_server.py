"""$0 로컬 챗 서버 — 구독 shim으로 NL→IR·agent LLM을 claude -p로 (테스트 환경 Part B2).

라이브 FastAPI 서버(app.main:app)를 띄우되 `anthropic.Anthropic`을 ClaudeCodeBackend
(chat_eval 구독 shim)로 몽키패치 → compile_nl·에이전트가 **유료 API 대신 claude -p 구독($0)**
을 쓴다. QP_CORE_DATA_DIR=dev-data(Part A)로 **프로덕션 데이터**. 프로덕션 서버 코드는
무변경 — 이 dev 진입점만 shim을 끼운다(uvicorn reload=False라 단일 프로세스에 패치 유지).

전제:
  1. Part A로 데이터 pull:  python scripts/pull_prod_data.py
  2. 구독 토큰:            claude setup-token  (→ CLAUDE_CODE_OAUTH_TOKEN 또는 ~/.claude/oauth_token.txt)

사용:
  python scripts/run_dev_server.py --port 8010
  그 뒤 웹 dev를 이 서버로 물려(web/.env.local 의 VITE_API_BASE=http://localhost:8010)
  Chrome에서 로그인 → 챗에 자연어 → NL→IR→엔진(실데이터)→차트, 전부 API $0.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))
sys.path.insert(0, str(_ROOT / "core"))
sys.path.insert(0, str(_ROOT / "scripts" / "chat_eval"))


def main() -> int:
    ap = argparse.ArgumentParser(description="$0 로컬 챗 서버(구독 shim)")
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()

    # 구독 토큰 — 없으면 파일 폴백(chat_eval과 동일)
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        f = Path.home() / ".claude" / "oauth_token.txt"
        if f.exists():
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "".join(f.read_text(encoding="utf-8").split())
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("✗ CLAUDE_CODE_OAUTH_TOKEN 없음 — 'claude setup-token' 후 재실행", file=sys.stderr)
        return 2

    # compile_nl 존재가드 통과용 더미(실인증=shim). 데이터·빠른 기동.
    os.environ.setdefault("ANTHROPIC_API_KEY", "dev-shim")
    os.environ.setdefault("QP_CORE_DATA_DIR", str(Path.home() / ".quant-platform" / "dev-data"))
    os.environ.setdefault("QP_SKIP_STARTUP_JOBS", "1")   # cron·백필 skip → 즉시 기동(Mode B)

    import anthropic
    from backend import ClaudeCodeBackend
    anthropic.Anthropic = ClaudeCodeBackend   # NL→IR·agent seam → 구독($0)
    print(f"✓ LLM shim 활성(claude -p·API $0) · 데이터 {os.environ['QP_CORE_DATA_DIR']}")
    print(f"✓ http://127.0.0.1:{args.port} 기동 — 웹 dev를 VITE_API_BASE로 물리면 로컬 챗 $0")

    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
