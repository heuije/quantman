"""전 종목 재무 캐시 재생성 — 연간 = 전자공시 원문 보고서, 분기 = DART 단일분기.

financials.refresh()가 원문 사업보고서(연간)+OpenAPI(분기)를 받아 저장한다. 일회성 마이그레이션.
네트워크 불안정 대비: socket 전역 timeout(소켓 행 방지) + 스레드 병렬(한 종목이 느려도 나머지 진행).
※ 백그라운드 Bash는 회수될 수 있으니 detached(PowerShell Start-Process)로 실행 권장.
"""
import os
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

socket.setdefaulttimeout(45)   # 소켓 read/connect 행 방지(원문 문서 6MB 다운로드 불안정 대비)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from app import financials  # noqa: E402

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "app", "data", "financials")
codes = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
total = len(codes)
WORKERS = 4
print(f"START refetch {total} codes (workers={WORKERS}, socket_to=45s)", flush=True)

lock = threading.Lock()
done = ok = fail = dart_q = fg_q = no_q = 0
fails = []


def work(code):
    global done, ok, fail, dart_q, fg_q, no_q
    try:
        data = financials.refresh(code)
        n = len((data.get("quarterly", {}).get("PL", {}) or {}).get("periods", []))
        with lock:
            ok += 1
            if n >= 6:
                dart_q += 1
            elif n:
                fg_q += 1
            else:
                no_q += 1
    except Exception as e:  # noqa: BLE001
        with lock:
            fail += 1
            fails.append((code, f"{type(e).__name__}: {e}"))
    with lock:
        done += 1
        if done % 20 == 0 or done == total:
            print(f"{done}/{total} ok={ok} fail={fail} dartQ={dart_q} fgFallback={fg_q} noQ={no_q}", flush=True)


with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(as_completed(ex.submit(work, c) for c in codes))

print(f"DONE ok={ok} fail={fail} dartQ={dart_q} fgFallback={fg_q} noQ={no_q}", flush=True)
if fails:
    print("FAILS:", fails[:60], flush=True)
