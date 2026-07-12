# 장중 신호 전략 자동매매 지원 — 통합 설계서 (Phase 1 → 최종)

> 2026-07-12 **승인** — Phase 1 구현 브랜치 `feat/intraday-template`. 리뷰용 v1(Phase 1 단독)을
> 대체·흡수한 통합 설계서: ① 챗봇·자동매매 모듈 코드 정밀 맵(2 병렬 탐색·전 인용 file:line) 반영
> ② 최종 형태(Phase 3)까지 전체 설계 ③ v1 통합 지점 오류 교정(§9). 기준 코드: origin/main 8faae17.
> **구현 편차(§2.6)**: 앱버전 게이트는 Device 컬럼 신설 대신 **최신 SyncSnapshot payload의
> app_version**을 읽는 것으로 구현(마이그레이션 0·push_snapshot 1줄 주입 — 더 단순, 동일 보장).

---

## 0. 목적·원칙·최종 형태

**목적**: "신호를 실시간 수준으로 캐치해 거래를 생성하는" 전략 부류를 백테스트(자유)와
자동매매(제약 적합)가 함께 지원한다. 1호 실수요 = 급등/상한가 마감형 오버나이트
(자체 연구 실증: 승률 74%·연 344~699건·건당 +4~6.7%).

**불변 원칙** (대화에서 확정):
1. 백테스트는 자유, 자동매매는 **템플릿 화이트리스트**로만 — 보장은 검증이 아니라 설계로.
2. 브로커 패리티: KIS·LS 동시 배선+각각 실측이 템플릿 출시 조건.
3. 챗봇(LLM)은 판정하지 않는다 — 구분·보장은 전부 결정적 코드.
4. 기존 연동 전략 byte-identical 무변경. 기존 챗봇 구조 유지(확장 지점 보강만).
5. 남는 근사(시간 해상도)는 숨기지 않고 자기서술로 표시.

**최종 형태(End State)** — 템플릿 3부류가 같은 인프라를 공유:

| 부류 | 템플릿 예 | 감시 방식 | 진입 시점 | 백테스트 정직성 |
|---|---|---|---|---|
| P1 마감 확정형 | `limit_up_close_v1` | 15:25 스캔 1회(랭킹) | 종가 단일가 | 일봉으로 정직 ✅ |
| P2 워치리스트형 | `watchlist_trigger_v1` | 장중 WS/폴링(지정 N종목) | 트리거 즉시 | 일봉 근사(명시) → P3에서 상향 |
| P3 랭킹 상시형 | `intraday_rank_surge_v1` | 장중 랭킹 폴링(1분) | 트리거 즉시 | 분봉 트랙으로 정직화 |

공유 인프라(§2): IR `template` 필드 + core 매처 + 승격 게이트 템플릿 분기 + 이식성 배지 +
로컬 스캔/트리거 seam + 브로커 scan 계약 + 앱 버전 게이트 + 챗봇 4표면 보강.

**비범위(항구)**: 틱 반응형(호가·초단타) — 옵션과 동일하게 스코프 밖 선언.
장중 임의 조건(템플릿 밖)의 자동매매 연동 — 백테스트·챗 연구는 가능, 연동 버튼 없음.

---

## 1. 모듈 정밀 근거 (설계가 딛고 선 코드 사실)

두 병렬 탐색으로 확정한, 설계를 좌우하는 사실들. (전체 맵은 탐색 보고에 있고 여기엔 결정에 쓰인 것만.)

**IR·챗봇 축**
- I1. `StrategyIR` 최상위는 `select`/`prescribe` 사이드카 패턴([spec.py:249-250]) — `template`도 같은 자리.
  최상위 모델은 `extra="ignore"`라 **필드 선언 없인 동결 원장 왕복에서 소실**된다.
