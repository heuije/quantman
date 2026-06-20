#!/usr/bin/env python
"""frozen 실데이터 스냅샷 캡처 — 진단 하니스 --real 모드용.

load_dataset_for로 심볼을 받아 core/tests/fixtures/frozen/<symbol>.csv로 저장한다. 진단 CLI
(scripts/analysis_diag.py --real)와 test_analysis_corpus의 실데이터 경로가 이 스냅샷을 소비.

⚠️ 데이터 접근 가능한 환경에서 실행: 온라인 머신, 또는 프로덕션 데이터에 붙는 `railway run`.
오프라인/미캐시 심볼은 스킵된다(진단은 합성으로 graceful 폴백). 코스피200선물은 번들 CSV로
오프라인 캡처 가능(scripts 주석 참조) — 그 외(S&P500 등)는 이 도구로 데이터 접근 시 캡처.

  PYTHONUTF8=1 PYTHONPATH=core python scripts/capture_frozen.py S&P500 코스피200선물 --tail 520
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "core"))

import quant_core as qc


def main() -> int:
    ap = argparse.ArgumentParser(description="frozen 실데이터 스냅샷 캡처")
    ap.add_argument("symbols", nargs="+", help="캡처할 심볼들(예: S&P500 코스피200선물)")
    ap.add_argument("--tail", type=int, default=520, help="최근 N 행만 저장(스냅샷 경량)")
    args = ap.parse_args()
    out = _ROOT / "core" / "tests" / "fixtures" / "frozen"
    out.mkdir(parents=True, exist_ok=True)

    try:
        ds = qc.load_dataset_for(args.symbols)
    except Exception as e:  # noqa: BLE001
        print(f"load_dataset_for 실패(데이터 접근 환경에서 실행하세요): {type(e).__name__}: {e}")
        return 1

    saved = 0
    for s in args.symbols:
        df = ds.get(s) if isinstance(ds, dict) else None
        if df is None or getattr(df, "empty", True):
            print(f"  스킵 {s}: 데이터 없음(오프라인/미캐시)")
            continue
        df = df.tail(args.tail).copy()
        df.index.name = "date"
        df.to_csv(out / f"{s}.csv", encoding="utf-8")
        print(f"  저장 {s}: {df.shape} {df.index.min().date()}~{df.index.max().date()} → {s}.csv")
        saved += 1
    print(f"\n{saved}/{len(args.symbols)} 캡처 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
