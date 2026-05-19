"""Phase 5 - 통합 E2E 검증.

실제 사용자 여정을 처음부터 끝까지 한 번에 따라간다:
  가입 -> 백테스트 -> 전략 저장 -> 로컬앱 페어링 -> 모의투자 -> 웹 동기화 조회.

실행 전 플랫폼 서버(localhost:8000)가 떠 있어야 한다.
  python platform/verify_e2e.py
"""

import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_TMP = Path(tempfile.mkdtemp(prefix="qp_e2e_"))
os.environ["QP_LOCAL_DIR"] = str(_TMP)
os.environ["QP_CORE_DATA_DIR"] = str(Path(__file__).resolve().parent / "core" / "data")
sys.path.insert(0, str(Path(__file__).parent / "local"))

import requests  # noqa: E402

import localapp  # noqa: E402, F401
import quant_core as qc  # noqa: E402
from localapp import pairing, secrets_store, sync_client  # noqa: E402
from localapp.broker import MockBroker  # noqa: E402
from localapp.trader import Trader  # noqa: E402

API = "http://localhost:8000"
EMAIL = f"e2e_{int(time.time())}@quant.kr"
PW = "e2e-verify-123"


def step(n, msg):
    print(f"\n[{n}] {msg}")


def main():
    print("=" * 60)
    print("  통합 E2E 검증 - 사용자 여정 전체")
    print("=" * 60)

    # ── 웹: 가입 ───────────────────────────────────────────────────────────────
    step(1, "신규 사용자가 플랫폼에 가입한다")
    r = requests.post(f"{API}/auth/signup", json={"email": EMAIL, "password": PW})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    print(f"    가입 완료: {EMAIL}")

    # ── 웹: 백테스트로 전략 발견·검증 ─────────────────────────────────────────
    step(2, "백테스트 모듈에서 전략을 만들고 검증한다")
    syms = requests.get(f"{API}/symbols", headers=H).json()["symbols"]
    tgt = next(s for s in syms if s["tradable"]
               and any("pct_change" in i["key"] for i in s["indicators"]))
    indic = next(i["key"] for i in tgt["indicators"] if "pct_change" in i["key"])
    definition = {
        "name": "E2E 모멘텀 전략",
        "trade_symbol": tgt["symbol"],
        "buy": {"conditions": [{"symbol": tgt["symbol"], "indicator": indic,
                                "op": "<", "value": 0.0}], "logic": "AND"},
        "exit_rules": {"hold_days": 5, "stop_loss": -5.0},
        "amount_pct": 100.0,
    }
    bt = requests.post(f"{API}/backtest/run", headers=H,
                       json={"strategy": definition, "initial_capital": 10_000_000}).json()
    assert bt["success"], bt
    print(f"    백테스트: {tgt['symbol']} 거래 {bt['metrics']['n_trades']}회, "
          f"수익률 {bt['metrics']['total_return']:.1f}%")

    # ── 웹: 전략을 모의투자로 저장 ────────────────────────────────────────────
    step(3, "검증한 전략을 모의투자(paper)로 저장한다")
    cr = requests.post(f"{API}/strategies", headers=H,
                       json={"definition": definition, "run_mode": "paper"})
    assert cr.status_code == 201, cr.text
    print(f"    저장 완료: 전략 #{cr.json()['id']} (run_mode=paper)")

    # ── 로컬앱: 설치 후 KIS 자격증명 등록 (keyring) ───────────────────────────
    step(4, "사용자가 로컬앱을 설치하고 KIS 자격증명을 등록한다")
    secrets_store.save_kis("E2E-APPKEY", "E2E-APPSECRET", "50009999-01", virtual=True)
    print("    KIS 모의투자 자격증명을 OS 자격증명 저장소에 보관 (PC 밖으로 안 나감)")

    # ── 로컬앱 <-> 웹: 브라우저 위임 페어링 ───────────────────────────────────
    step(5, "로컬앱을 웹 계정과 페어링한다")
    info = pairing.start_pairing("E2E 데스크탑")
    print(f"    로컬앱 화면에 연결 코드 표시: {info['user_code']}")
    requests.post(f"{API}/auth/device/approve", headers=H,
                  json={"user_code": info["user_code"]})
    print("    사용자가 웹에서 코드 입력 -> 승인")
    pairing.poll_for_token(info["device_code"], interval=0.5, timeout=20)
    print("    로컬앱이 기기 토큰 수신 (키는 전송되지 않음)")

    # ── 로컬앱: 전략 풀 ───────────────────────────────────────────────────────
    step(6, "로컬앱이 모의투자 전략을 가져온다")
    pulled = sync_client.pull_strategies()
    assert any(s["name"] == "E2E 모멘텀 전략" for s in pulled)
    print(f"    수신한 paper 전략 {len(pulled)}개")

    # ── 로컬앱: 모의투자 사이클 (MockBroker로 체험) ──────────────────────────
    step(7, "로컬앱 스케줄러가 전략을 평가하고 모의매매한다")
    dataset = qc.load_dataset(with_indicators=True)
    broker = MockBroker(10_000_000, lambda s: float(dataset[s]["Close"].iloc[-1]))
    trader = Trader(broker)
    payload = trader.cycle(pulled, dataset, today=date(2026, 1, 5))
    print(f"    체결 {len(payload['trades'])}건, "
          f"평가금액 {payload['balance']['total_eval']:,}원, "
          f"보유 {len(payload['positions'])}종목")

    # ── 로컬앱 -> 웹: 안전정보 동기화 ─────────────────────────────────────────
    step(8, "로컬앱이 잔고·수익률을 플랫폼에 동기화한다")
    sync_client.push_snapshot(payload)
    print("    스냅샷 푸시 완료 (API키·주문 원본은 전송 안 함)")

    # ── 웹: 어디서나 대시보드로 조회 ──────────────────────────────────────────
    step(9, "사용자가 웹/모바일 대시보드에서 모의투자 현황을 확인한다")
    snap = requests.get(f"{API}/sync/snapshot", headers=H).json()
    assert snap is not None, "대시보드에 동기화 데이터 없음"
    assert snap["payload"]["balance"]["total_eval"] == payload["balance"]["total_eval"]
    devices = requests.get(f"{API}/auth/devices", headers=H).json()
    assert len(devices) == 1
    print(f"    대시보드 확인: 평가금액 {snap['payload']['balance']['total_eval']:,}원, "
          f"연결 기기 {len(devices)}대")

    # ── 정리 ──────────────────────────────────────────────────────────────────
    secrets_store.clear()

    print("\n" + "=" * 60)
    print("  [OK] 통합 E2E 검증 통과")
    print("  가입 -> 백테스트 -> 전략저장 -> 페어링 -> 모의투자 -> 동기화조회")
    print("  웹과 로컬앱이 안전정보만으로 정상 연동됨")
    print("=" * 60)


if __name__ == "__main__":
    main()
