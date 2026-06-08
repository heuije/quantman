"""M8 국내선물(KOSPI200) 라이브 라운드트립 — 사용자가 직접 실행하는 진단 스크립트.

자동매매 #4 계층(선물)을 라이브로 켜기 전, 국내선물 모의계좌(예: 60044290)로
주문 라운드트립을 검증한다. **이 스크립트가 자동매매 게이트(M1a)를 여는 전제 조건.**

사전 준비:
  1) 한국투자증권 선물옵션 모의계좌 + KIS Developers 모의 앱키
  2) 선물 자격증명 등록(keyring):
       python -c "from localapp.secrets_store import save_kis_futures; \
                  save_kis_futures('APPKEY','APPSECRET','60044290-03', virtual=True)"

실행:
  python futures_preflight.py            읽기 전용 — 토큰·front-month·잔고·시세·dry-run 주문바디 (안전)
  python futures_preflight.py --order    + 모의계좌에 1계약 매수 → 체결조회 → 청산(매도) 라운드트립

--order는 모의투자(VTS) 계좌라 실제 돈이 아니며, **장중(09:00~15:45)** 에 실행해야 체결됩니다.
출력의 [OK]/[!] 를 체크리스트(docs/futures/M8-domestic-roundtrip-checklist.md)와 대조하세요.
"""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import localapp  # noqa: E402, F401
from quant_core.exec_defaults import instrument_spec  # noqa: E402
from localapp.futures_contracts import ContractResolver  # noqa: E402
from localapp.kis_futures_broker import KisFuturesBroker  # noqa: E402
from localapp.secrets_store import load_kis_futures  # noqa: E402
from localapp.trader import _round_limit  # noqa: E402

SYMBOL = "코스피200선물"          # 데이터셋 심볼(국내 유일 선물)


def main():
    do_order = "--order" in sys.argv

    creds = load_kis_futures()
    if not creds:
        print("[!] 국내선물 자격증명이 없습니다. 먼저 save_kis_futures(...) 등록 (docstring 참조).")
        sys.exit(1)

    print("국내선물(KOSPI200) 라이브 라운드트립 점검\n")
    spec = instrument_spec(SYMBOL)
    print(f"  계약명세: 승수 {spec.multiplier:,.0f} · 틱 {spec.tick} · 증거금률 {spec.init_margin_rate:.0%}"
          f" · 만기룰 {spec.expiry_rule}")

    # 1) 토큰 발급
    broker = KisFuturesBroker()
    if not broker.virtual:
        print("[!] 모의(virtual=True) 설정이 아닙니다 — 실거래 계좌로는 이 스크립트를 돌리지 마세요.")
        sys.exit(1)
    broker._token()
    print("1) [OK] OAuth 토큰 발급")

    # 2) front-month 계약코드 + 만기일 해석 (마스터 다운로드)
    resolver = ContractResolver()
    code = resolver.resolve(SYMBOL)
    _, expiry = resolver.resolve_expiry(SYMBOL)
    if not code:
        print("[!] front-month 계약코드 해석 실패 — fo_idx_code.mst 다운로드/파싱 확인.")
        sys.exit(1)
    print(f"2) [OK] front-month: {SYMBOL} → {code} (만기 {expiry})")

    # 3) 잔고/증거금 조회 (읽기 전용)
    snap = broker.account_snapshot()
    acct = snap.get("account", {})
    print(f"3) [OK] 잔고 조회 — 주문가능현금 {acct.get('order_cash', 0):,.0f} · "
          f"증거금합계 {acct.get('margin_total', 0):,.0f} · 보유 {len(snap.get('positions', []))}계약")

    # 4) 현재가 조회
    px = broker.price(code)
    if px <= 0:
        print(f"[!] 현재가 조회 0 — 장 시간 또는 시세 권한 확인 ({code})")
    else:
        print(f"4) [OK] 시세 조회 — {code} 현재가 {px:,.2f}")

    # 5) dry-run 주문바디 (발주하지 않음 — 틱 라운딩·바디 형식 점검)
    ref = px if px > 0 else 350.0
    limit = _round_limit(ref * 1.01, "up", SYMBOL)       # 매수 tolerance 1% 가정
    print(f"5) [OK] dry-run 지정가 라운딩: {ref:,.2f}×1.01 → {limit} (틱 {spec.tick} 그리드)")

    # 6) (선택) 실제 1계약 라운드트립 — 모의계좌
    if do_order:
        if px <= 0:
            print("\n[!] 현재가 0 — 라운드트립 생략(장중에 재실행).")
            sys.exit(1)
        print("\n6) 모의 1계약 라운드트립 (매수 → 체결조회 → 청산)")
        buy_limit = _round_limit(px * 1.01, "up", SYMBOL)
        r = broker.buy_limit(code, 1, buy_limit)
        on = r.get("order_no", "")
        print(f"   매수 발주: success={r.get('success')} order_no={on} limit={buy_limit} | {r.get('message','')}")
        if not on:
            print("   [!] 주문번호 없음 — 거부 메시지 확인."); sys.exit(1)
        time.sleep(3)
        st = broker.order_status(on)
        print(f"   체결조회: status={st['status']} 체결 {st['filled_qty']}계약 @ {st['fill_price']}")
        if st["filled_qty"] > 0:
            sell_limit = _round_limit(px * 0.99, "down", SYMBOL)
            r2 = broker.sell_limit(code, st["filled_qty"], sell_limit)
            print(f"   청산 발주: success={r2.get('success')} order_no={r2.get('order_no','')} | {r2.get('message','')}")
        else:
            print("   [!] 미체결 — 지정가가 호가 밖이거나 장 시간 확인. 미체결 주문은 KIS가 마감 자동취소.")
    else:
        print("\n6) 실제 라운드트립 생략 (--order 로 활성화, 장중 실행)")

    print("\n[완료] 위 1~5가 모두 [OK]면 국내선물 라이브 경로 준비 완료. "
          "--order 라운드트립까지 통과하면 M1a 게이트 개방 가능.")


if __name__ == "__main__":
    main()
