# 청산 타이밍(exit.fill) 1급화 재설계 (2026-07-11)

〔담당: 조대표〕 모듈: 인사이트 엔진(IR 스펙·백테스트) + 자동매매 엔진(라이브 청산 라우팅) + 웹(빌더·표시).
**제1원칙: 기존 연동 전략(실전 포함)은 업데이트 후에도 동작이 1바이트도 변하지 않는다** — 신필드는
opt-in, 기본값(None)은 현행 경로 그대로(무마이그레이션·golden byte-identical 게이트).

---

## 0. 문제 (실측 확정)

1. **표현력 갭**: IR `Exit`(spec.py:88)엔 hold_days·익절·손절·트레일·매도조건뿐 — **청산 시점(시가/종가)
   필드가 없다.** 진입은 `simulation.fill`(next_open/close/typical)로 선택 가능하나 청산 시점은 파생값:
   - 백테스트: 청산도 진입 `fill`과 **같은 바 가격**(backtest.py:98 `defer=(fill=="next_open")` ·
     engine.py:354/931 동일).
   - 라이브: `hold_days==0`→종가창(15:25/15:40), `hold_days≥1`→**아침 시가창**(trader.py
     `_plan_cycle_liquidations`:1832 "hold≥1만"·주석 2194) — `fill`과 무관.
   → "종가매수 → **익일 종가** 매도" 조합이 **어느 계층에서도 표현 불가**(사용자 지적 2026-07-11).
2. **패리티 갭(파생 결함)**: `fill=close` + `hold_days≥1`(오버나이트)에서 백테스트=익일 **종가** 청산,
   라이브=익일 **시가** 청산 — 서로 다른 시점. 실전 전략 #29(mwmw1119·종가매수 익일시가매도)가 해당.
   회원 의도는 시가매도(라이브가 의도와 일치·확인됨) — 즉 **백테스트 쪽이 의도 미표현**. 기존 메모
   "종가매수 라이브 백테스트 패리티 보류"와 동일 항목.

## 1. 결정 (D1~D6)

### D1. 스펙 — `position.exit.fill` 신설 (opt-in·기본 None=legacy)

```python
class Exit(BaseModel):
    ...
    # 청산 체결 시점. None=legacy(진입 fill·hold_days에서 파생 — 현행과 동일).
    # "next_open"=다음 개장 시가, "close"=해당일 종가. 진입 fill과 독립.
    fill: Optional[Literal["next_open", "close"]] = None
```
- 어휘는 기존 `SimSpec.fill`과 동일 계열(next_open/close) — 새 vocabulary 도입 금지(원칙 3).
  `typical`은 청산엔 불허(수요 없음·over-engineering 방지).
- 검증기 추가 제약 없음. 단 기존 degenerate 가드(진입 close+hold 0) 의미는 그대로 —
  `exit.fill="close"`+`hold_days=0`+진입 close도 같은 가드에 걸린다(진입 바=청산 바).
- 의미 매트릭스(신필드 명시 시):

| 진입 fill | exit.fill | hold_days | 의미 | 비고 |
|---|---|---|---|---|
| next_open | next_open | ≥1 | 시가 진입 → N일 후 시가 청산 | 현행 legacy와 동일 결과 |
| next_open | close | 0 | 시가 진입 → 당일 종가 청산 | 현행 당일매매와 동일 결과 |
| next_open | close | ≥1 | 시가 진입 → N일 후 **종가** 청산 | **신규 조합** |
| close | next_open | ≥1 | 종가 진입 → 익일 **시가** 청산 | **신규 — #29 의도의 명시 표현**(백테=라이브) |
| close | close | ≥1 | 종가 진입 → 익일 **종가** 청산 | **신규 — 사용자 요청 조합** |

### D2. 백테스트 엔진 — 청산가 오버라이드 (legacy 분기 보존)

- `exit.fill is None` → **현행 코드 경로 그대로**(진입 fill의 defer/exec 공유) — 골든 byte-identical.
- `exit.fill` 명시 → 청산 체결만 분리:
  - `"close"` → 청산 신호 바의 종가 체결(현 same_day 경로 재사용).
  - `"next_open"` → 익일 시가 체결(현 defer 경로 재사용).
- 구현 지점: 단일종목 루프(backtest.py:98·101·175)와 포트폴리오 루프(engine.py:354·455·931·959)의
  청산가 선택부에 `exit_fill` 파라미터 — **기존 두 메커니즘(defer·same_day)의 재배선이지 신규 기계장치
  아님.** hold_days 산정·수수료·세금 회계는 무변경.

