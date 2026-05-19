"""KIS 모의투자 실연동 검증 - 사용자가 직접 실행하는 스크립트.

사전 준비:
  1) 한국투자증권 계좌 + KIS Developers 모의투자 앱키 발급
  2) python run.py setup   (앱키/시크릿/계좌번호 등록)

실행:
  python verify_kis.py            토큰·잔고·시세 조회만 (읽기 전용, 안전)
  python verify_kis.py --order    + 모의계좌에 1주 매수/매도 테스트 (모의 자금)

--order는 모의투자(VTS) 계좌라 실제 돈이 아니며, 장중에 실행해야 체결됩니다.
"""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import localapp  # noqa: E402, F401
from localapp.kis_broker import KisBroker  # noqa: E402
from localapp.secrets_store import load_kis  # noqa: E402

TEST_TICKER = "069500"   # KODEX 200 ETF


def main():
    do_order = "--order" in sys.argv

    if not load_kis():
        print("KIS 자격증명이 없습니다. 먼저: python run.py setup")
        sys.exit(1)

    print("KIS 모의투자 실연동 검증\n")

    # 1) 토큰 발급 + 브로커 초기화
    broker = KisBroker()
    assert broker.virtual, "모의투자(virtual=True) 설정이 아닙니다. setup을 확인하세요."
    broker._token()
    print("1) OAuth 토큰 발급 OK")

    # 2) 계좌 잔고/보유종목 조회
    snap = broker.account_snapshot()
    bal = snap["balance"]
    print(f"2) 잔고 조회 OK - 예수금 {bal['cash']:,}원, "
          f"평가금액 {bal['total_eval']:,}원, 보유 {len(snap['positions'])}종목")

    # 3) 현재가 조회
    px = broker.price(TEST_TICKER)
    assert px > 0, f"현재가 조회 실패 ({TEST_TICKER})"
    print(f"3) 시세 조회 OK - KODEX 200({TEST_TICKER}) 현재가 {px:,.0f}원")

    # 4) 주문 테스트 (선택)
    if do_order:
        print("\n4) 모의 주문 테스트 (1주 매수 → 매도)")
        r = broker.buy(TEST_TICKER, 1)
        print(f"   매수: success={r['success']} | {r['message']}")
        assert r["success"], "매수 주문 거부 — 장 시간 또는 예수금 확인"
        time.sleep(2)
        r = broker.sell(TEST_TICKER, 1)
        print(f"   매도: success={r['success']} | {r['message']}")
    else:
        print("\n4) 주문 테스트 생략 (--order 로 활성화)")

    print("\n[OK] KIS 모의투자 실연동 정상")


if __name__ == "__main__":
    main()
