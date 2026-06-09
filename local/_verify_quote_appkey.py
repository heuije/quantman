"""시세용 실전 앱키 수정 검증 — 모의 주문 앱키 + 실전 시세 앱키 라우팅(읽기 전용·주문 없음).

전제: setup(또는 아래 명령)으로 시세용 실전 앱키를 등록한 상태.
  from localapp.secrets_store import load_kis, save_kis
  c = load_kis()
  save_kis(c['app_key'], c['app_secret'], c['account_no'], virtual=c['virtual'],
           hts_id=c.get('hts_id',''), quote_app_key='<실전APPKEY>', quote_app_secret='<실전SECRET>')

실행: python local/_verify_quote_appkey.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_LOCAL = Path(__file__).resolve().parent
for p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

from localapp.kis_broker import KisBroker

try:
    b = KisBroker()
except RuntimeError as e:
    print("❌ 브로커 생성 실패:", e)
    print("→ 위 docstring의 save_kis 명령으로 시세용 실전 앱키를 먼저 등록하세요.")
    raise SystemExit(1)

print(f"=== KIS ({'모의(VTS)' if b.virtual else '실전(REAL)'}) ===")
print(f"  주문 앱키(앞4): {b.key[:4]}…  /  시세 앱키(앞4): {b.quote_key[:4]}…  (다르면 이중키 정상)")
print(f"  시세 도메인: {b.quote_base}")

print("\n[시세 조회] broker.price() — 모의 계정이라도 실전 시세 앱키로 호출")
ok = True
for s in ["AAPL", "AMZN", "005930"]:        # US + KR
    try:
        px = b.price(s)
        v = "✅ 정상" if px > 0 else "⚠️ 0 (장외·심볼 등)"
        if px <= 0:
            ok = False
        print(f"  {s}: {px}  {v}")
    except Exception as e:
        ok = False
        print(f"  {s}: ❌ 실패 — {str(e)[:100]}")

print("\n" + "=" * 60)
if ok:
    print("판정: ✅ 실전 시세 앱키로 US·KR 현재가 정상 수신 — 수정 검증 통과.")
    print("      (모의 주문 + 실전 시세 라우팅 동작. EGW02004 해소.)")
else:
    print("판정: ⚠️ 일부 0/실패 — 출력(특히 EGW 메시지)을 공유해 주세요.")
