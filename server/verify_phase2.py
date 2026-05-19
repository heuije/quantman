"""Phase 2 검증 - API 서버 전체 흐름을 TestClient로 점검.

가입 -> 전략 CRUD -> 백테스트/분석 -> 기기 페어링 -> 동기화 push/pull 까지.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 격리된 테스트 DB 사용 (app import 전에 설정해야 함)
_TEST_DB = Path(tempfile.gettempdir()) / "qp_verify_phase2.db"
_TEST_DB.unlink(missing_ok=True)
os.environ["QP_DB_URL"] = f"sqlite:///{_TEST_DB}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main():
    with TestClient(app) as c:
        # 1) health
        assert c.get("/health").json()["status"] == "ok"
        print("1) health OK")

        # 2) 회원가입 -> 토큰
        r = c.post("/auth/signup", json={"email": "test@quant.kr", "password": "pw123456"})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}
        print("2) 회원가입 + JWT 발급 OK")

        # 3) 내 정보
        assert c.get("/auth/me", headers=H).json()["email"] == "test@quant.kr"
        print("3) /auth/me OK")

        # 4) 심볼/지표 목록
        syms = c.get("/symbols", headers=H).json()["symbols"]
        # 0을 자주 넘나드는 수익률 계열 지표를 가진 매수대상을 고른다
        def _ret_indic(s):
            return next((i["key"] for i in s["indicators"]
                         if "pct_change" in i["key"] or "return" in i["key"]), None)
        tradable = next(s for s in syms if s["tradable"] and _ret_indic(s))
        symbol = tradable["symbol"]
        indic = _ret_indic(tradable)
        print(f"4) /symbols OK - {len(syms)}개, 매수대상 후보={symbol}, 지표={indic}")

        # 5) 전략 정의 (core Strategy 형태)
        definition = {
            "name": "검증 전략",
            "trade_symbol": symbol,
            "buy": {"conditions": [{"symbol": symbol, "indicator": indic,
                                    "op": "<", "value": 0.0}], "logic": "AND"},
            "exit_rules": {"hold_days": 5, "stop_loss": -5.0},
            "amount_pct": 100.0,
        }

        # 6) 전략 CRUD
        r = c.post("/strategies", headers=H,
                   json={"definition": definition, "run_mode": "draft"})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        assert len(c.get("/strategies", headers=H).json()) == 1
        assert c.get(f"/strategies/{sid}", headers=H).json()["name"] == "검증 전략"
        r = c.put(f"/strategies/{sid}", headers=H,
                  json={"definition": definition, "run_mode": "paper"})
        assert r.status_code == 200 and r.json()["run_mode"] == "paper"
        print("5) 전략 CRUD (생성/조회/목록/수정) OK")

        # 7) 백테스트
        r = c.post("/backtest/run", headers=H,
                   json={"strategy": definition, "initial_capital": 10_000_000})
        bt = r.json()
        assert bt["success"], bt
        m = bt["metrics"]
        print(f"6) 백테스트 OK - 거래 {m['n_trades']}회, "
              f"수익률 {m['total_return']:.1f}%, 자산곡선 {len(bt['equity'])}점")

        # 8) 데이터분석
        r = c.post("/analysis/run", headers=H, json={
            "conditions": [{"symbol": symbol, "indicator": indic,
                            "op": "<", "value": 0.0}],
            "logic": "AND", "target_symbol": symbol,
            "target_indicator": indic, "forward_days": 1,
        })
        an = r.json()
        assert an["success"], an
        print(f"7) 데이터분석 OK - 표본 {an['n_samples']}개, "
              f"양수확률 {an['prob_positive']:.1f}%")

        # 9) 기기 페어링 (OAuth 기기 그랜트)
        r = c.post("/auth/device/start", json={"device_name": "검증용 PC"})
        pair = r.json()
        dcode, ucode = pair["device_code"], pair["user_code"]
        # 승인 전 폴링 -> pending
        assert c.post("/auth/device/token",
                      json={"device_code": dcode}).json()["status"] == "pending"
        # 웹에서 사용자가 승인
        r = c.post("/auth/device/approve", headers=H, json={"user_code": ucode})
        assert r.status_code == 200, r.text
        # 승인 후 폴링 -> 기기 토큰 발급
        r = c.post("/auth/device/token", json={"device_code": dcode})
        dt = r.json()
        assert dt["status"] == "approved" and dt["device_token"]
        device_token = dt["device_token"]
        DH = {"Authorization": f"Bearer {device_token}"}
        assert len(c.get("/auth/devices", headers=H).json()) == 1
        print("8) 기기 페어링 (start->pending->approve->token) OK")

        # 10) 동기화 - 로컬앱이 푸시
        r = c.post("/sync/push", headers=DH, json={"payload": {
            "balance": {"cash": 5_000_000, "total_eval": 5_200_000},
            "positions": [{"symbol": "069500", "qty": 10}],
            "equity": [{"date": "2026-05-19", "value": 10_200_000}],
        }})
        assert r.status_code == 200, r.text
        # 로컬앱이 모의/실전 전략을 풀
        pulled = c.get("/sync/strategies", headers=DH).json()
        assert len(pulled) == 1 and pulled[0]["run_mode"] == "paper"
        # 웹이 스냅샷 조회
        snap = c.get("/sync/snapshot", headers=H).json()
        assert snap["payload"]["balance"]["total_eval"] == 5_200_000
        print("9) 동기화 (push -> pull strategies -> snapshot) OK")

        # 11) 권한 격리 - 토큰 없이 접근 차단
        assert c.get("/strategies").status_code == 401
        assert c.get("/sync/strategies").status_code == 401
        print("10) 인증 격리 (무토큰 401) OK")

        # 12) 전략 삭제
        assert c.delete(f"/strategies/{sid}", headers=H).status_code == 200
        print("11) 전략 삭제 OK")

    from app.db import engine
    engine.dispose()                       # 파일 잠금 해제 후 정리
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass
    print("\n[OK] Phase 2 검증 통과 - API 서버 전체 흐름 정상")


if __name__ == "__main__":
    main()
