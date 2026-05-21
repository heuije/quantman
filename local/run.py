"""로컬앱 CLI 진입점.

  python run.py setup     KIS 모의투자 자격증명 등록 (keyring)
  python run.py pair      플랫폼 계정과 기기 페어링
  python run.py cycle     자동매매 사이클 1회 실행
  python run.py run       스케줄러 상주 실행
  python run.py status    현재 연동 상태 확인

Phase 38.3: --mock 제거. KIS 자격증명이 없으면 명시적 RuntimeError.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from localapp import secrets_store, single_instance
from localapp.config import LEDGER_PATH, PLATFORM_URL
from localapp.logging_setup import setup_logging


def cmd_setup():
    print("KIS 모의투자 자격증명 등록 (OS 자격증명 저장소에 저장됩니다)")
    app_key = input("  App Key: ").strip()
    app_secret = getpass.getpass("  App Secret: ").strip()
    account_no = input("  계좌번호 (예: 50001234-01): ").strip()
    secrets_store.save_kis(app_key, app_secret, account_no, virtual=True)
    print("저장 완료. 키는 이 PC를 떠나지 않습니다.")


def cmd_pair():
    from localapp.pairing import poll_for_token, start_pairing
    info = start_pairing("내 PC")
    print("\n" + "=" * 44)
    print(f"  브라우저에서 {info['verification_uri']} 접속 후")
    print(f"  아래 코드를 입력해 이 기기를 승인하세요:")
    print(f"\n      연결 코드:  {info['user_code']}\n")
    print("=" * 44)
    print("승인 대기 중...")
    poll_for_token(info["device_code"])
    print("페어링 완료. 기기 토큰을 저장했습니다.")


def _guard_or_exit():
    """단일 인스턴스 잠금 — 이중 주문 방지."""
    if not single_instance.acquire():
        print("오류: 로컬앱이 이미 실행 중입니다. 이중 주문 방지를 위해 중단합니다.")
        sys.exit(1)


def cmd_cycle():
    setup_logging()
    _guard_or_exit()
    try:
        from localapp.runner import run_cycle
        payload = run_cycle()
        bal = payload["balance"]
        print(f"평가금액 {bal['total_eval']:,}원 · 예수금 {bal['cash']:,}원 · "
              f"보유 {len(payload['positions'])}종목 · 체결 {len(payload['trades'])}건")
    finally:
        single_instance.release()


def cmd_run():
    setup_logging()
    _guard_or_exit()
    try:
        from localapp.scheduler import start
        start()
    finally:
        single_instance.release()


def cmd_status():
    kis = secrets_store.load_kis()
    dev = secrets_store.load_device_token()
    print(f"플랫폼 URL     : {PLATFORM_URL}")
    print(f"KIS 자격증명   : {'등록됨' if kis else '없음'}")
    print(f"기기 페어링    : {'완료' if dev else '안 됨'}")
    print(f"보유 원장      : {LEDGER_PATH if LEDGER_PATH.exists() else '없음'}")


def main():
    p = argparse.ArgumentParser(description="퀀트 플랫폼 로컬앱")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "pair", "cycle", "run", "status"):
        sub.add_parser(name)
    args = p.parse_args()

    if args.cmd == "setup":   cmd_setup()
    elif args.cmd == "pair":  cmd_pair()
    elif args.cmd == "cycle": cmd_cycle()
    elif args.cmd == "run":   cmd_run()
    elif args.cmd == "status": cmd_status()


if __name__ == "__main__":
    main()
