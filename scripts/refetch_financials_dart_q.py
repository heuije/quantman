"""전 종목 재무 캐시 재생성 — 분기를 DART 전자공시(단일분기)로 교체.

financials.refresh()가 FnGuide + DART(연간 5개년·분기 8분기)를 받아 저장한다.
일회성 마이그레이션. 진행/실패를 stdout에 기록(백그라운드 로그로 모니터).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from app import financials  # noqa: E402

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "app", "data", "financials")
codes = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
total = len(codes)
print(f"START refetch {total} codes", flush=True)

ok = fail = dart_q = fg_q = no_q = 0
fails = []
for i, code in enumerate(codes, 1):
    try:
        data = financials.refresh(code)
        n = len((data.get("quarterly", {}).get("PL", {}) or {}).get("periods", []))
        if n >= 6:
            dart_q += 1
        elif n:
            fg_q += 1
        else:
            no_q += 1
        ok += 1
    except Exception as e:  # noqa: BLE001
        fail += 1
        fails.append((code, f"{type(e).__name__}: {e}"))
    if i % 20 == 0 or i == total:
        print(f"{i}/{total} ok={ok} fail={fail} dartQ={dart_q} fgFallback={fg_q} noQ={no_q}", flush=True)
    time.sleep(0.2)

print(f"DONE ok={ok} fail={fail} dartQ={dart_q} fgFallback={fg_q} noQ={no_q}", flush=True)
if fails:
    print("FAILS:", fails[:40], flush=True)
