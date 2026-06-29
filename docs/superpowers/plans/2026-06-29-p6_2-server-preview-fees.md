# P6-2 — 서버 preview 예상수수료 + 선물 레버리지 정보 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec §3.4.

**Goal:** 사전(서버 preview) 투명성 — 주식 후보에 **예상 수수료**, 선물 후보에 **정적 레버리지 정보**(레버리지·승수·증거금률)를 추가. (선물 계약수·금액은 서버가 사이징 못 함 — 발주 시점 로컬[P6-1]이 진실원천.)

**Architecture:** `server/app/preview_engine.py`의 per-candidate dict(주식 line 337-342·선물 line 307-313·미국 316-322)에 필드 추가. 주식 `est_fee_krw` = `est_total × commission_rate`(전략 SimSpec.commission 우선, 없으면 exec_defaults `bt_commission_bps`=3bps). 선물·미국 후보엔 `leverage`/`multiplier`/`margin_rate`(선물만, instrument_spec에서). 새 외부 데이터 0.

**Tech Stack:** FastAPI preview_engine. `quant_core.exec_defaults`(instrument_spec·bt_commission_bps). pytest.

**불변식:** 추가 필드만 — 기존 candidate 소비처(웹 preview·로컬 dataset_scope) 무영향. 레거시·미국·미사이징 후보는 est_fee_krw=None.

---

## Task 1: preview 후보에 예상수수료·레버리지 추가 (TDD)

**Files:**
- Modify: `server/app/preview_engine.py` (per-candidate dict 빌드부 ~307-342)
- Test: `server/tests/test_preview_fees.py` (신규 — 기존 `test_preview_ir.py`/`test_preview_live_basket.py` fixture 재사용)

- [ ] **Step 1: 실패 테스트 작성**

기존 preview 테스트(`test_preview_ir.py`)의 dataset·strategy·preview 호출 패턴 재사용. 케이스:
```python
def test_kr_equity_candidate_has_est_fee(...):
    # 주식 후보 → est_fee_krw ≈ est_total × commission_rate (>0), currency KRW
    cand = <주식 후보>
    assert cand["est_fee_krw"] is not None and cand["est_fee_krw"] > 0
    # 대략: est_total의 commission_rate(기본 0.0003) 배
    assert cand["est_fee_krw"] == approx(cand["est_total"] * rate, rel=0.01)

def test_futures_candidate_has_leverage_info(...):
    # 코스피200선물 후보 → qty None 유지 + leverage 10.0·multiplier 250000·margin_rate 0.10
    cand = <코스피200선물 후보>
    assert cand["qty"] is None
    assert cand["leverage"] == approx(10.0)
    assert cand["multiplier"] == approx(250_000)
    assert cand["margin_rate"] == approx(0.10)
```
> 정확한 fixture·전략·코스피200선물 후보 구성은 `test_preview_ir.py`/`test_preview_live_basket.py`에서 복사.
> commission_rate 출처(SimSpec vs default)는 구현에서 단일 결정 후 테스트가 그 값으로 단언.

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd server && python -m pytest tests/test_preview_fees.py -v`
Expected: FAIL (est_fee_krw/leverage 키 없음).

- [ ] **Step 3: 구현**

- commission_rate 결정(단일 출처): 전략 SimSpec에 commission이 있으면 그 값, 없으면 `exec_defaults`의
  `bt_commission_bps/10000`(3bps=0.0003). (preview의 `s`=StrategyIR에서 simulation/commission 접근 —
  `spec.py`/`exec_defaults` 확인.) helper `_commission_rate(s) -> float`.
- 주식 후보 dict(line 337-342)에 `"est_fee_krw": round(est_price * qty * rate)` 추가.
- 선물 후보 dict(line 307-313)에 `instrument_spec(sym)`에서 `"leverage": round(1/mr,1) if mr else None`,
  `"multiplier": spec.multiplier`, `"margin_rate": mr` 추가(`mr = spec.init_margin_rate`). qty/est_total None 유지.
- 미국 후보(316-322)·미사이징엔 `"est_fee_krw": None`(키 일관 — 소비처 안전).
- 주식 후보에도 `"leverage": None` 키 추가(전 후보 키 셋 일관 — 선택).

- [ ] **Step 4: 통과 + 서버 전체 회귀 + 커밋**

Run: `cd server && python -m pytest tests/test_preview_fees.py -v` → PASS; 이어 `python -m pytest -q` → 전부 pass(기존 preview 테스트 무영향).
```bash
git add server/app/preview_engine.py server/tests/test_preview_fees.py
git commit -m "feat(server): preview 예상수수료(주식)·레버리지 정보(선물) (P6-2)"
```

---

## Self-Review
- **Spec §3.4 사전 투명성:** 주식 예상수수료 + 선물 레버리지·증거금률 → Task 1. (계약수·금액은 로컬 P6-1.)
- **Placeholder:** fixture·commission 출처는 기존 테스트 재사용·단일결정 *명시*.
- **타입 일관성:** est_fee_krw(int|None)·leverage(float|None)·multiplier·margin_rate. 전 후보 분기 키 일관(None로 채움).
- **무영향:** 필드 추가만 — 웹/로컬 candidate 소비처 회귀 없음(서버 전체 스위트 잠금).
