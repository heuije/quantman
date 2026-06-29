"""LS 계좌번호 검증 라이브 캡처 (read-only — 발주 없음).

P5.0: LS 잔고/계좌 TR이 (a) 성공/오류를 어떻게 신호하는지, (b) 잘못된 계좌번호를 거부하는지,
(c) 응답에 실제 계좌식별자가 echo되는지(read-back 가능 필드)를 실데이터로 확정한다.

실행(본인 LS 계좌로):
  python verify_ls_account.py <app_key> <app_secret> <account_no> [--real] [--futures]
  # 2회 권장: ① 올바른 계좌번호 ② 끝자리 바꾼 '틀린' 계좌번호 → 응답 차이를 비교

이 스크립트는 토큰 발급 + 잔고/계좌 조회만 한다(발주 없음). 검증된 production 경로
(LsBroker._balance_raw / LsFuturesBroker._acct_summary_raw)를 그대로 재사용하므로 필드 가정 위험이 없다.
브로커는 keyring 저장 없이 인메모리 자격증명으로 구성한다(__new__ + _LsAuth.__init__ — 인증/HTTP만 초기화).
"""
import json, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import localapp  # noqa: F401

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        print("usage: python verify_ls_account.py <app_key> <app_secret> <account_no> [--real] [--futures]")
        sys.exit(1)
    app_key, app_secret, account_no = args[0], args[1], args[2]
    virtual = "--real" not in sys.argv
    futures = "--futures" in sys.argv
    creds = {"app_key": app_key, "app_secret": app_secret,
             "account_no": account_no, "virtual": virtual}

    if futures:
        from localapp.ls_futures_broker import LsFuturesBroker
        from localapp.ls_broker import _LsAuth
        # _acct_summary_raw는 self._post(=_LsAuth)만 의존 — LsFuturesBroker.__init__이 세팅하는
        # _ov/_dom_configured를 쓰지 않으므로 __new__ + _LsAuth.__init__로 충분(keyring 미저장).
        b = LsFuturesBroker.__new__(LsFuturesBroker)
        _LsAuth.__init__(b, creds)
        print(f"[LS 선물] virtual={virtual} account={account_no}")
        raw = b._acct_summary_raw()   # CFOAQ50600 — 선물 계좌요약
    else:
        from localapp.ls_broker import LsBroker, _LsAuth
        # _balance_raw도 self._post만 의존. _overseas_unavailable는 _balance_raw가 직접 읽지
        # 않지만, LsBroker.__init__이 세팅하는 인스턴스 속성이므로 명시 세팅해 정합 유지.
        b = LsBroker.__new__(LsBroker)
        _LsAuth.__init__(b, creds)
        b._overseas_unavailable = True
        print(f"[LS 주식] virtual={virtual} account={account_no}")
        raw = b._balance_raw()        # t0424 — 주식 잔고

    print("\n--- RAW 응답 (전 필드) ---")
    print(json.dumps(raw, ensure_ascii=False, indent=2, default=str))
    print("\n점검: ① 위 호출이 예외 없이 반환됐는가(=성공 신호) "
          "② 응답에 입력 계좌번호와 매칭되는 필드가 있는가(read-back 후보) "
          "③ 틀린 계좌번호로 재실행 시 예외/오류 코드가 나는가")

if __name__ == "__main__":
    main()