- I2. 검증 규칙은 `Issue(rule, severity, message, path)`·S-*/M-* 체계([spec.py:474-840]),
  `is_research` 게이트([spec.py:486]) 준수 필수 — 위반 시 리서치 쿼리 거짓 거부→repair 후퇴.
- I3. 등락률 조건의 표준 IR: `compare(>=, data("__SELF__.pct_change_1d"), const(29.5))` —
  `pct_change_1d`는 **% 단위** 사전계산 지표([indicators.py:33], 코퍼스 실례 [analysis_corpus.py:150]).
  전용 신규 op 불필요. `_normalize_pct_thresholds`는 |const|<0.01만 교정하므로 무간섭.
- I4. 결과계약: shape/status는 core(`run.py:159-170`), 챗 payload엔 `result["ir"]`·`adjustable`이
  `tools.run_simulate`([tools.py:396-399])에서 부착, 방법론은 `attach_methodology`([tools.py:712])가
  `agent.py:399` 직후 부착 — **배지도 같은 자리**.
- I5. 웹 연동 버튼 게이트는 `ChatResultView.canLink`([ChatResultView.tsx:460])가 shape/status/ir로 판정,
  draft 생성은 `createStrategy(ir,"draft")` → 서버 `create_strategy`([strategies.py:262]).
- I6. **승격(운용 시작) 게이트 `_assert_live_tradable`([strategies.py:74-166])가 현재
  `universe.kind=all` 차단(:123)·`on_signal+screener` 차단(:162-166)** — 템플릿 전략의 자연 IR이
  기존 게이트에 막힌다 → 템플릿은 **별도 검증 세트로 분기**해야 함(§2.3). 선례: 승격 게이트=422, 삭제=409.
- I7. NL 컴파일 관용구는 `<idioms>` 16레시피([ir_compiler.py:296-400])+few-shot([:26-158])에 추가하는
  구조. repair 루프 max 2회, 원의도 앵커([ir_compiler.py:556-600]).

**자동매매 축**
- A1. KRX 종가창 cron: 주식 15:25 `run_close_cycle(market="KRX", instrument_class="stock")`
  ([scheduler.py:194-198]). 장중 loop 창 08:50~15:30([:169-181]).
- A2. **라이브 종가 진입의 실제 경로는 `run_close_netting`([trader.py:1994])** —
  `run_close_cycle`([runner.py:480-482])이 호출하며 `enter_close_candidates`(2207)는 레거시 미러.
  넷팅(PLAN→NET→APPLY)·킬스위치·손실한도·drawdown 게이트가 이 경로에 내장.
- A3. 합성 매수 후보의 최소 스키마 = `{"strategy_id": sid, "candidates":[{"symbol":…,"direction":"long"}]}`
  (후보에서 실소비 키는 symbol·direction뿐 [trader.py:1657,1675]) + `strategies` 목록에 해당 전략
  definition 필수([:1576-1578]) — definition이 fill 라우팅(1601)·사이징·커버리지를 전부 구동.
- A4. 상속되는 안전장치 전수(추가 코드 0): skip_wrong_account(1620)·skip_uncovered(1627)·
  skip_held(1641)·skip_no_data(1312)·skip_halted/managed(1340)·skip_daily_count/turnover(1372)·
  skip_funds(1505)·**skip_idempotent L-01(1516)**·killswitch/일일손실/drawdown(_close_entry_blocked 1948).
- A5. **degenerate 방어([trader.py:1609])가 hold_days==0 + 종가매수를 차단** — 종가 스캔 템플릿은
  반드시 `fill="close" + hold_days>=1`(우리 IR 패턴과 일치).
- A6. 커버리지 게이트 자리 = [trader.py:1625-1632] `skip_uncovered` — **미지 템플릿 차단을 같은 자리·
  같은 형식**으로(`skip_unknown_template`).
