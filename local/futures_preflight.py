"""선물 라이브 진단 — 사용자가 직접 실행하는 라운드트립/캡처 스크립트.

자동매매 #4 계층(선물)을 라이브로 켜기 전, 국내선물 모의계좌로 주문 라운드트립을
검증하고, 라이브 응답의 raw 필드를 캡처해 다운스트림 코드(equity 반영·해외 사이징)
확정의 근거로 삼는다. **국내 라운드트립 통과가 자동매매 게이트(M1a)를 여는 전제.**

사전 준비:
  1) 한국투자증권 선물옵션 모의계좌 + KIS Developers 모의 앱키
  2) 국내선물 자격증명 등록(keyring):
       python -c "from localapp.secrets_store import save_kis_futures; \
                  save_kis_futures('APPKEY','APPSECRET','60044290-03', virtual=True)"
     해외선물 read-only 캡처는 별도(실전 전용):
       python -c "from localapp.secrets_store import save_kis_overseas_futures; \
                  save_kis_overseas_futures('APPKEY','APPSECRET','47272165-08', virtual=False)"

실행:
  python futures_preflight.py               국내 읽기전용 — 토큰·front-month·잔고·시세·dry-run 바디 (안전)
  python futures_preflight.py --order       + 모의계좌 1계약 매수 → 체결조회 → (보유중 raw 캡처) → 청산
  python futures_preflight.py --overseas-readonly   해외 실전계좌 read-only 캡처 (주문 없음·토큰/잔고/주문가능 raw)

--order는 모의투자(VTS) 계좌라 실제 돈이 아니며, **장중(09:00~15:45)** 에 실행해야 체결됩니다.
--overseas-readonly는 실전계좌를 읽기만 합니다(발주 없음). CME 장중 실행 권장.
출력의 [OK]/[!] 와 raw 덤프를 체크리스트(docs/futures/M8-domestic-roundtrip-checklist.md)와 대조하세요.
"""

import json
import sys
import time

# pytest 캡처 등 reconfigure 미지원 stdout에서도 import 가능하도록 가드.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import localapp  # noqa: E402, F401
from quant_core.exec_defaults import instrument_spec  # noqa: E402
from localapp.futures_contracts import ContractResolver  # noqa: E402
from localapp.kis_futures_broker import (  # noqa: E402
    KisFuturesBroker,
    build_balance_params,
    _BALANCE_PATH,
    _OV_BALANCE_PATH,
    _OV_BALANCE_TR,
    _OV_PSAMOUNT_PATH,
    _OV_PSAMOUNT_TR,
)
from localapp.secrets_store import load_kis_futures, load_kis_overseas_futures  # noqa: E402
from localapp.trader import _round_limit  # noqa: E402

SYMBOL = "코스피200선물"          # 진단 기본값 — 정규 코스피200선물
OVERSEAS_SYMBOL = "금선물"        # 해외 read-only 캡처 대표 종목(GC, globex GCx)


