"""LS증권 모의투자 실연동 검증 + 응답 raw 필드 캡처 — 사용자가 직접 실행.

LsBroker의 조회·주문 메서드는 응답 블록명/필드명이 아직 '초안'이다
(docs/ls-api GOTCHAS 의 ⚠ 항목 — 단위테스트는 mock fixture 로만 통과해 실측이 아니다).
이 스크립트는 각 TR 의 *raw 응답*을 (자격증명·계좌번호 마스킹 후) 그대로 덤프해,
실제 블록·필드명을 확정하고 ls_broker.py 의 ⚠ 가정을 교정할 근거를 만든다.
verify_kis.py 대칭 + raw 캡처 + 당일매매 체결인지(t0425 status) 실측.

━━ 사전 준비 ━━
  1) LS증권 계좌 개설 → LS OpenAPI(openapi.ls-sec.co.kr) 신청 → '모의투자' appkey/appsecretkey 발급
  2) python desktop.py  →  설정 wizard 에서 [LS증권] 선택 → App Key/Secret/계좌번호/모의 체크 → 저장
     (LS 자격증명은 GUI wizard 로 등록한다. run.py setup 은 KIS 전용.)

━━ 실행 (local/ 디렉터리에서) ━━
  python verify_ls.py            토큰·잔고·시세·미체결 raw 캡처 (읽기 전용 — 주문 없음, 항상 안전)
  python verify_ls.py --kosdaq   위 + KOSDAQ 종목(035720 카카오) 시세도 — exchgubun='Q' 필요 여부 확정(G21)
  python verify_ls.py --order    위 + 모의계좌 1주 시장가 매수→체결조회→정리 (모의 자금 · 장중 09:00~15:20)

⚠ --order 는 모의투자라 실제 돈이 아니다. 단, 장중에만 체결된다. 실전 키면 명시 확인을 요구한다.
   출력의  ===== RAW: ... =====  블록을 통째로 복사해 전달하면 ⚠ 필드를 실측 확정한다.
"""
from __future__ import annotations

import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import localapp  # noqa: E402, F401  (패키지/sys.path 초기화)
from localapp.ls_broker import LsBroker  # noqa: E402
from localapp.secrets_store import get_active_broker, load_ls  # noqa: E402

KOSPI_TICKER = "005930"    # 삼성전자
KOSDAQ_TICKER = "035720"   # 카카오 (KOSDAQ — exchgubun 확정용)

# 마스킹 대상 키 패턴 (소문자 부분일치) — 자격증명·계좌·비밀번호·토큰만.
# 모의 잔고·종목·가격은 가짜 자금이라 보존한다(실제 필드명 확정에 그 값이 필요).
_SECRET_KEY_HINTS = ("acntno", "acnt", "pwd", "pass", "passwd",
                     "appkey", "appsecret", "secret", "token", "authorization")