- A7. KIS 랭킹 GET은 기존 `_get_retry(path, TR, params, base=self.quote_base)` 재사용
  ([kis_broker.py:302-335], 선례 `_price_domestic`:500-505). **모의계좌도 시세는 실전 도메인+실전
  시세앱키**(quote_base, [:124-138]) → KIS 모의 유저도 랭킹 TR 호출이 가능할 전망(실측 필요, §8).
  LS는 `_post(tr_cd, body)`([ls_broker.py:129-165]) 위에 얹는다. 전역 스로틀 8건/초 공유([:96]).
- A8. `intraday_loop`은 **매도 전용**(IntradayStopManager, submit_sell_fn [intraday_loop.py:443-448]),
  구독=보유 종목만(481·594), 시장당 1개, 디스크 SSOT(M9 reload_state 규약 162·523·549),
  WS 폴백 폴링 라운드 이미 존재(284-339) — P2 진입 트리거의 확장 지점과 제약.
- A9. 앱 버전은 서버 어디에도 없다(Device·SyncSnapshot·heartbeat 전부 [models.py:76-124]) —
  버전 게이트를 위해 신설 필요(§2.6).
- A10. preview의 전략별 스킵 사유는 `skipped[].reason`으로 이미 웹 렌더 — "장중 스캔 대기"는
  `_evaluate_ir_strategy` 조기 분기([preview_engine.py:189-392])로 자동 표면화.

---

## 2. 공통 인프라 설계 (모든 phase가 공유 — Phase 1에서 구축)

### 2.1 IR: `template` 사이드카 필드 (D2)

```python
class TemplateConfig(BaseModel):
    id: Literal["limit_up_close_v1"]          # phase마다 Literal 확장
    max_daily_entries: int = Field(3, ge=1, le=5)
```
`StrategyIR.template: Optional[TemplateConfig] = None` — [spec.py:249] `select`/`prescribe` 옆(I1).
- **엔진은 태그를 읽지 않는다** — 백테스트 의미는 기존 프리미티브가 완전 결정(태그 유/무 결과
  byte-identical 테스트로 잠금). 라이브·게이트·배지만 태그를 소비.
- 전략 파라미터의 단일 출처 = IR(임계=signal const, 시장=universe screener, 사이징=sizing).
  템플릿 선언(§2.2)엔 브로커 사실만 — 이중 기재 드리프트 차단(D3).

### 2.2 core 템플릿 모듈 (`ir_engine/templates.py` 신규) — 매처·선언 단일 출처

```
TEMPLATES: dict[id, TemplateDef]
  TemplateDef = 패턴검사(pure fn) · 파라미터 추출 · 실행창 · min_app_version
              · broker_support: {kis: {...}, ls: {...}}   # 지원여부·모의여부·스캔 TR 명세(문서용)
template_issues(s: StrategyIR) -> list[Issue]      # S-template-* — validate_strategy에서 호출
scan_params(s: StrategyIR) -> ScanParams           # {threshold_pct, markets, max_entries}
```
- `limit_up_close_v1` 패턴 요건(전부 기존 프리미티브): `query=simulate`·`study.axis=none`·
  `entry.mode=on_signal`·`direction=long`·`simulation.fill=close`·`exit.hold_days=1`·
  `exit.fill="next_open"`·`universe.kind=all`(screener는 Market attribute만 허용)·
  signal = **정규형** `compare(>=, data(__SELF__.pct_change_1d), const(X))`, X∈[20.0, 29.9].
  정규형 강제가 파라미터 추출을 패턴매칭이 아닌 판정으로 만든다(I3).
- `validate_strategy` 연결: `if s.template is not None:`에서 ①등록된 id인지 ②`is_research`면
  S-template 오류(템플릿=simulate 전용, I2) ③패턴 요건 검사. LLM repair 루프가 S-template
  메시지로 자동 교정(I7과 동일 채널).
- **서버(게이트·배지)와 로컬(스캔 라우팅)이 같은 함수를 공유** — core=SSOT(live.py seam 선례).

### 2.3 승격 게이트: `_assert_live_tradable` 템플릿 분기 (I6 해소)