### D3. 라이브 라우팅 — 창 선택자 확장 (기존 두 창 재사용)

현행 선택자: 아침 §2/넷팅 PLAN = `hold_days≥1` · 종가창 = `hold_days==0`. 확장:

- **아침 시가창**(§2 exit pass + `_plan_cycle_liquidations`): 대상 = `exit_fill != "close"`인 보유기간
  도달 포지션 (None 포함 — legacy 유지).
- **종가창**(`liquidate_day_trades` + `plan_close_liquidations`): 대상 = `hold_days==0`(현행) **또는**
  `exit_fill=="close" and held≥hold_days`(신규).
- 판독 소스는 원장 `pos["definition"].position.exit.fill`(원장이 full definition 보유 — 실측 확인).
- 익절·손절·트레일·매도조건 청산은 현행 사이클 평가 그대로(시점 필드는 **시간기반 만기 청산의 체결창**만
  결정 — 조건 청산까지 창을 미루면 위험 확대라 의도적으로 제외·명시).
- 넷팅: 아침·종가 양창 넷팅이 이미 존재 — 선택자만 확장되므로 신규 로직 없음.
- 불변식 I5 확장(**I5+**): 정산 감시 대상에 "오늘 청산됐어야 할 `exit_fill=close` 포지션 잔존" 추가
  (`n_daytrade_unclosed`와 동일 채널) — 종가창 미발화 시 당일 인지 보장.

### D4. 웹·프리뷰

- **빌더**: 청산 섹션에 "청산 시점" 선택(기본='자동(현행)' = 필드 미기록·None). 신규 전략만 명시값 저장.
- **설정값 탭**(#308 투명화): `exitTimingDesc`가 신필드 우선 판독 — "보유 N일 후 다음 개장 — 시가 청산" /
  "보유 N일 후 당일 마감 — 종가 청산". None이면 현행 파생 문구 유지.
- **프리뷰**: exit_candidates 산출부가 legacy 파생을 쓰는지 구현 시 감사(미확인 지점 — 정직 표기).
  NL 컴파일러 쿡북 노출은 P1.5(로컬 릴리스 후 — D5 순서 제약).

### D5. 롤아웃 순서 (기존 전략 안전 보장의 실행 형태)

1. **core+server 랜딩** — 엔진·스펙이 필드를 이해. 기존 전략(필드 없음)엔 완전 불활성.
2. **로컬앱 릴리스(v0.9.7x)** — 청산 라우팅 확장 포함.
3. **웹 빌더·NL 노출은 로컬 릴리스 publish 후** — 순서 강제 이유: 구버전 로컬앱은 미지의 필드를
   무시하고 legacy(아침 시가) 라우팅하므로, 노출을 먼저 열면 `exit.fill=close` 신규 전략이 구앱에서
   **조용히 시가 청산**되는 divergence 창이 생긴다. 노출을 뒤로 미루는 것이 가장 단순한 차단(게이트
   기계장치 추가보다 우선 — 원칙 2/3).
- 기존 연동 전략(#27·#29·모의 17/18): DB 정의 무변경 → 모든 계층에서 None 분기 = 현행 경로.
  **#29 백테스트 표시 갭은 legacy 유지**(사용자 확인 2026-07-11: "당장 백테스트 화면은 안 바뀌어도
  된다") — 회원 동의 시 해당 전략에 `exit.fill="next_open"` 명시가 opt-in 교정 경로.

### D6. 검증 (게이트)

1. **Pin-first**: 구현 전, 현행 legacy 동작(fill=close+hold1 → 백테 종가청산·라이브 아침창 라우팅)을
   그대로 고정하는 테스트를 먼저 추가(red 아님·현행 green 고정) — 이후 전 단계에서 이 pin이 깨지면 즉시
   회귀로 검출.
2. 골든 백테스트 suite byte-identical(legacy 무변경의 기계 증명) + core/local/server 전체 green.
3. 신규: 엔진 청산가 매트릭스(위 표 5조합) · 라이브 선택자(아침 제외/종가 포함·넷팅 경유) · I5+ 감시 ·
   패리티 테스트(진입 close+exit next_open 백테 == 현행 라이브 의미론 — #29형 조합의 갭 마감 증명).

## 2. 범위 밖

- 조건부 청산(익절/손절/매도신호)의 체결창 선택 — 위험 확대 방향이라 제외(D3 명시).
- 기존 전략 정의 자동 마이그레이션·백테스트 재해석 — 금지(제1원칙).
- `typical` 청산·부분청산 비율 등 추가 옵션 — 수요 발생 시 별도(원칙 2).
