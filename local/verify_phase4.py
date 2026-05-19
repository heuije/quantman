"""Phase 4 검증 - 로컬앱 전체 흐름.

keyring 보관 -> 기기 페어링 -> 전략 풀 -> 모의매매(MockBroker) -> 스냅샷 푸시.
실행 전 플랫폼 서버(localhost:8000)가 떠 있어야 한다.
"""

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 격리된 로컬 데이터 디렉터리 (localapp import 전에 설정)
_TMP = Path(tempfile.mkdtemp(prefix="qp_verify4_"))
os.environ["QP_LOCAL_DIR"] = str(_TMP)
# 검증은 번들된 core 데이터를 사용 (로컬앱 기본값은 사용자 디렉터리)
os.environ["QP_CORE_DATA_DIR"] = str(Path(__file__).resolve().parent.parent / "core" / "data")

import requests  # noqa: E402

import localapp  # noqa: E402, F401  (corepath 등록 — quant_core import 전에 필요)
import quant_core as qc  # noqa: E402
from localapp import pairing, secrets_store, sync_client  # noqa: E402
from localapp.broker import MockBroker  # noqa: E402
from localapp.config import PLATFORM_URL  # noqa: E402
from localapp.trader import Trader  # noqa: E402

EMAIL = "phase4@quant.kr"
PW = "verify123456"


def main():
    # 1) keyring 보관 ──────────────────────────────────────────────────────────
    secrets_store.save_kis("APPKEY-x", "APPSECRET-y", "50001234-01", virtual=True)
    kis = secrets_store.load_kis()
    assert kis and kis["app_key"] == "APPKEY-x" and kis["virtual"] is True
    print("1) keyring KIS 자격증명 저장/조회 OK")

    # 2) 플랫폼 계정 준비 ───────────────────────────────────────────────────────
    requests.post(f"{PLATFORM_URL}/auth/signup",
                  json={"email": EMAIL, "password": PW})       # 있으면 409 무시
    r = requests.post(f"{PLATFORM_URL}/auth/login",
                      json={"email": EMAIL, "password": PW})
    jwt = r.json()["access_token"]
    H = {"Authorization": f"Bearer {jwt}"}
    print("2) 플랫폼 계정 로그인 OK")

    # 3) 기기 페어링 (브라우저 위임 흐름) ──────────────────────────────────────
    info = pairing.start_pairing("검증 PC")
    # 웹에서 사용자가 승인하는 단계를 API로 시뮬레이션
    ap = requests.post(f"{PLATFORM_URL}/auth/device/approve",
                       headers=H, json={"user_code": info["user_code"]})
    assert ap.status_code == 200, ap.text
    token = pairing.poll_for_token(info["device_code"], interval=0.5, timeout=20)
    assert token and secrets_store.load_device_token() == token
    print("3) 기기 페어링 (start->approve->token) OK")

    # 4) paper 전략 생성 후 풀 ─────────────────────────────────────────────────
    definition = {
        "name": "검증 모의전략",
        "trade_symbol": "S&P500",
        # price_level > 0 : 항상 참 -> 결정적으로 매수 신호 발생
        "buy": {"conditions": [{"symbol": "S&P500", "indicator": "price_level",
                                "op": ">", "value": 0}], "logic": "AND"},
        "exit_rules": {"hold_days": 3},
        "amount_pct": 100.0,
    }
    cr = requests.post(f"{PLATFORM_URL}/strategies", headers=H,
                       json={"definition": definition, "run_mode": "paper"})
    assert cr.status_code == 201, cr.text
    strategies = sync_client.pull_strategies()
    assert len(strategies) >= 1 and strategies[0]["run_mode"] == "paper"
    print(f"4) 전략 풀 OK - paper 전략 {len(strategies)}개 수신")

    # 5) 데이터 로드 + MockBroker ──────────────────────────────────────────────
    dataset = qc.load_dataset(with_indicators=True)
    broker = MockBroker(10_000_000, lambda s: float(dataset[s]["Close"].iloc[-1]))
    trader = Trader(broker)
    print(f"5) 데이터셋 {len(dataset)}심볼 로드 + MockBroker 준비 OK")

    # 6) 매수 사이클 ───────────────────────────────────────────────────────────
    d0 = date(2026, 1, 5)
    p1 = trader.cycle(strategies, dataset, today=d0)
    buys = [t for t in p1["trades"] if t["action"] == "buy"]
    assert buys, f"매수 체결 없음: {p1['trades']}"
    assert p1["positions"], "보유 종목 없음"
    print(f"6) 모의매수 OK - {buys[0]['symbol']} {buys[0]['qty']}주, "
          f"평가금액 {p1['balance']['total_eval']:,}원")

    # 7) 스냅샷 푸시 -> 플랫폼 조회 ─────────────────────────────────────────────
    sync_client.push_snapshot(p1)
    snap = requests.get(f"{PLATFORM_URL}/sync/snapshot", headers=H).json()
    assert snap and snap["payload"]["balance"]["total_eval"] == p1["balance"]["total_eval"]
    print("7) 스냅샷 푸시 -> 웹 조회 일치 OK")

    # 8) 보유기간 청산 사이클 ──────────────────────────────────────────────────
    p2 = trader.cycle(strategies, dataset, today=d0 + timedelta(days=4))
    sells = [t for t in p2["trades"] if t["action"] == "sell"]
    assert sells, f"보유기간 매도 없음: {p2['trades']}"
    print(f"8) 보유기간 청산 OK - {sells[0]['symbol']} 매도 (사유: {sells[0]['reason']})")

    # 정리 ────────────────────────────────────────────────────────────────────
    secrets_store.clear()
    assert secrets_store.load_kis() is None
    print("9) keyring 정리 OK")

    print("\n[OK] Phase 4 검증 통과 - 로컬앱 모의투자 흐름 정상")


if __name__ == "__main__":
    main()