[strategies.py:74] 진입부에서:
```
if ir.template: return _assert_template_tradable(ir, account_broker, run_mode, user)
```
별도 검증 세트: ① core 매처 통과(=validate에서 이미 보장, 이중확인) ② `autotrade_capability`
(kr_equity × 브로커) ③ TEMPLATES.broker_support[브로커] 존재 ④ 모의/실전 지원 플래그
⑤ **앱 버전**: 유저 기기의 `Device.app_version >= min_app_version`(§2.6) — 미달이면 422
"로컬앱 vX.Y 이상 필요(현재 vA.B)". 일반 경로의 kind=all 차단(:123)·screener 차단(:162)은
템플릿 분기로 우회 — "템플릿=사전 검증된 별도 라이브 경로"의 구조적 표현. 오류 코드는 승격
게이트 선례(422) 유지.

### 2.4 이식성 배지: `attach_portability` (I4·I5)

서버 `tools.py`에 `attach_methodology` 옆 신규, `agent.py:399` 직후 호출.
```
result["autotrade"] = {eligible, template_id, reasons[], min_app_version}
```
- **게이트와 배지의 미러 드리프트 원천 차단**: 판정 로직을 `portability_check(ir, broker|None)`
  단일 함수로 두고 `_assert_live_tradable` 템플릿 분기(§2.3)와 배지가 **공유**한다.
  브로커 미정(계좌 미연동) 시 KIS·LS 교집합으로 보수 판정.
- 웹 `ChatResultView.canLink`([:460])를 배지 필드 소비로 교체 — TS 게이트 중복 제거.
  simulate/extremize + `result.ir` 필수 조건은 유지(I4: ir 없으면 배지도 없음).

### 2.5 로컬: 스캔 seam + 진입 배선 + 이중 안전망

- **Broker 계약**: `scan_close_surge(min_change_pct, markets) -> list[ScanRow]`,
  `ScanRow = {symbol, name, expected_price, change_pct, is_limit_up, total_ask_rem}`.
  KIS 구현 = `_get_retry` 위(A7), LS 구현 = `_post` 위 — **같은 PR에서 패리티 배선**.
  미구현 브로커는 명시 `NotImplementedError`(조용한 빈 리스트 금지).
- **주입 지점**: `run_close_cycle`의 `pull_preview` 직후([runner.py:464]) —
  ① 배정 전략 중 template 전략 선별 ② 파라미터 셋별 스캔 dedupe(동일 스캔 1회)
  ③ `scan_params`로 필터(임계·시장·**is_limit_up 필수**·max_daily_entries: 등락률 내림차순 상한)
  ④ 로컬 dataset에 있는 심볼만(ETF/우선주/관리종목 자연 배제 + skip_no_data 정합)
  ⑤ A3 스키마로 합성 후보 append → 이후 **무변경**: `run_close_netting`이 A4 안전장치 전수 상속.
- **스캔 실패 = 진입 skip + decision 경보**(fail-soft) — 종가창의 기존 preview pull 실패 계약과
  동형([runner.py:459-466]). 청산·안전장치는 스캔과 무관하게 동작.
- **이중 안전망**: [trader.py:1625] 커버리지 게이트 자리에 `skip_unknown_template` —
  로컬이 모르는 template id의 전략은 어떤 경로로 후보가 와도 차단+표면화(A6).

### 2.6 앱 버전 게이트 (A9 해소 — 구앱 조용한 divergence 차단)

- 로컬: `push_snapshot`·`push_heartbeat` payload에 `app_version=__version__` 주입
  ([sync_client.py:52,210] — 1줄씩).
- 서버: `Device.app_version` 컬럼 신설(널 허용), sync/heartbeat 수신 시 갱신.
  게이트(§2.3)는 최근 활동 기기의 버전으로 판정 — **버전 미보고(구앱) = 미달로 간주**(안전 기본값).
