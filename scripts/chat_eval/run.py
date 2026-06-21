"""챗봇 LLM 평가 러너 — 로컬 SQLite + Claude Code shim 백엔드 + 합성 데이터 주입으로
코퍼스 시나리오를 *실제* run_chat_turn에 흘려 트랜스크립트를 저장한다.

데이터: 엔진이 결과 형상·요약·시각화를 만들어내도록 합성 OHLCV+팩터를 `tools._load_dataset`
seam에 주입한다(실데이터 불요·결정적·$0). 평가 대상인 LLM 거동(라우팅·해석·시각화중복)은
데이터 내용 realism과 무관하므로 합성으로 충분 — 실데이터 충실도는 프로덕션에서(설계서 §11).
영속(run_chat_turn._persist)은 로컬 SQLite에 남아 로컬 웹앱에서 실제 차트로 검수 가능(§8).

실행: scripts/chat_eval.py CLI가 호출. CLAUDE_CODE_OAUTH_TOKEN 필요(setup-token·§9).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # _wt-chat-eval
OUT = ROOT / "chat_eval_out"
_HERE = Path(__file__).resolve().parent


def bootstrap() -> None:
    """워크트리 core/server를 sys.path 최우선(stale platform/core override) + 더미 키 + 토큰 해석."""
    for p in ("core", "server", "core/tests"):
        sp = str(ROOT / p)
        if sp not in sys.path:
            sys.path.insert(0, sp)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))             # backend.py · corpus.py
    # compile_nl 존재가드 통과용 더미(shim이 서브프로세스 env에서 제거 → 실인증=OAuth).
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-eval-dummy-ignored-by-shim")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        f = Path.home() / ".claude" / "oauth_token.txt"   # 개발 편의 폴백(.gitignore)
        if f.exists():
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "".join(f.read_text(encoding="utf-8").split())


# ── 합성 데이터 주입 (엔진이 결과를 내도록) ───────────────────────────────────

_SYNTH_UNIVERSE = [f"{i:06d}" for i in range(1, 25)]   # all/screener용 가상 유니버스 24종
_SECTORS = ["반도체", "자동차", "화학", "금융", "바이오"]


def _synth_df(sym: str, i: int):
    import analysis_corpus as AC                  # core/tests — 검증된 합성 빌더 재사용
    n = 400
    seed = sum(ord(c) for c in sym) % 9999        # 결정적(PYTHONHASHSEED 무관)
    drift = 0.0009 - 0.0005 * (i % 3)
    close = AC._noisy(n, vol=0.013, seed=seed, drift=drift)
    extra = {"pb_ratio": round(0.6 + 0.35 * (i % 6), 2),
             "trailing_pe": round(7 + 2.5 * (i % 6), 1),
             "ev_ebitda": round(5 + 1.5 * (i % 5), 1),
             "market_cap": 5e11 * (1 + i % 7),
             "momentum_12_1m": round(0.5 + (i % 4) * 0.4, 2)}
    df = AC._ohlc(close, extra)
    df["sector"] = _SECTORS[i % len(_SECTORS)]
    return df


def synth_load_dataset(ir: dict) -> dict:
    """tools._load_dataset 대체 — IR 참조 심볼(또는 all이면 가상 유니버스)에 합성 데이터."""
    from quant_core.ir_engine import StrategyIR, needed_symbols
    try:
        sir = StrategyIR.model_validate(ir)
    except Exception:                             # noqa: BLE001 — 파싱불가는 엔진 검증경로가 처리
        return {}
    syms = needed_symbols(sir) or _SYNTH_UNIVERSE
    return {s: _synth_df(s, i) for i, s in enumerate(syms)}


# ── 세션·시나리오 실행 ────────────────────────────────────────────────────────

def _ensure_user(session) -> int:
    from sqlmodel import select
    from app.models import User
    u = session.exec(select(User).where(User.email == "chat-eval@local")).first()
    if u is None:
        u = User(email="chat-eval@local")
        session.add(u)
        session.commit()
        session.refresh(u)
    return u.id


def run_scenario(session, user_id: int, scenario: dict) -> dict:
    """시나리오의 멀티턴을 새 대화에서 실제 run_chat_turn으로 실행 → 트랜스크립트."""
    from app.models import Conversation
    from app.chat.agent import run_chat_turn
    from backend import ClaudeCodeBackend

    conv = Conversation(user_id=user_id, title=f"[eval] {scenario['name']}")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    turns: list[dict] = []
    for user_text in scenario["turns"]:
        parts = run_chat_turn(session, conv.id, user_text, client=ClaudeCodeBackend())
        turns.append({"user": user_text, "parts": parts})
    return {"name": scenario["name"], "conversation_id": conv.id,
            "expect": scenario.get("expect", {}), "turns": turns}


def run_all(scenarios: list[dict]) -> list[Path]:
    """시나리오 목록 실행 → chat_eval_out/<name>.json 저장. 저장된 트랜스크립트 경로 목록 반환."""
    bootstrap()
    import anthropic
    import app.chat.tools as tools
    from backend import ClaudeCodeBackend
    from sqlmodel import Session, SQLModel

    anthropic.Anthropic = ClaudeCodeBackend        # compile_nl·news 다이제스트 seam
    tools._load_dataset = synth_load_dataset       # 데이터 seam(합성 주입)

    from app.db import engine                       # 서버와 동일 DB(로컬 SQLite) → §8 viewing 공유
    SQLModel.metadata.create_all(engine)
    OUT.mkdir(exist_ok=True)
    written: list[Path] = []
    with Session(engine) as s:
        uid = _ensure_user(s)
        for sc in scenarios:
            print(f"  ▶ {sc['name']} …", flush=True)
            try:
                t = run_scenario(s, uid, sc)
            except Exception as e:                  # noqa: BLE001 — 한 시나리오 실패가 전체를 막지 않게
                s.rollback()                        # 실패한 커밋 후 세션 복구(다음 시나리오 보호)
                t = {"name": sc["name"], "error": f"{type(e).__name__}: {e}",
                     "expect": sc.get("expect", {}), "turns": []}
                print(f"    ⚠ 실패: {e}")
            fp = OUT / f"{sc['name']}.json"
            fp.write_text(json.dumps(t, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
            written.append(fp)
            print(f"    ✓ → {fp.name}", flush=True)
    import backend as _backend                     # 쿼터 집계(REPORT 토큰 요약)
    (OUT / "_usage_run.json").write_text(
        json.dumps(_backend.USAGE_LOG, ensure_ascii=False), encoding="utf-8")
    return written


def load_corpus(only: str | None = None) -> list[dict]:
    bootstrap()
    import corpus
    scenarios = corpus.SCENARIOS
    if only:
        scenarios = [s for s in scenarios if only in s["name"]]
    return scenarios