def _redact(obj, secret_values):
    """raw 응답/요청에서 민감 값을 '***' 로 마스킹.

    키 이름이 자격증명류이거나, 값이 계좌번호·앱키와 일치하면 치환.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in str(k).lower() for h in _SECRET_KEY_HINTS):
                out[k] = "***"
            else:
                out[k] = _redact(v, secret_values)
        return out
    if isinstance(obj, list):
        return [_redact(x, secret_values) for x in obj]
    if isinstance(obj, str) and obj and obj in secret_values:
        return "***"
    return obj


def _dump(title, payload, secret_values):
    bar = "=" * 72
    print(f"\n{bar}\n===== RAW: {title}\n{bar}")
    print(json.dumps(_redact(payload, secret_values), ensure_ascii=False, indent=2))
    print(bar)


def _confirm_real() -> bool:
    print("\n⚠⚠ 실전투자 키입니다. --order 는 실제 주문을 낼 수 있습니다.")
    print("    읽기 전용 검증만 하려면 --order 없이 실행하세요.")
    return input("    실전 계좌로 계속하려면 정확히 'REAL' 입력: ").strip() == "REAL"


def main():
    do_order = "--order" in sys.argv
    do_kosdaq = ("--kosdaq" in sys.argv) or do_order

    creds = load_ls()
    if not creds:
        print("LS 자격증명이 없습니다. 먼저 python desktop.py → 설정에서 [LS증권] 키를 등록하세요.")
        sys.exit(1)
    if get_active_broker() != "ls":
        print(f"⚠ 활성 브로커가 'ls'가 아닙니다(현재 '{get_active_broker()}'). "
              "설정 wizard 에서 LS 선택·저장 시 활성화됩니다. (프로브는 그대로 진행)")

    secret_values = {
        str(creds.get("account_no", "")), str(creds.get("account_no", "")).replace("-", ""),
        str(creds.get("app_key", "")), str(creds.get("app_secret", "")),
    }
    secret_values.discard("")

    broker = LsBroker()
    mode = "모의투자" if broker.virtual else "⚠실전투자⚠"
    tail = ("…" + broker.account_no[-2:]) if broker.account_no else "?"
    print(f"LS 실연동 검증 — {mode} (계좌 {tail})")
    # 읽기 전용은 실전 키여도 안전. 실전 + 실주문(--order)만 명시 확인을 요구.
    if not broker.virtual and do_order and not _confirm_real():
        print("중단.")
        sys.exit(1)

    # ── _post 캡처 래퍼: 본문 중복 없이 각 메서드의 raw 요청/응답을 잡는다 ──
    captured = {"last": None}
    orig_post = broker._post

    def _capturing_post(path, tr_cd, body, **kw):
        try:
            r = orig_post(path, tr_cd, body, **kw)
            captured["last"] = {"path": path, "tr_cd": tr_cd, "request_body": body,
                                "response": r, "error": None}
            return r
        except Exception as e:  # HTTP 4xx/5xx 도 캡처(경로·tr_cd 오류 확인용)
            captured["last"] = {"path": path, "tr_cd": tr_cd, "request_body": body,
                                "response": None, "error": repr(e)}
            raise

    broker._post = _capturing_post

    def _show_last(title):
        if captured["last"]:
            _dump(title, captured["last"], secret_values)
            captured["last"] = None

    # [1] 토큰
    broker._token()
    print("\n[1] OAuth 토큰 발급 OK (토큰 값은 출력하지 않음)")

    # [2] 잔고 (t0424) — 블록명·필드명·예수금/평가 소스 확정
    print("\n[2] 잔고 조회 (t0424)")
    snap = None
    try:
        snap = broker.account_snapshot()
    except Exception as e:
        print(f"   account_snapshot 예외: {e!r}")
    _show_last("t0424 주식잔고 (path / tr_cd / 요청 / 응답)")
    if snap:
        bal = snap["balance"]
        ff = bal.get("fetch_failed")
        print(f"   파싱결과 → 예수금(cash) {bal['cash']:,} · 평가(total_eval) {bal['total_eval']:,} · "
              f"보유 {len(snap['positions'])}종목" + (f" · ⚠fetch_failed={ff}" if ff else ""))
        if not ff and bal["cash"] == 0 and bal["total_eval"] == 0:
            print("   ⚠ cash·total_eval 모두 0 — 위 RAW 에서 실제 잔고 필드명이 "
                  "sunamt1/tappamt 와 다른지 확인 필요(필드명 불일치 의심).")

    # [3] 시세 (t1102) — KOSPI
    print(f"\n[3] 시세 조회 (t1102) — 삼성전자 {KOSPI_TICKER} (KOSPI)")
    px = op = 0.0
    try:
        px = broker.price(KOSPI_TICKER)
        op = broker.today_open(KOSPI_TICKER)
    except Exception as e:
        print(f"   price 예외: {e!r}")
    _show_last(f"t1102 시세 {KOSPI_TICKER}")
    print(f"   파싱결과 → 현재가(price) {px:,.0f} · 시가(open) {op:,.0f}")
    if px == 0:
        print("   ⚠ price=0 — RAW 에서 현재가 필드명이 'price'와 다른지 확인.")

    # [3b] KOSDAQ — exchgubun 확정 (G21)
    if do_kosdaq:
        print(f"\n[3b] 시세 조회 (t1102) — 카카오 {KOSDAQ_TICKER} (KOSDAQ · exchgubun 확정)")
        pxk = 0.0
        try:
            pxk = broker.price(KOSDAQ_TICKER)
        except Exception as e:
            print(f"   price 예외: {e!r}")
        _show_last(f"t1102 시세 {KOSDAQ_TICKER} (KOSDAQ)")
        print(f"   파싱결과 → KOSDAQ 현재가 {pxk:,.0f}")
        if pxk == 0:
            print("   ⚠ KOSDAQ 현재가 0 — t1102 InBlock 에 exchgubun='Q' 필요 가능성(G21). "
                  "RAW 확인 후 _price_raw 에 exchgubun 추가 검토.")

    # [4] 미체결 (t0425, chegb=2)
    print("\n[4] 미체결 조회 (t0425 chegb=2)")
    pend = []
    try:
        pend = broker.pending_orders()
    except Exception as e:
        print(f"   pending_orders 예외: {e!r}")
    _show_last("t0425 미체결 (chegb=2)")
    print(f"   파싱결과 → 미체결 {len(pend)}건")

    # [5] 주문 라운드트립 (--order, 모의·장중)
    if do_order:
        _order_roundtrip(broker, orig_post, captured, secret_values, _show_last)
    else:
        print("\n[5] 주문 라운드트립 생략 (--order 로 활성화 — 모의·장중 09:00~15:20)")

    print("\n[완료] 위 ===== RAW: ... ===== 블록 전체를 복사해 전달하면 ⚠ 필드를 실측 확정합니다.")


def _order_roundtrip(broker, orig_post, captured, secret_values, _show_last):
    """모의 1주 시장가 매수 → 체결조회 폴링 → t0425 chegb=0 status 실측 → 보유 시 매도 / 미체결 시 취소."""
    sym = KOSPI_TICKER
    print(f"\n[5] 모의 주문 라운드트립 — {sym} 시장가 1주 매수")
    print("    (장중 09:00~15:20 에만 체결. 장외면 미체결로 남아 [5d]에서 취소됩니다.)")

    # [5a] 매수 — buy()는 normalize 까지 수행, 래퍼가 raw 캡처
    try:
        r = broker.buy(sym, 1)
    except Exception as e:
        _show_last("CSPAT00601 매수 (요청/응답 — 예외)")
        print(f"   ⚠ 매수 예외: {e!r}")
        return
    _show_last("CSPAT00601 매수 응답 (★성공코드 rsp_cd · OrdNo 필드 확정★)")
    print(f"   정규화결과 → success={r['success']} · order_no='{r['order_no']}' · "
          f"msg_cd='{r['msg_cd']}' · {r['message']}")
    if not r["success"] or not r["order_no"]:
        print("   ⚠ 매수 미접수. 위 RAW 에서 실제 OrdNo 블록/필드명과 rsp_cd 성공값 확인(G17/G11). 중단.")
        return
    order_no = r["order_no"]

    # [5b] 체결조회 폴링 — order_status (chegb=2 기반: 체결되면 목록에서 사라질 수 있음 = G10/G19)
    print(f"\n[5b] 체결 조회 — order_status('{order_no}') 폴링 3회")
    for i in range(3):
        time.sleep(1.5)
        try:
            st = broker.order_status(order_no, symbol=sym)
            print(f"   폴링{i + 1} → status={st['status']} · filled={st['filled_qty']} · "
                  f"remain={st['remain_qty']} · fill_price={st['fill_price']}")
        except Exception as e:
            print(f"   폴링{i + 1} 예외: {e!r}")
    _show_last("t0425 order_status 폴링 마지막 raw (chegb=2)")

    # [5c] ★당일매매 핵심★ — t0425 chegb=0(전체) raw 로 체결/취소 status 필드 실측 (G19)
    print("\n[5c] ★체결 인지 실측★ — t0425 chegb=0 (전체조회) — status 필드/값 확인")
    print("     현 코드는 chegb=2=미체결만 → 체결되면 사라져 'unknown'. chegb=0 응답의 status 로")
    print("     filled/cancelled 를 구분할 수 있는지가 당일매매(hold_days=0) 종가청산의 관건 — G19.")
    try:
        raw0 = orig_post("/stock/accno", "t0425",
                         {"t0425InBlock": {"expcode": "", "chegb": "0", "medosu": "0",
                                           "sortgb": "1", "cts_ordno": ""}})
        _dump("t0425 전체조회 (chegb=0) — ★status 필드/값 확정★", raw0, secret_values)
        rows = raw0.get("t0425OutBlock1") or []
        print(f"   chegb=0 행 {len(rows)}건. 위 RAW 에서 방금 주문({order_no}) 행을 찾아 "
              "체결/접수/취소를 구분하는 필드명·값(status?)을 확인하세요.")
    except Exception as e:
        print(f"   ⚠ chegb=0 조회 예외: {e!r} (경로/파라미터 확인 필요)")

    # [5d] 정리 — 보유 시 1주 시장가 매도, 미체결이면 취소
    print("\n[5d] 정리 — 보유 시 1주 매도, 미체결이면 취소")
    held = 0
    try:
        snap = broker.account_snapshot()
        for p in snap["positions"]:
            if p["symbol"] == sym:
                held = p["qty"]
    except Exception as e:
        print(f"   잔고 재조회 예외: {e!r}")
    if held >= 1:
        try:
            rs = broker.sell(sym, 1)
            _show_last("CSPAT00601 매도 응답")
            print(f"   매도 → success={rs['success']} · order_no='{rs['order_no']}' · {rs['message']}")
        except Exception as e:
            print(f"   ⚠ 매도 예외: {e!r}")
    else:
        print("   보유 0주(미체결 추정) → 매수 취소 시도")
        try:
            rc = broker.cancel(order_no, sym, 1)
            _show_last("CSPAT00801 취소 응답")
            print(f"   취소 → success={rc['success']} · {rc['message']}")
        except Exception as e:
            print(f"   ⚠ 취소 예외: {e!r}")


if __name__ == "__main__":
    main()