- 롤아웃 순서(§7)와 결합: 챗봇·웹 노출 전에 로컬 릴리스가 선행되므로 정상 유저는 게이트를
  느끼지 못하고, 업데이트 안 한 유저만 명시 메시지를 본다.

### 2.7 챗봇 4표면 보강 (기존 구조 유지 — I7)

| 지점 | 내용 |
|---|---|
| NL 컴파일 | `<idioms>`에 템플릿 레시피(정규형 IR 예시+파라미터 범위) + few-shot 1개. `capability_spec`에 템플릿 항목 |
| 검증기 | §2.2의 S-template — repair 루프가 기존 채널로 교정, 템플릿 밖+자동매매 의도=정직 고지 |
| 결과계약 | §2.4 배지 |
| 오케스트레이터 | `prompt.py` `<analysis_menu>`·연동 버튼 안내(:142)에 템플릿 흐름 문구 |

preview: `_evaluate_ir_strategy` 조기 분기로 템플릿 전략은 EOD 후보 생성 없이
`skipped=[{"reason":"장중 스캔 대기(종가창 로컬 실행)"}]` — 웹 자동 렌더(A10).

---

## 3. Phase 1 — `limit_up_close_v1` (급등/상한가 마감형 오버나이트)

**전략 의미**: 15:25(장중·마감 동시호가 진행 중) 당일 등락률 임계 이상 + **상한가 잠김
(예상체결가=상한가)** 종목을 종가 단일가로 매수 → 익일 시가 매도(#358 exit.fill 경로).
백테스트↔라이브의 알려진 비대칭 1건: 백테스트 잠김 필터=등락률 임계 근사(일봉),
라이브=예상체결가·상한가 실측(더 정확) — 자기서술 명시.

> **D8 정량 실측(2026-07-12·dev-data 전 KR 3,577종목·2020~2026.5)**: 일봉 근사 백테스트
> (임계 29.5%)는 연 465건(연구 344~699 ✅)·건당 +4.15%(연구 4~6.7 ✅)·**승률 53.0%**
> (연구 74% ❌). 잠김 일봉 근사(Close≥High)·거래대금 하한(10억/50억)은 승률에 무영향 —
> 연구 74%는 dt_scan 9회 반복의 최종 필터 조합(별도 아티팩트) 산물로, 일봉 임계 근사로는
> 미재현. 함의: 유저에게 표시되는 백테스트 승률은 ~53%대(보수·정직)이고 라이브는 잠김
> 실측 필터라 연구상 그보다 유리할 것으로 기대 — **근사 방향이 과대광고가 아닌 과소표시**
> 라 자금 안전 문제 없음. 최종 판정은 실측 게이트 ⓒ의 라이브 체결률·승률 계측.

**스캔 구현** (브로커별·docs/kis-api KB + LS 가이드 확정):

| | KIS | LS |
|---|---|---|
| 랭킹 | `FHPST01820000` 예상체결 상승상위 — **`fid_mkop_cls_code=1`(장마감예상)**·상승률순·시장코드(0000/0001/1001)·최대 30건·모의 TR 미지원이나 **호출은 quote_base(실전 시세앱키)라 모의계좌도 가능 전망**(A7·실측 §8) | `t1488` 예상체결가등락율상위(2콜/초·연속조회 idx) |
| 잠김 판정 | 후보별 `FHKST01010100` 현재가 시세 → `stck_mxpr`(상한가) vs 예상체결가. ≤30콜 @8/초 ≈ 4초 | `t8407` 멀티현재가(5콜/초·50종목/콜) → `uplmtprice` vs price. 1콜 |
| 일일 비용 | 랭킹 1~3 + 후보 ≤30 ≈ 4~7초 | 2~4콜 ≈ 2~4초 |

**작업 목록** (=기존 태스크 #2~#6, §2 공통 인프라 포함):
core(§2.1·2.2, 매처 TDD) → local(§2.5 스캔 seam KIS·LS + 종가창 배선 + skip_unknown_template
+ 시나리오 테스트 + §2.6 로컬측) → server(§2.3 게이트 + §2.6 서버측 + preview 분기)
→ 검증(§6) → 로컬 릴리스 → 실측 → 챗봇·웹 노출(§2.4·2.7).

---

## 4. Phase 2 — `watchlist_trigger_v1` (워치리스트 장중 트리거형)

**전략 의미**: 유저 지정 종목(`universe.kind=list`, **N≤20**)을 장중 감시, 가격 트리거
(전일 종가 대비 +X% 돌파 등 정규형 조건) 발동 즉시 진입. 청산은 기존 Exit 전부 사용 가능.

**라이브** (A8의 확장 지점):
- `intraday_loop`에 `EntryTriggerManager` 신설 — 매도 전용 `IntradayStopManager`와 대칭.
  구독 = 보유 ∪ 워치리스트, **WS 예산: 보유 + ΣN ≤ 41(KIS)** — 템플릿 파라미터 상한(N≤20)
  × 동시 연동 상한(K=1~2, TEMPLATES 상수)으로 **정적 보장** + loop 시작 시 로컬 재검증.
  초과·WS 단절 시 기존 폴링 라운드(284-339) 폴백.
- 트리거 발동 → 합성 후보 1건 → **cycle 스코프 진입 실행**: `_on_ks_trigger`(542-579)가 loop
  안에서 Trader 액션을 하는 기존 선례를 따라 `_CYCLE_LOCK` + M9 reload_state 규약 준수.
- **트리거 멱등**: 전략×종목×일 1회(디스크 기록) + 기존 L-01 skip_idempotent 이중.
- 예산 degrade 우선순위 명문화: **청산 감시 > 주문 > 진입 트리거**(진입이 먼저 잘린다).

**백테스트** — 이 phase의 유일한 엔진 변경:
- `SimSpec.fill`에 `"trigger"` 추가(1값): 발동일 판정 = High ≥ 임계가(롱), 체결가 =
  max(Open, 임계가)+슬리피지 — **보수 근사**. 자기서술·배지에 "장중 근사(일봉 경로 불명)" 명시.
- 대안(백테스트 미지원 유지)은 4계층 계약 위반이라 기각 — 근사 명시가 정직한 지원.
- P3 분봉 트랙 완료 시 같은 IR을 분봉 러너로 재실행해 근사 라벨 해제(§5).

**챗봇**: 관용구 레시피 1개 추가 외 무변경(§2.7 인프라 재사용).

---

## 5. Phase 3 — `intraday_rank_surge_v1` + 분봉 데이터 트랙

**랭킹 상시형**: 장중 1분 주기 랭킹 폴링(KIS `FHPST01700000` 등락률순위·LS `t1441`, 각 1콜/분 —
8/초 예산의 ~0.2%)으로 시장 전체에서 "장중 +X% 돌파" 종목 즉시 진입. WS 불필요(랭킹=폴링)라
종목 수 한도 원천 비발생. 라이브 배선은 P2의 EntryTriggerManager에 "폴링 소스" 추가 —
신규 인프라 없음.

**분봉 데이터 트랙** (별도 승인 게이트·데이터 캠페인):
- 목적: P2·P3 템플릿의 백테스트를 근사→실측으로 상향(fill="trigger"의 High 근사를 분봉
  경로 재현으로 교체), 향후 장중 전략 연구 일반의 기반.
- 수집: 매일 당일 분봉 적재(KIS 분봉 API — 과거 백필 제한적이라 **적재 시작일부터 이력 축적**,
  과거 백필은 벤더 비용 발생 시 별도 결정). 저장: parquet 별도 트리(용량 산정 후 확정).
- 엔진: 분봉 백테스트 러너 — 중기 캠페인, 이 설계서 범위에선 트랙 존재와 승인 게이트만 정의.

**최종 상태 도달**: 템플릿 3부류 + 공유 인프라. 이후 새 템플릿 추가 = TEMPLATES 등록 +
브로커 스캔/트리거 소스 + 관용구 1개 + 실측 — 패턴화된 반복 작업.

---

## 6. 검증 계획 (phase별)

| Phase | 결정적 | 실측 게이트(릴리스 전) |
|---|---|---|
| P1 | 매처 red→green·태그 유/무 백테스트 동일·스캔 파서 픽스처(KIS/LS)·종가창 시나리오(모의 스캔 주입→진입계획·max_entries·미지템플릿 차단)·골든 byte-identical·전 스위트·dt_scan 연구 교차(승률·건수 범위) | KIS·LS 각각: ⓐ 모의/실전 랭킹 TR 가용성 1회(특히 KIS 모의계좌+실전 시세앱키 조합) ⓑ 15:25 드라이런(발주 없이 스캔·후보 로그) ⓒ 소액 라이브 1건. 체결률 계측 시작(잠김 후보 대비 실제 체결 — 백테스트 74%는 체결 가정) |
| P2 | fill="trigger" 엔진 테스트(근사 규칙 pin)·EntryTriggerManager 시나리오·WS 예산 정적 검증·트리거 멱등 | 페이퍼 모드 1주 관찰(트리거 발화·중복 진입 0) 후 소액 라이브 |
| P3 | 폴링 소스 시나리오·분봉 러너는 트랙 별도 | 랭킹 폴링 반나절 드라이런 |

---

## 7. 롤아웃 (공통 규칙 — exit.fill D5 계승)

각 phase: **구현·머지(유저 접점 0) → 로컬앱 릴리스 → 실측 게이트 → 챗봇·웹 노출**.
앱 버전 게이트(§2.6)가 구앱을 명시 차단하므로 노출 후에도 divergence 없음.
P1 릴리스 목표 = v0.9.72.

## 8. 열린 결정 (승인 시 확정 필요)

1. **KIS 모의계좌의 스캔 가용성** — 시세가 실전 도메인(quote_base)이라 가능 전망(A7)이나
   실측 전 미확정. 실측 ⓐ에서 판정: 가능=모의 유저도 P1 사용, 불가=모의 차단+메시지(권장 기본).
2. **P2 `fill="trigger"` 엔진 확장 승인** — 엔진 코어의 유일한 변경(1 fill 모드). §4 근사 규칙으로.
3. **분봉 트랙 착수 시점** — P2 근사 라벨 운용 경험 후 결정 권장.
4. 템플릿 버저닝 `*_v1` 컨벤션(파라미터 의미 변경 시 v2 신설·구버전 전략은 그대로 동작).

## 9. v1 설계서에서 교정된 것 (코드 맵 반영)

- 진입 경로: `enter_close_candidates`가 아니라 **`run_close_netting`**([trader.py:1994])이
  현행 라이브 경로 — 합성 후보는 runner([runner.py:464])에서 주입해 넷팅·게이트 전수 상속.
- **승격 게이트 충돌 발견**: `_assert_live_tradable`의 kind=all·screener 차단(I6) —
  템플릿 분기(§2.3) 없이는 P1 전략이 운용 시작 자체가 안 된다. v1엔 없던 필수 작업.
- 배지·게이트 미러를 "같은 로직 복제"가 아니라 **단일 함수 공유**(§2.4)로 강화.
- 잠김 필터를 열린 결정에서 **패턴 확정**으로: 예상체결가=상한가 판정(KIS stck_mxpr·LS uplmtprice),
  등락률 임계와 독립.
- KIS 모의 스캔이 "미지원 확정"이 아니라 **가능 전망(quote_base)**으로 — 실측 항목화.
- 앱 버전 인프라가 현재 전무함을 확인(A9) — §2.6을 공통 인프라로 승격.
