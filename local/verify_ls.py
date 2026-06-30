"""LS증권 모의투자 실연동 검증 + 응답 raw 필드 캡처 — 사용자가 직접 실행.

LsBroker / LsFuturesBroker 의 조회·주문 메서드는 응답 블록명/필드명이 아직 '초안'이다
(docs/ls-api GOTCHAS 의 ⚠ 항목 — 단위테스트는 mock fixture 로만 통과해 실측이 아니다).
이 스크립트는 각 TR 의 *raw 응답*을 (자격증명·계좌번호 마스킹 후) 그대로 덤프해,
실제 블록·필드명을 확정하고 브로커의 ⚠ 가정을 교정할 근거를 만든다.
verify_kis.py 대칭 + raw 캡처 + 당일매매 체결인지(t0425 status) 실측.

런북: docs/ls-api/E2E-LIVE-TEST.md

━━ 사전 준비 ━━
  1) LS증권 계좌 개설 → LS OpenAPI(openapi.ls-sec.co.kr) 신청 → '모의투자' appkey/appsecretkey 발급
  2) python desktop.py  →  설정 wizard 에서 [LS증권] 선택 → App Key/Secret/계좌번호/모의 체크 → 저장
     (LS 자격증명은 GUI wizard 로 등록한다. run.py setup 은 KIS 전용.)

━━ 실행 (local/ 디렉터리에서) ━━
  [국내주식 — 기본, 자격증명: load_ls()]
  python verify_ls.py            토큰·잔고·시세·미체결 raw 캡처 (읽기 전용 — 주문 없음, 항상 안전)
  python verify_ls.py --kosdaq   위 + KOSDAQ 종목(035720 카카오) 시세도 — exchgubun='Q' 필요 여부 확정(G21)
  python verify_ls.py --order    위 + 모의계좌 1주 시장가 매수→체결조회→정리 (모의 자금 · 장중 09:00~15:20)

  [국내선물 — load_ls_futures() 자격증명 필요]
  python verify_ls.py --futures            잔고·시세·마스터 raw 캡처 (읽기 전용)
  python verify_ls.py --futures --order    위 + 모의 1계약 시장가 매수→체결조회→정리

  [해외주식 — load_ls() 자격증명 필요 (국내주식과 동일 LsBroker)]
  python verify_ls.py --overseas-stock            해외 잔고·AAPL 시세 raw 캡처 (읽기 전용)
  python verify_ls.py --overseas-stock --order    위 + AAPL 1주 매수→체결조회→정리

  [해외선물 — load_ls_overseas_futures() 자격증명 필요]
  python verify_ls.py --overseas-futures            해외선물 잔고·마스터 raw 캡처 (읽기 전용)
  python verify_ls.py --overseas-futures --order    위 + 금선물 1계약 매수→체결조회→정리

  [전체 — 자격증명 있는 자산군 모두 읽기 전용 프로브]
  python verify_ls.py --all

⚠ 읽기 전용 프로브는 자격증명이 있는 자산군에 대해 항상 안전 (주문 없음).
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
from localapp.ls_futures_broker import LsFuturesBroker  # noqa: E402
from localapp.ls_futures_contracts import LsContractResolver  # noqa: E402
from localapp.secrets_store import (  # noqa: E402
    get_active_broker, load_ls, load_ls_futures, load_ls_overseas_futures,
)

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


def _make_capturing_post(auth_obj, captured, label="last"):
    """auth_obj._post 를 래핑해 raw 요청/응답을 captured[label] 에 저장하는 버전으로 교체.
    원본 _post 를 반환해 복원·직접 호출에 사용한다."""
    orig = auth_obj._post

    def _capturing(path, tr_cd, body, **kw):
        try:
            r = orig(path, tr_cd, body, **kw)
            captured[label] = {"path": path, "tr_cd": tr_cd, "request_body": body,
                               "response": r, "error": None}
            return r
        except Exception as e:
            captured[label] = {"path": path, "tr_cd": tr_cd, "request_body": body,
                               "response": None, "error": repr(e)}
            raise

    auth_obj._post = _capturing
    return orig


def _show_last(captured, label, title, secret_values):
    if captured.get(label):
        _dump(title, captured[label], secret_values)
        captured[label] = None


def _collect_secret_values(creds_dict: dict | None) -> set[str]:
    if not creds_dict:
        return set()
    sv = {
        str(creds_dict.get("account_no", "")),
        str(creds_dict.get("account_no", "")).replace("-", ""),
        str(creds_dict.get("app_key", "")),
        str(creds_dict.get("app_secret", "")),
    }
    sv.discard("")
    return sv


def main():
    do_order = "--order" in sys.argv
    do_kosdaq = ("--kosdaq" in sys.argv) or do_order
    do_futures = "--futures" in sys.argv or "--all" in sys.argv
    do_overseas_stock = "--overseas-stock" in sys.argv or "--all" in sys.argv
    do_overseas_futures = "--overseas-futures" in sys.argv or "--all" in sys.argv
    # 자산군 플래그가 하나도 없으면 기존 동작(국내주식만)
    any_asset_flag = any(a in sys.argv for a in ("--futures", "--overseas-stock",
                                                  "--overseas-futures", "--all"))
    do_domestic_stock = (not any_asset_flag)

    # ── 자격증명 로드 ──────────────────────────────────────────────────────────
    creds_stock = load_ls()
    creds_fut = load_ls_futures()
    creds_ovf = load_ls_overseas_futures()

    # 국내주식 플래그 활성 시 자격증명 필수
    if do_domestic_stock and not creds_stock:
        print("LS 자격증명이 없습니다. 먼저 python desktop.py → 설정에서 [LS증권] 키를 등록하세요.")
        sys.exit(1)

    if get_active_broker() != "ls" and do_domestic_stock:
        print(f"⚠ 활성 브로커가 'ls'가 아닙니다(현재 '{get_active_broker()}'). "
              "설정 wizard 에서 LS 선택·저장 시 활성화됩니다. (프로브는 그대로 진행)")

    # 전체 secret_values — 세 자격증명 세트 모두 마스킹
    secret_values = (
        _collect_secret_values(creds_stock)
        | _collect_secret_values(creds_fut)
        | _collect_secret_values(creds_ovf)
    )

    # ── 브로커 생성 (자격증명이 있을 때만) ────────────────────────────────────
    broker: LsBroker | None = None
    fbroker: LsFuturesBroker | None = None
    resolver: LsContractResolver | None = None

    if creds_stock and (do_domestic_stock or do_overseas_stock):
        broker = LsBroker()
        if not broker.virtual and do_order and not _confirm_real():
            print("중단.")
            sys.exit(1)

    need_fbroker = (do_futures and creds_fut) or (do_overseas_futures and creds_ovf)
    if need_fbroker:
        fbroker = LsFuturesBroker()
        resolver = LsContractResolver(fbroker)
        if not fbroker.virtual and do_order and not _confirm_real():
            print("중단.")
            sys.exit(1)

    # ── _post 캡처 래퍼 설치 ──────────────────────────────────────────────────
    captured: dict = {}

    if broker:
        orig_broker_post = _make_capturing_post(broker, captured, "broker_last")

    if fbroker:
        orig_fbroker_post = _make_capturing_post(fbroker, captured, "fbroker_last")
        # 해외선물 컨텍스트(_ov)도 별도 래핑 — o3101 등 캡처
        if fbroker._ov is not None:
            orig_ov_post = _make_capturing_post(fbroker._ov, captured, "ov_last")

    def show_broker(title):
        _show_last(captured, "broker_last", title, secret_values)

    def show_fbroker(title):
        _show_last(captured, "fbroker_last", title, secret_values)

    def show_ov(title):
        _show_last(captured, "ov_last", title, secret_values)

    # ── 국내주식 프로브 ────────────────────────────────────────────────────────
    if do_domestic_stock:
        _probe_domestic_stock(
            broker, captured, secret_values,
            show_broker, orig_broker_post,
            do_kosdaq, do_order,
        )

    # ── 국내선물 프로브 ────────────────────────────────────────────────────────
    if do_futures:
        if not creds_fut:
            print("\n[국내선물] skip: 자격증명 없음 (load_ls_futures() — setup wizard에서 LS선물 키 등록 필요)")
        elif not fbroker.domestic_configured:
            print("\n[국내선물] skip: LsFuturesBroker 국내 자격증명 미설정 (domestic_configured=False)")
        else:
            _probe_futures(fbroker, resolver, captured, secret_values,
                           show_fbroker, do_order)

    # ── 해외주식 프로브 ────────────────────────────────────────────────────────
    if do_overseas_stock:
        if not creds_stock:
            print("\n[해외주식] skip: 자격증명 없음 (load_ls() — setup wizard에서 LS증권 키 등록 필요)")
        else:
            _probe_overseas_stock(broker, captured, secret_values,
                                  show_broker, do_order)

    # ── 해외선물 프로브 ────────────────────────────────────────────────────────
    if do_overseas_futures:
        if not creds_ovf:
            print("\n[해외선물] skip: 자격증명 없음 (load_ls_overseas_futures() — setup wizard에서 LS해외선물 키 등록 필요)")
        elif fbroker is None or not fbroker.overseas_configured:
            print("\n[해외선물] skip: LsFuturesBroker 해외 자격증명 미설정 (overseas_configured=False)")
        else:
            _probe_overseas_futures(fbroker, resolver, captured, secret_values,
                                    show_fbroker, show_ov, do_order)

    print("\n[완료] 위 ===== RAW: ... ===== 블록 전체를 복사해 전달하면 ⚠ 필드를 실측 확정합니다.")


# ─────────────────────────────────────────────────────────────────────────────
# 국내주식 프로브 (기존 동작 보존)
# ─────────────────────────────────────────────────────────────────────────────

def _probe_domestic_stock(broker, captured, secret_values,
                           show_broker, orig_broker_post,
                           do_kosdaq, do_order):
    mode = "모의투자" if broker.virtual else "⚠실전투자⚠"
    tail = ("…" + broker.account_no[-2:]) if broker.account_no else "?"
    print(f"\nLS 실연동 검증 — {mode} (계좌 {tail})")

    # [1] 토큰
    broker._token()
    print("\n[1] OAuth 토큰 발급 OK (토큰 값은 출력하지 않음)")

    # [2] 잔고 (t0424)
    print("\n[2] 잔고 조회 (t0424)")
    snap = None
    try:
        snap = broker.account_snapshot()
    except Exception as e:
        print(f"   account_snapshot 예외: {e!r}")
    show_broker("t0424 주식잔고 (path / tr_cd / 요청 / 응답)")
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
    show_broker(f"t1102 시세 {KOSPI_TICKER}")
    print(f"   파싱결과 → 현재가(price) {px:,.0f} · 시가(open) {op:,.0f}")
    if px == 0:
        print("   ⚠ price=0 — RAW 에서 현재가 필드명이 'price'와 다른지 확인.")

    # [3b] KOSDAQ
    if do_kosdaq:
        print(f"\n[3b] 시세 조회 (t1102) — 카카오 {KOSDAQ_TICKER} (KOSDAQ · exchgubun 확정)")
        pxk = 0.0
        try:
            pxk = broker.price(KOSDAQ_TICKER)
        except Exception as e:
            print(f"   price 예외: {e!r}")
        show_broker(f"t1102 시세 {KOSDAQ_TICKER} (KOSDAQ)")
        print(f"   파싱결과 → KOSDAQ 현재가 {pxk:,.0f}")
        if pxk == 0:
            print("   ⚠ KOSDAQ 현재가 0 — t1102 InBlock 에 exchgubun='Q' 필요 가능성(G21). "
                  "RAW 확인 후 _price_raw 에 exchgubun 추가 검토.")

    # [4] 미체결 (t0425)
    print("\n[4] 미체결 조회 (t0425 chegb=2)")
    pend = []
    try:
        pend = broker.pending_orders()
    except Exception as e:
        print(f"   pending_orders 예외: {e!r}")
    show_broker("t0425 미체결 (chegb=2)")
    print(f"   파싱결과 → 미체결 {len(pend)}건")

    # [5] 주문 라운드트립
    if do_order:
        _order_roundtrip(broker, orig_broker_post, captured, secret_values,
                         lambda t: _show_last(captured, "broker_last", t, secret_values))
    else:
        print("\n[5] 주문 라운드트립 생략 (--order 로 활성화 — 모의·장중 09:00~15:20)")


# ─────────────────────────────────────────────────────────────────────────────
# 국내주식 주문 라운드트립 (기존 동작 보존, _show_last 시그니처 어댑터 추가)
# ─────────────────────────────────────────────────────────────────────────────

def _order_roundtrip(broker, orig_post, captured, secret_values, _show_last_fn):
    """모의 1주 시장가 매수 → 체결조회 폴링 → t0425 chegb=0 status 실측 → 보유 시 매도 / 미체결 시 취소."""
    sym = KOSPI_TICKER
    print(f"\n[5] 모의 주문 라운드트립 — {sym} 시장가 1주 매수")
    print("    (장중 09:00~15:20 에만 체결. 장외면 미체결로 남아 [5d]에서 취소됩니다.)")

    # [5a] 매수
    try:
        r = broker.buy(sym, 1)
    except Exception as e:
        _show_last_fn("CSPAT00601 매수 (요청/응답 — 예외)")
        print(f"   ⚠ 매수 예외: {e!r}")
        return
    _show_last_fn("CSPAT00601 매수 응답 (★성공코드 rsp_cd · OrdNo 필드 확정★)")
    print(f"   정규화결과 → success={r['success']} · order_no='{r['order_no']}' · "
          f"msg_cd='{r['msg_cd']}' · {r['message']}")
    if not r["success"] or not r["order_no"]:
        print("   ⚠ 매수 미접수. 위 RAW 에서 실제 OrdNo 블록/필드명과 rsp_cd 성공값 확인(G17/G11). 중단.")
        return
    order_no = r["order_no"]

    # [5b] 체결조회 폴링
    print(f"\n[5b] 체결 조회 — order_status('{order_no}') 폴링 3회")
    for i in range(3):
        time.sleep(1.5)
        try:
            st = broker.order_status(order_no, symbol=sym)
            print(f"   폴링{i + 1} → status={st['status']} · filled={st['filled_qty']} · "
                  f"remain={st['remain_qty']} · fill_price={st['fill_price']}")
        except Exception as e:
            print(f"   폴링{i + 1} 예외: {e!r}")
    _show_last_fn("t0425 order_status 폴링 마지막 raw (chegb=2)")

    # [5c] t0425 chegb=0 전체조회 — status 필드 실측
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

    # [5d] 정리
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
            _show_last_fn("CSPAT00601 매도 응답")
            print(f"   매도 → success={rs['success']} · order_no='{rs['order_no']}' · {rs['message']}")
        except Exception as e:
            print(f"   ⚠ 매도 예외: {e!r}")
    else:
        print("   보유 0주(미체결 추정) → 매수 취소 시도")
        try:
            rc = broker.cancel(order_no, sym, 1)
            _show_last_fn("CSPAT00801 취소 응답")
            print(f"   취소 → success={rc['success']} · {rc['message']}")
        except Exception as e:
            print(f"   ⚠ 취소 예외: {e!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 국내선물 프로브
# ─────────────────────────────────────────────────────────────────────────────

def _probe_futures(fbroker, resolver, captured, secret_values, show_fbroker, do_order):
    mode = "모의투자" if fbroker.virtual else "⚠실전투자⚠"
    tail = ("…" + fbroker.account_no[-2:]) if fbroker.account_no else "?"
    print(f"\n━━ 국내선물 프로브 — {mode} (계좌 {tail}) ━━")

    # [F1] 잔고 (CFOAQ50600 + t0441)
    print("\n[F1] 선물 잔고 조회 (CFOAQ50600 + t0441)")
    # CFOAQ50600(평가TR) 단독 dump — 실전 EvalDpsamtTotamt 제공 여부·equity 정확성 확정.
    # (모의는 01900 미제공→OrdAbleAmt 근사로 가짜하락. 실전 제공 시 평가 정확·equity #3 자동해소.)
    try:
        fbroker._acct_summary_raw()
        show_fbroker("CFOAQ50600 예탁·증거금 (★ EvalDpsamtTotamt 제공 여부 — equity 정확성)")
    except Exception as e:
        print(f"   CFOAQ50600 예외: {e!r}")
    fsnap = None
    try:
        fsnap = fbroker.account_snapshot()
    except Exception as e:
        print(f"   account_snapshot 예외: {e!r}")
    show_fbroker("t0441 포지션 (path / tr_cd / 요청 / 응답)")
    if fsnap:
        acct = fsnap["account"]
        print(f"   파싱결과 → equity {acct['equity']:,} · order_cash {acct['order_cash']:,} · "
              f"포지션 {len(fsnap['positions'])}건")
        if acct["equity"] == 0:
            print("   ⚠ equity=0 — RAW 에서 EvalDpsamtTotamt 필드명 확인 필요.")

    # [F2] 마스터(t8432) + resolve("코스피200선물")
    print("\n[F2] 지수선물 마스터 (t8432) + resolve('코스피200선물')")
    code = None
    try:
        code = resolver.resolve("코스피200선물")
    except Exception as e:
        print(f"   resolver.resolve 예외: {e!r}")
    show_fbroker("t8432 지수선물마스터 (hname·shcode 형식 확정)")
    if code:
        print(f"   resolve('코스피200선물') → '{code}'")
    else:
        print("   ⚠ resolve None — t8432 hname 형식이 _HNAME_YM 정규식과 불일치 가능(G-DF9). "
              "RAW 의 hname 형식 확인 후 ls_futures_contracts.py 교정 필요.")

    # [F2.5] 주문가능수량 (CFOAQ10100 NewOrdAbleQty) — 모델 A 라이브 사이징 토대 (읽기전용·무발주)
    if code:
        print(f"\n[F2.5] 주문가능수량 (CFOAQ10100 NewOrdAbleQty) — {code} · 모델 A 사이징 토대")
        ref = 0.0
        try:
            ref = fbroker.price(code) or 0.0
        except Exception:
            ref = 0.0
        for side in ("buy", "sell"):
            try:
                q = fbroker.orderable_qty(code, side, ref)
                print(f"   orderable_qty({side}, 기준가 {ref:,.0f}) → {q} 계약")
            except Exception as e:
                print(f"   orderable_qty({side}) 예외: {e!r}")
        show_fbroker("CFOAQ10100 주문가능수량 (★ NewOrdAbleQty 필드 존재·값 확정 — 모델 A 토대)")
    else:
        print("\n[F2.5] 주문가능수량 생략 (code=None)")

    # [F3] 시세 (t2101)
    if code:
        print(f"\n[F3] 선물 시세 (t2101) — {code}")
        fpx = fop = 0.0
        try:
            fpx = fbroker.price(code)
            fop = fbroker.today_open(code)
        except Exception as e:
            print(f"   price 예외: {e!r}")
        show_fbroker(f"t2101 선물시세 {code}")
        print(f"   파싱결과 → 현재가 {fpx:,.0f} · 시가 {fop:,.0f}")
        if fpx == 0:
            print("   ⚠ price=0 — t2101OutBlock.price 필드명 확인.")
    else:
        print("\n[F3] 선물 시세 생략 (code=None)")

    # [F4] 주문 라운드트립
    if do_order:
        _futures_roundtrip(fbroker, resolver, captured, secret_values,
                           show_fbroker)
    else:
        print("\n[F4] 주문 라운드트립 생략 (--order 로 활성화 — 모의·장중)")


def _futures_roundtrip(fbroker, resolver, captured, secret_values, show_fbroker):
    """국내선물 모의 1계약 시장가 매수 → 체결조회 → 정리."""
    print("\n[F4] 국내선물 모의 주문 라운드트립 — 코스피200선물 시장가 1계약")
    print("    (장중 09:00~15:20 에만 체결. 장외면 미체결로 남아 취소됩니다.)")

    code = None
    try:
        code = resolver.resolve("코스피200선물")
    except Exception as e:
        print(f"   ⚠ resolve 예외: {e!r}. 중단.")
        return
    if not code:
        print("   ⚠ resolve('코스피200선물') = None — 마스터 미수신 또는 hname 형식 불일치. 중단.")
        return

    # [F4a] 매수 — CFOAT00100
    try:
        r = fbroker.buy(code, 1)
    except Exception as e:
        show_fbroker("CFOAT00100 선물매수 (예외)")
        print(f"   ⚠ buy 예외: {e!r}")
        return
    show_fbroker("CFOAT00100 선물매수 응답 (★OrdNo 필드 확정★)")
    print(f"   정규화결과 → success={r['success']} · order_no='{r['order_no']}' · "
          f"msg_cd='{r['msg_cd']}' · {r['message']}")
    if not r["success"] or not r["order_no"]:
        print("   ⚠ 매수 미접수. RAW OrdNo/rsp_cd 확인. 중단.")
        return
    order_no = r["order_no"]

    # [F4b] 체결조회 폴링 — t0434
    print(f"\n[F4b] 체결 조회 — order_status('{order_no}') 폴링 3회")
    for i in range(3):
        time.sleep(1.5)
        try:
            st = fbroker.order_status(order_no)
            print(f"   폴링{i + 1} → status={st['status']} · filled={st['filled_qty']} · "
                  f"remain={st['remain_qty']} · fill_price={st['fill_price']}")
        except Exception as e:
            print(f"   폴링{i + 1} 예외: {e!r}")
    show_fbroker("t0434 order_status 폴링 마지막 raw")

    # [F4c] 정리 — 보유 시 매도, 미체결 시 취소
    print("\n[F4c] 정리 — 보유 포지션 시 매도, 미체결이면 취소")
    held = 0
    try:
        fsnap = fbroker.account_snapshot()
        for p in fsnap["positions"]:
            if p["symbol"] == code:
                held = p["qty"]
    except Exception as e:
        print(f"   잔고 재조회 예외: {e!r}")
    if held >= 1:
        try:
            rs = fbroker.sell(code, 1)
            show_fbroker("CFOAT00100 선물매도 응답")
            print(f"   매도 → success={rs['success']} · order_no='{rs['order_no']}' · {rs['message']}")
        except Exception as e:
            print(f"   ⚠ 매도 예외: {e!r}")
    else:
        print("   보유 0계약(미체결 추정) → 매수 취소 시도")
        try:
            rc = fbroker.cancel(order_no, code, 1)
            show_fbroker("CFOAT00300 선물취소 응답")
            print(f"   취소 → success={rc['success']} · {rc['message']}")
        except Exception as e:
            print(f"   ⚠ 취소 예외: {e!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 해외주식 프로브
# ─────────────────────────────────────────────────────────────────────────────

def _probe_overseas_stock(broker, captured, secret_values, show_broker, do_order):
    mode = "모의투자" if broker.virtual else "⚠실전투자⚠"
    tail = ("…" + broker.account_no[-2:]) if broker.account_no else "?"
    print(f"\n━━ 해외주식 프로브 — {mode} (계좌 {tail}) ━━")

    # [OS1] 해외 잔고 (COSOQ00201)
    print("\n[OS1] 해외주식 잔고 조회 (COSOQ00201)")
    ovsnap = None
    try:
        ovsnap = broker.overseas_snapshot()
    except Exception as e:
        print(f"   overseas_snapshot 예외: {e!r}")
    show_broker("COSOQ00201 해외잔고 (USD 예수금·환율·보유종목)")
    if ovsnap:
        print(f"   파싱결과 → usd_cash={ovsnap['usd_cash']:,.2f} · fx_usdkrw={ovsnap['fx_usdkrw']:,.2f} · "
              f"foreign_eval_krw={ovsnap['foreign_eval_krw']:,.0f} · 보유 {len(ovsnap['positions'])}종목")
        if ovsnap["fx_usdkrw"] == 0:
            print("   ⚠ fx_usdkrw=0 — COSOQ00201OutBlock3.BaseXchrat 필드명 확인.")
        if ovsnap["fx_usdkrw"] > 0 and ovsnap["foreign_eval_krw"] == 0 and len(ovsnap["positions"]) > 0:
            print("   ⚠ 보유 있는데 foreign_eval_krw=0 — eval_price 필드(OvrsScrtsCurpri) 확인.")

    # [OS2] AAPL 시세 (g3101)
    print("\n[OS2] 해외주식 시세 (g3101) — AAPL (NASDAQ)")
    opx = oop = 0.0
    try:
        opx = broker.price("AAPL")
        oop = broker.today_open("AAPL")
    except Exception as e:
        print(f"   price 예외: {e!r}")
    show_broker("g3101 해외시세 AAPL")
    print(f"   파싱결과 → 현재가 {opx:,.2f} USD · 시가 {oop:,.2f} USD")
    if opx == 0:
        print("   ⚠ price=0 — g3101OutBlock.price 필드명 확인.")

    # [OS3] 주문 라운드트립
    if do_order:
        _overseas_stock_roundtrip(broker, captured, secret_values, show_broker)
    else:
        print("\n[OS3] 주문 라운드트립 생략 (--order 로 활성화 — 모의·미국장중 23:30~06:00 KST)")


def _overseas_stock_roundtrip(broker, captured, secret_values, show_broker):
    """해외주식 모의 AAPL 1주 매수 → 체결조회 → 정리."""
    sym = "AAPL"
    print(f"\n[OS3] 해외주식 모의 주문 라운드트립 — {sym} 1주 매수")
    print("    (미국 장중 23:30~06:00 KST 에만 체결. 장외면 미체결로 남아 취소됩니다.)")

    # [OS3a] 매수 — COSAT00301
    try:
        r = broker.buy(sym, 1)
    except Exception as e:
        show_broker("COSAT00301 해외주식매수 (예외)")
        print(f"   ⚠ buy 예외: {e!r}")
        return
    show_broker("COSAT00301 해외주식매수 응답 (★OrdNo 필드 확정★)")
    print(f"   정규화결과 → success={r['success']} · order_no='{r['order_no']}' · "
          f"msg_cd='{r['msg_cd']}' · {r['message']}")
    if not r["success"] or not r["order_no"]:
        print("   ⚠ 매수 미접수. RAW OrdNo/rsp_cd 확인. 중단.")
        return
    order_no = r["order_no"]

    # [OS3b] 체결조회 폴링 — COSAQ00102
    print(f"\n[OS3b] 체결 조회 — order_status('{order_no}', symbol='{sym}') 폴링 3회")
    for i in range(3):
        time.sleep(1.5)
        try:
            st = broker.order_status(order_no, symbol=sym)
            print(f"   폴링{i + 1} → status={st['status']} · filled={st['filled_qty']} · "
                  f"remain={st['remain_qty']} · fill_price={st['fill_price']}")
        except Exception as e:
            print(f"   폴링{i + 1} 예외: {e!r}")
    show_broker("COSAQ00102 order_status 폴링 마지막 raw")

    # [OS3c] 정리
    print("\n[OS3c] 정리 — 보유 시 1주 매도, 미체결이면 취소")
    held = 0
    try:
        ovsnap = broker.overseas_snapshot()
        for p in ovsnap["positions"]:
            if p["symbol"].upper() == sym.upper():
                held = p["qty"]
    except Exception as e:
        print(f"   잔고 재조회 예외: {e!r}")
    if held >= 1:
        try:
            rs = broker.sell(sym, 1)
            show_broker("COSAT00301 해외주식매도 응답")
            print(f"   매도 → success={rs['success']} · order_no='{rs['order_no']}' · {rs['message']}")
        except Exception as e:
            print(f"   ⚠ 매도 예외: {e!r}")
    else:
        print("   보유 0주(미체결 추정) → 매수 취소 시도")
        try:
            rc = broker.cancel(order_no, sym, 1)
            show_broker("COSAT00301 해외주식취소 응답")
            print(f"   취소 → success={rc['success']} · {rc['message']}")
        except Exception as e:
            print(f"   ⚠ 취소 예외: {e!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 해외선물 프로브
# ─────────────────────────────────────────────────────────────────────────────

def _probe_overseas_futures(fbroker, resolver, captured, secret_values,
                             show_fbroker, show_ov, do_order):
    mode = "모의투자" if fbroker.virtual else "⚠실전투자⚠"
    tail = ("…" + fbroker.account_no[-2:]) if fbroker.account_no else "?"
    print(f"\n━━ 해외선물 프로브 — {mode} (계좌 {tail}) ━━")

    # [OF1] 해외선물 잔고 (CIDBQ03000 + CIDBQ05300 + CIDBQ01500)
    print("\n[OF1] 해외선물 잔고 조회 (CIDBQ03000 + CIDBQ05300 + CIDBQ01500)")
    ovfsnap = None
    try:
        ovfsnap = fbroker.overseas_account_snapshot()
    except Exception as e:
        # Xchrat<=0 killswitch raise 포함 — raw 확인을 위해 show_ov 호출
        show_ov("CIDBQ05300 환율조회 raw (Xchrat 필드 확인)")
        show_fbroker("CIDBQ03000/CIDBQ01500 해외선물잔고 raw")
        print(f"   ⚠ overseas_account_snapshot 예외: {e!r}")
        print("   ⚠ Xchrat<=0 이면 환율 TR(CIDBQ05300) 응답 위 RAW 확인.")
    if ovfsnap:
        acct = ovfsnap["account"]
        print(f"   파싱결과 → equity(KRW) {acct['equity']:,.0f} · "
              f"order_cash(KRW) {acct['order_cash']:,.0f} · "
              f"fx_usdkrw {acct.get('fx_usdkrw', 0):,.2f} · "
              f"포지션 {len(ovfsnap['positions'])}건")
        if acct["equity"] == 0:
            print("   ⚠ equity=0 — CIDBQ03000OutBlock2.EvalAssetAmt 필드명 확인.")
    # 각 TR raw 덤프
    show_ov("CIDBQ05300 환율조회 raw (Xchrat 필드)")
    show_fbroker("CIDBQ03000+CIDBQ01500 해외선물잔고 raw")

    # [OF2] 해외선물 마스터 (o3101) + resolve("금선물")
    print("\n[OF2] 해외선물 마스터 (o3101) + resolve('금선물')")
    ofcode = None
    try:
        ofcode = resolver.resolve("금선물")
    except Exception as e:
        print(f"   resolver.resolve 예외: {e!r}")
    show_ov("o3101 해외선물마스터 (BscGdsCd·Symbol 형식 확인)")
    if ofcode:
        print(f"   resolve('금선물') → '{ofcode}'")
    else:
        print("   ⚠ resolve None — o3101 BscGdsCd 필드가 CME root(GC 등)과 불일치 가능. "
              "RAW 의 BscGdsCd 값 확인 후 OVERSEAS_ROOTS 또는 _pick_front_overseas 교정 필요.")

    # [OF3] 주문 라운드트립
    if do_order:
        _overseas_futures_roundtrip(fbroker, resolver, captured, secret_values,
                                    show_fbroker, show_ov)
    else:
        print("\n[OF3] 주문 라운드트립 생략 (--order 로 활성화 — 모의·CME 장중)")


def _overseas_futures_roundtrip(fbroker, resolver, captured, secret_values,
                                 show_fbroker, show_ov):
    """해외선물 모의 금선물 1계약 매수 → 체결조회 → 정리."""
    print("\n[OF3] 해외선물 모의 주문 라운드트립 — 금선물 시장가 1계약")
    print("    (CME 장중에만 체결. 장외면 미체결로 남아 취소됩니다.)")

    ofcode = None
    try:
        ofcode = resolver.resolve("금선물")
    except Exception as e:
        print(f"   ⚠ resolve 예외: {e!r}. 중단.")
        return
    if not ofcode:
        print("   ⚠ resolve('금선물') = None — 마스터 미수신 또는 BscGdsCd 불일치. 중단.")
        return

    # [OF3a] 매수 — CIDBT00100 via _ov._post
    try:
        r = fbroker.overseas_buy(ofcode, 1)
    except Exception as e:
        show_ov("CIDBT00100 해외선물매수 (예외)")
        print(f"   ⚠ overseas_buy 예외: {e!r}")
        return
    show_ov("CIDBT00100 해외선물매수 응답 (★OvrsFutsOrdNo 필드 확정★)")
    print(f"   정규화결과 → success={r['success']} · order_no='{r['order_no']}' · "
          f"msg_cd='{r['msg_cd']}' · {r['message']}")
    if not r["success"] or not r["order_no"]:
        print("   ⚠ 매수 미접수. RAW OvrsFutsOrdNo/rsp_cd 확인. 중단.")
        return
    order_no = r["order_no"]

    # 원주문일자(OrdDt) 캡처 — overseas_cancel에 필요
    # CIDBT00100 응답에서 OrdDt 추출 시도, 없으면 오늘 날짜 fallback
    from datetime import datetime as _dt
    ord_dt = _dt.now().strftime("%Y%m%d")   # 기본값: 오늘
    last_raw = captured.get("ov_last") or {}
    resp_body = last_raw.get("response") or {}
    for key, val in resp_body.items():
        if isinstance(val, dict) and "OrdDt" in val:
            candidate = str(val["OrdDt"]).strip()
            if candidate and candidate != "0" and len(candidate) == 8:
                ord_dt = candidate
                break

    # [OF3b] 체결조회 폴링 — CIDBQ02400
    print(f"\n[OF3b] 체결 조회 — overseas_order_status('{order_no}') 폴링 3회")
    for i in range(3):
        time.sleep(1.5)
        try:
            st = fbroker.overseas_order_status(order_no)
            print(f"   폴링{i + 1} → status={st['status']} · filled={st['filled_qty']} · "
                  f"remain={st['remain_qty']} · fill_price={st['fill_price']}")
        except Exception as e:
            print(f"   폴링{i + 1} 예외: {e!r}")
    show_ov("CIDBQ02400 overseas_order_status 폴링 마지막 raw")

    # [OF3c] 정리
    print("\n[OF3c] 정리 — 보유 포지션 시 매도, 미체결이면 취소")
    held = 0
    try:
        ovfsnap = fbroker.overseas_account_snapshot()
        for p in ovfsnap["positions"]:
            if p["symbol"] == ofcode:
                held = p["qty"]
    except Exception as e:
        print(f"   잔고 재조회 예외: {e!r}")
    if held >= 1:
        try:
            rs = fbroker.overseas_sell(ofcode, 1)
            show_ov("CIDBT00100 해외선물매도 응답")
            print(f"   매도 → success={rs['success']} · order_no='{rs['order_no']}' · {rs['message']}")
        except Exception as e:
            print(f"   ⚠ 매도 예외: {e!r}")
    else:
        print(f"   보유 0계약(미체결 추정) → 취소 시도 (OrdDt={ord_dt})")
        try:
            rc = fbroker.overseas_cancel(order_no, ofcode, ord_dt)
            show_ov("CIDBT01000 해외선물취소 응답")
            print(f"   취소 → success={rc['success']} · {rc['message']}")
        except Exception as e:
            print(f"   ⚠ 취소 예외: {e!r}")


if __name__ == "__main__":
    main()
