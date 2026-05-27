## 주요 변경 — Phase 7: 자동 Catch-up

### 🆕 PC 꺼져 있어 놓친 cycle/settlement/손절 자동 보완

기존: PC가 꺼져 있던 동안 트리거된 cron(KRX 08:55 cycle, 15:35 settlement,
US 야간 cycle 등)은 영구 skip — `MemoryJobStore`라 missed 이력 자체가 사라짐.
→ 다음 cron까지 잔고 stale, 손절 신호 누락, ledger ↔ KIS drift 누적.

변경: **로컬앱 기동 시 catch-up이 자동으로 missed 작업 보완**.

#### 자동 보완되는 케이스

| 상황 | Catch-up 동작 |
|---|---|
| 어제·오늘 KRX settlement (15:35) missed | reconcile + 잔고 push 즉시 실행 (3회 retry) |
| 어젯밤 US settlement (close+5분) missed | 동일 |
| 평일 09:00~15:30 사이 PC 켰는데 오늘 KRX cycle 없음 | run_cycle(market="KRX") 즉시 실행 |
| US 장중 PC 켰는데 오늘 cycle 없음 | run_cycle(market="US") 즉시 실행 |
| 장중 PC 켰을 때 보유 종목 손절선 즉시 체크 | 보유 종목별 현재가 조회 → 손절 trigger면 즉시 매도 |

#### 자금 안전성 — 매수 catch-up

- **지정가 매수**: `ref_price(어제 종가) × (1 + buy_tolerance_pct%)` 그대로 발주.
  시간과 무관한 fixed 가격 → 백테스트 alignment 완벽, selection bias 없음.
- **시장가 매수**: 시초가 자동 조회 → `시가 × (1 + bt_slippage_bps)` limit
  자동 변환. 백테스트의 "시가 + slippage" 모델과 정확히 alignment. 시가 못
  받으면 보수적 skip.

손절도 catch-up — 보유 종목 현재가 일괄 조회 → 손절선 비교 → trigger 지난
종목 즉시 매도 (L-04 over-sell 방지 + intent journal 그대로 적용).

#### Idempotency

`cycles.jsonl` 기반 — 이미 실행된 cycle/settlement은 다시 안 함. cycle entry에
`market`·`kind` 명시 (Phase 4 보강). 기존 entry는 ts 시각으로 추정 fallback.

### 🆕 Hero 영역 amber 배너 — catch-up 결과 표시

기동 시 catch-up이 동작하면 자동 업데이트 배너와 같은 amber 스타일로 결과
표시. 예:
```
⏰ 자동 catch-up 실행됨:
  · krx_settle: reconcile drift 2건 정정
  · us_stop_loss: 보유 3건 → 🔴 1건 손절 발주
```
사용자가 [확인] 클릭하면 배너 숨김 + 결과 파일 삭제.

### 내부 변경

**[localapp/catchup.py](localapp/catchup.py)** (신규, 327줄):
- `CatchupPlan` dataclass + `_decide_catchup_plan` (cycles.jsonl 기반 idempotency)
- `_is_krx_intraday`·`_is_us_intraday` (시간·캘린더 판단)
- `_catchup_stop_loss` — `IntradayStopManager.on_tick` 재사용 (평가+발주+L-04
  over-sell 방지 + intent journal 모두 통합)
- `_catchup_cycle` — `runner.run_cycle(market, catchup=True)` 호출
- `_catchup_settlement` — 3회 retry (30초 간격) + `runner.run_post_close_settlement`
- `_save_result` — `~/.quant-platform/catchup_result.json` 저장 (gui polling 대상)

**[localapp/trader.py](localapp/trader.py)**:
- `_submit_buy`·`_try_buy_one_symbol`·`_enter_from_preview`·`cycle`에 `catchup`
  파라미터 전파
- 시장가 매수 시 catchup이면 시초가 limit으로 변환
- `cycle_summary`에 `market`·`kind` 명시 (catch-up idempotency 지원)

**[localapp/runner.py](localapp/runner.py)**:
- `run_cycle`에 `catchup=False` 파라미터
- `_run_post_close_settlement_locked`에서 `cycle_summary["market"]·["kind"]` set
  + `order_log.log_cycle` 명시 호출 (cycles.jsonl 기록)

**[localapp/kis_broker.py](localapp/kis_broker.py)**:
- `today_open(symbol)` 신규 + `_open_domestic`·`_open_overseas` 분기

**[localapp/broker.py](localapp/broker.py)**:
- `Broker` Protocol에 `today_open` 추가

**[localapp/scheduler.py](localapp/scheduler.py)**:
- `register_jobs()` 끝에 `catchup.run_catchup_on_startup` background thread spawn

**[localapp/gui.py](localapp/gui.py)**:
- `catchup_banner` (Frame·Label·Button) — amber 스타일, [확인] dismiss
- `_check_catchup_result_polling` — 5초 간격 ×12회 polling
- `_show_catchup_banner`·`_format_catchup_summary`·`_dismiss_catchup_banner`

### Windows·macOS 동일 동작

플랫폼 의존 코드 없음. 양쪽 빌드 모두 동일 catch-up flow.

### v0.9.0-beta 사용자 자동 업데이트

기존 focus event update check가 v0.9.1-beta publish 직후 감지 → amber 배너 →
[지금 업데이트] 클릭으로 자동 적용. 첫 catch-up 실행은 업데이트 후 첫 기동 시.
