"""자동매매 건강 진단 CLI — 운영자·Claude Code가 전 유저 건강을 CLI로 본다.

읽기전용(Phase 3a): compute_user_health를 CLI로 노출.
    railway run python -m app.health_cli              # 전 유저(RED 상단)
    railway run python -m app.health_cli --red-only   # RED/AMBER 유저만
    railway run python -m app.health_cli --user 73 -v # 단일 유저 9조건 상세
    railway run python -m app.health_cli --json       # 기계용 JSON

**신선도 주의:** 마지막 스냅샷 기준이다. 시간기반(런타임·연결)은 항상 신선하나, content(데이터·
브로커·판단)는 유저의 마지막 사이클/프로브만큼만 신선 — 장 시간 밖 fresh full 진단은 Phase 3b의
readiness 프로브(`--probe`)가 온다(릴리스 후).

chat_analytics/manage.py 패턴: from .db import engine으로 세션 열어 호출·출력만.
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlmodel import Session

from .db import engine
from .health_monitor import (AMBER, GREEN, RED, UNKNOWN, compute_user_health)

# cp949 콘솔·파이프에서 안전하도록 이모지 대신 ASCII 마커(Claude Code 파싱도 용이).
_MARK = {RED: "RED  ", AMBER: "AMBER", GREEN: "GREEN", UNKNOWN: "UNK  "}
# 조건 키 → 짧은 라벨(대시보드와 동일 순서).
_COND_LABEL = {
    "runtime": "런타임", "connectivity": "연결", "data_freshness": "데이터",
    "strategy_validity": "전략유효", "broker_ready": "브로커", "cycle_execution": "사이클",
    "decision_output": "판단", "order_placement": "발주", "reconciliation": "정합",
    "ref_price_integrity": "참조가",
}


def _print_rows(rows: list[dict], *, verbose: bool) -> None:
    if not rows:
        print("(해당 유저 없음)")
        return
    n_red = sum(1 for r in rows if r["overall"] == RED)
    n_amber = sum(1 for r in rows if r["overall"] == AMBER)
    print(f"자동매매 건강 — {len(rows)}명 (RED {n_red} · AMBER {n_amber}) · RED 상단 정렬\n")
    for r in rows:
        codes = ",".join(a["code"] for a in r["alert_reasons"])
        push = (r["snapshot_at"] or "없음")[:16]
        # content_at(데이터·판단 등 content 조건이 읽은 스냅샷)이 최신 push와 다르면 표시 —
        # "데이터는 직전 cycle 기준"임을 투명화(F3). 최신이 곧 cycle이면 생략(동일).
        cat = (r.get("content_at") or "")[:16]
        content_note = f" content={cat}" if cat and cat != push else ""
        print(f"[{_MARK.get(r['overall'], '?????')}] {(r['email'] or '?'):28} "
              f"live={r['live_strategies']} push={push}{content_note}"
              + (f"  ALERT: {codes}" if codes else ""))
        # RED/AMBER 유저는 문제 조건을, verbose면 전 조건을 상세 출력.
        show_all = verbose
        for k, lbl in _COND_LABEL.items():
            c = r["conditions"].get(k, {})
            st = c.get("status")
            if show_all or st in (RED, AMBER):
                print(f"        {_MARK.get(st, '?????')} {lbl}: {c.get('detail', '')}")
        # 번들/데이터 로드 요약(F1) — content 조건의 원천 수치를 한 줄로.
        b, d = r.get("bundle") or {}, r.get("dataset") or {}
        if verbose and (b or d):
            print(f"        · 번들 {b.get('result', '?')}/{b.get('n_files', '?')}files"
                  f"/{b.get('n_failed', '?')}fail · dataset "
                  f"{d.get('loaded', '?')}/{d.get('needed', '?')} loaded")
        # 원시 최근 에러(F1) — 신호등만 보고 놓치던 raw 에러(예: CON.parquet WinError)를 직접 노출.
        # 이게 이번 CON 오진의 근본수정: 증거가 DB에 있어도 CLI가 안 보여줘 진단자가 못 봤다.
        # 전부 노출 — 클라가 이미 최근 10건으로 캡한 목록이라 truncate하면 가장 진단적인 에러
        # (예: 오래돼 밀려난 CON.parquet WinError)를 놓친다. 이번 오진의 직접 교훈.
        errs = r.get("recent_errors") or []
        if errs:
            print(f"        최근 에러 {len(errs)}건:")
            for e in errs:
                print(f"          - {str(e)[:150]}")


def main_cli(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="자동매매 건강 진단 CLI (운영자·Claude Code 사전 디버깅)")
    ap.add_argument("--user", type=int, default=None, help="특정 user_id만")
    ap.add_argument("--red-only", action="store_true", help="RED/AMBER 유저만")
    ap.add_argument("-v", "--verbose", action="store_true", help="전 9조건 상세 출력")
    ap.add_argument("--json", action="store_true", help="JSON 출력(기계용)")
    args = ap.parse_args(argv)

    with Session(engine) as s:
        rows = compute_user_health(s)
    if args.user is not None:
        rows = [r for r in rows if r["user_id"] == args.user]
    if args.red_only:
        rows = [r for r in rows if r["overall"] in (RED, AMBER)]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    _print_rows(rows, verbose=args.verbose or args.user is not None)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # cp949 콘솔에서 한글 detail 깨짐 방지
    except Exception:
        pass
    main_cli()