def _dump(label, obj):
    """진단 캡처 — 파서가 버리는 필드까지 그대로 출력. 다운스트림(equity·사이징·취소
    ORD_DT) 코드를 추측이 아니라 실데이터로 확정하기 위한 근거."""
    print(f"\n--- 캡처: {label} ---")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _raw_get(base, headers, path, params):
    """인증을 재사용한 진단용 raw GET — 정규화/파서가 떨군 필드를 보기 위해 원시 JSON 반환."""
    import requests
    r = requests.get(f"{base}{path}", headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8"))


def overseas_readonly():
    """해외선물 실전계좌 read-only 캡처(주문 없음).

    해외선물은 KIS 모의가 없어 라이브 검증이 곧 실전이다. 주문 전, 토큰·잔고(OTFM1412R)·
    주문가능(OTFM3304R)의 raw 응답을 떠서 Phase 5(USD 사이징 fx/funding·equity 반영) 코드를
    실데이터로 확정한다. 주문가능 조회는 빈 계좌에서도 동작한다."""
    if not load_kis_overseas_futures():
        print("[!] 해외선물 자격증명이 없습니다. save_kis_overseas_futures(...)로 등록하세요 (docstring 참조).")
        sys.exit(1)

    print("해외선물 실전계좌 read-only 캡처 (발주 없음)\n")
    broker = KisFuturesBroker()
    broker._ov_token()
    print("1) [OK] 해외 OAuth 토큰 발급")

    # 2) 잔고/미결제 — parsed + raw (USD 평가·증거금 필드 → _unified_equity_krw 배선 근거)
    try:
        _dump("parsed overseas_account_snapshot", broker.overseas_account_snapshot())
        raw_bal = _raw_get(
            broker._ov_base, broker._ov_headers(_OV_BALANCE_TR), _OV_BALANCE_PATH,
            {"CANO": broker._ov_cano, "ACNT_PRDT_CD": broker._ov_acnt_prdt_cd,
             "FUOP_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        _dump("raw 해외잔고 OTFM1412R (USD 평가·증거금 전체 필드)", raw_bal)
    except Exception as e:   # read-only 진단 — 한 엔드포인트 실패가 나머지 캡처를 막지 않게
        print(f"   [!] 해외 잔고 캡처 실패: {e}")

    # 3) 주문가능(OTFM3304R) raw — fm_new_ord_psbl_qty·fx·증거금 등 USD 사이징 근거
    resolver = ContractResolver()
    ov_code = resolver.resolve(OVERSEAS_SYMBOL)
    print(f"\n2) 해외 front-month: {OVERSEAS_SYMBOL} → {ov_code}")
    if ov_code:
        try:
            raw_ord = _raw_get(
                broker._ov_base, broker._ov_headers(_OV_PSAMOUNT_TR), _OV_PSAMOUNT_PATH,
                {"CANO": broker._ov_cano, "ACNT_PRDT_CD": broker._ov_acnt_prdt_cd,
                 "OVRS_FUTR_FX_PDNO": ov_code, "FM_ORD_PRIC": ""})
            _dump("raw 주문가능 OTFM3304R (신규주문가능 계약수·fx·증거금 전체 필드)", raw_ord)
        except Exception as e:
            print(f"   [!] 주문가능 캡처 실패: {e}")
    else:
        print("   [!] 해외 front-month 해석 실패 — ffcode.mst 다운로드/파싱 확인.")

    print("\n[완료] 위 raw 덤프를 Phase 5(해외 사이징 fx/funding·equity) 코드 확정에 사용.")


def main():
    if "--overseas-readonly" in sys.argv:
        overseas_readonly()
        return

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

    # 4) 현재가 조회 (브로커가 간헐 5xx는 재시도; 지속 실패는 px=0로 떨궈 크래시 대신 보고)
    try:
        px = broker.price(code)
    except Exception as e:
        px = 0.0
        print(f"[!] 현재가 조회 실패 — {e} (px=0 처리, 라운드트립은 시세 확보 시에만)")
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
        print("\n6) 모의 1계약 라운드트립 (매수 → 체결조회 → 보유중 캡처 → 청산)")
        buy_limit = _round_limit(px * 1.01, "up", SYMBOL)
        r = broker.buy_limit(code, 1, buy_limit)
        on = r.get("order_no", "")
        print(f"   매수 발주: success={r.get('success')} order_no={on} limit={buy_limit} | {r.get('message','')}")
        # 정규화 응답 검증 — success·order_no가 raw가 아닌 정규형으로 추출되는지(근본수정 확인)
        _dump("매수 주문 정규화 응답 (success·order_no 추출 확인)", r)
        if not on:
            print("   [!] 주문번호 없음 — 거부 메시지 확인."); sys.exit(1)
        time.sleep(3)
        st = broker.order_status(on)
        print(f"   체결조회: status={st['status']} 체결 {st['filled_qty']}계약 @ {st['fill_price']}")
        if st["filled_qty"] > 0:
            # ── 캡처(보유 중): 선물 평가가 어떤 잔고 필드로 들어오는지 확정 — _unified_equity_krw
            #    (kill-switch·drawdown)가 선물 PnL을 인지하도록 배선할 근거(Phase 1). 캡처 실패가
            #    안전상 중요한 청산을 막지 않도록 try/except(미청산 포지션 방지). ──
            try:
                _dump("parsed account_snapshot (positions·account)", broker.account_snapshot())
                raw_bal = _raw_get(broker.base, broker._headers(broker._balance_tr()),
                                   _BALANCE_PATH, build_balance_params(broker.cano, broker.acnt_prdt_cd))
                _dump("raw 잔고 VTFO6118R (output1 포지션 · output2 계좌요약 전체 필드)", raw_bal)
            except Exception as e:
                print(f"   [!] 보유중 잔고 캡처 실패(청산은 계속 진행): {e}")
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
