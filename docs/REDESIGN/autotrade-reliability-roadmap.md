# 자동매매 신뢰성 통합 수정 로드맵 (확정본)

> 2026-06-12~13 라이브 검증으로 진단 모델 전체가 입증된 뒤 확정. 실행 시 이 문서를 단일 로드맵으로 참조한다.
> 누적 컨텍스트: 메모리 `project-autotrading-failure-deepdive`, 인프라 상세 `_wt-infra/docs/handoff/2026-06-12-server-infra-handoff.md`, KIS 실측 `docs/kis-api/GOTCHAS.md`.

---

## 1. 수정 의도 및 목표

**의도.** 자동매매가 "거래는 되는데 기록·표시·자율성이 깨지는" 부류 결함을 **구조 단위로** 닫는다. 며칠간 무발주·무기록·거짓표시가 단건 핫픽스로 반복됐으므로, 증상이 아니라 **부류(class)** 를 닫는다.

**목표.**
1. **국내주식·국내선물·미국주식 자동매매 하자보수** — 4경로(KR/US × 주식/선물)가 사람 개입 없이 한 바퀴(발주→체결→**기록**→청산→**기록**)를 정확히 돈다.
2. **거짓 없는 상태 표시** — equity·preview·ledger·타임라인이 broker 현실과 일치한다(거짓 −98%·거짓 "누락"·거짓 녹색 "0건" 제거).
3. **위험제어 안전** — 부분 데이터로 킬스위치가 오작동하지 않는다(06-09 US 북 거짓 청산 재발 방지).
4. **웹앱 서버 인프라 위생** — 무한적재·인덱스 공백·폴링 egress·스케줄러 가드 부재를 닫아 사용성·비용·안정성을 개선(Neon egress 인시던트 재발 방지).

**비목표(이번 범위 아님).** 신규 거래 기능, 옵션 매매, 해외선물 라이브(모의 미지원), NL 컴파일러 로직 변경.

---

## 2. 범위 — 4계층 × 시장/상품 매트릭스

| | 국내주식 | 국내선물 | 미국주식 | 해외선물 |
|---|---|---|---|---|
| 발주 | ✅ 시장가 | ✅ 시장가(검증완료) | 예약 지정가 | 모의 미지원(범위 밖) |
| 체결감지 | ✅ 당일·odno | ✅ (선물 진입 검증) | ❌ **δ**(RC1·RC2) | 범위 밖 |
| 종가청산 | θ(wait 갭) | ❌ **θ**(라이브 실패·수동복구) | ❌ **N1**(ledger 블라인드) | 범위 밖 |
| 사이징 | ✅ | ✅ | ❌ **FX**(₩→$ 1370배) | 범위 밖 |

핵심: **KR은 δ 면역**(당일·odno 일치)이라 θ만 문제. **US는 δ+FX+N1 복합**이고 자가복구도 불가.

---

## 3. 발견된 결함 대장 (근본원인·증거·수정)

> 증거 위치: `~/.quant-platform/{ledger,pending_orders,cycles,orders,trades}.json(l)`, `logs/localapp.log*`. ts는 KST(orders는 일부 UTC 주의).

### A. 체결-기록 부류 (핵심 질병)
| ID | 결함 | 근본원인 (file:line) | 라이브 증거 | 수정 |
|---|---|---|---|---|
| **δ** | US 체결 미기록 | `kis_broker.py:835` `_overseas_ccnl_today` KST 날짜(체결은 US현지) → 0행 (**RC1**); `:855` 예약 접수 odno(448·273) ≠ 체결 odno 매칭실패 (**RC2**) | GOOG 448·0000040620·273 전부 pending stuck·ledger 없음 | **#118** |
| **병1** | reconcile가 내 체결을 "외부"로 무시 | `trader.py:252` `reconcile_with_kis` external_extras 미claim("자동매매가 산 게 아니므로") | 06-13 05:05 정산 `external_extras=1, applied=0`(GOOG 261 방치) | **#118** intent-앵커 |
| **θ** | 종가청산 체결 timing 갭 | `trader.py:1245` `liquidate_day_trades`에 `_wait_pending` 부재(1회 resolve) + 스케줄러 `15:35 정산 → 15:40 선물청산` 역순 | 선물 0000004525 15:40 발주→~15:45 체결인데 stuck, 18:54 수동 "지금 실행"으로 복구(+24.9M) | **θ(신규 PR)** |
| **M9** | 인스턴스 stale 덮어쓰기 | 장수명 loop Trader vs ephemeral cycle Trader가 같은 파일 통째 저장 | 체결 이벤트 3중 중복·매도 포지션 부활 | **#118** disk-SSOT |
| **η** | 명령 버스 at-most-once | SSE 명령 유실 창 | (잠재) | 후속(deferred) |

### B. 사이징·패리티 부류
| **FX** | ₩정액을 $로 취급(1370배) | `ir_engine/live.py` `event_buy_qty` fixed_amount 미환산 + `engine.py` `_budget` 수동미러 | "1만원" GOOG → **261주(≈$96k)**(매수여력 클램프); 06-12 "100만원" → 263주 | **#123** |
| **병3** | backtest=live 미강제 | 공유 아닌 수동미러 → 버그 양쪽 동일破 | red에서 양쪽 2832주 동일(패리티-in-bug) | **#123** 패리티 오라클 |

### C. 위험·equity 부류
| **ε** | 부분잔고 거짓 −98% | `kis_broker.py:235` account_snapshot이 해외/선물 fetch 실패를 0으로 삼킴 → 거짓 폭락 | 06-09 US 북 거짓 청산; cycles `equity_post 10M vs pre 510M`(라이브) | **#118** fetch_failed 표식+평가보류 |
| **D3-3** | equity 시계열 혼합 | 시계열=국내만(total_eval)·분모=통합 | 웹 자산곡선 −98% | **#118** 통합자산화 |

### D. 스키마·preview·인프라·관측 부류
| **K** | 레거시 IR 드리프트 | `spec.py` validator가 frozen sweep/period_split 거부 | 0000J0/Z0/0126Z0 "파싱 실패" 반복 | **#120** 파싱경계 마이그레이션 |
| **P** | preview 슬롯 거짓 누락 | `routers/trading.py` `_preview_events` `latest_gen ≥ sched-2min` | 07:30·18:15 둘 다 거짓 누락→자가회복 | **#119** "갱신중" 판정 |
| **#1/#2/#3** | 무한적재·인덱스공백·cron가드부재 | `db.py`·`main.py`·`models.py` | heartbeat 288행/기기/일·models.py:105 거짓주석 | **#121** |
| **#5** | Monitor 폴링 egress | /auth/devices·/sync/commands·/market/context ETag 부재+비가시 폴링 | 비가시 탭 ~1,140 req/hr | **#122** |
| **N1** | 당일매매 청산 방치(cascade) | 미기록 매수 → ledger 블라인드 → day-trade close가 ledger만 봄 | GOOG 261주 주말 방치(05:05 external_extras=1) | 근본=#118·**방어선 신규** |
| **N2** | 거짓 녹색 "0건"(관측) | 타임라인이 발주-but-미기록을 ✓ 성공으로 표시·선물청산 마일스톤 부재 | "22:10 미장 ✓ 0건" while 261주 방치 | **관측 신규** |

### E. US 심각도 (수정 우선순위 근거)
US 미기록은 **자가복구 불가** — `order_status(예약 odno)`가 δ(RC1 날짜+RC2 odno)로 막혀 "지금 실행"으로도 복구 안 됨(KR 선물은 복구됨). → **#118이 US 자율성의 단일 차단해소**.

---

## 4. 통합 계획 — 워크스트림 → PR 매핑

| 워크스트림 | 닫는 결함 | 상태 | PR |
|---|---|---|---|
| **WS-1 포지션 SSOT** | δ·병1·ε·D3-3·M9·pending GC | ✅ 구현완료 | **#118** (local) |
| **WS-2 패리티+FX** | FX·병3 | ✅ 구현완료 | **#123** (core+local) |
| **WS-3P preview 슬롯** | P | ✅ 구현완료 | **#119** (server+web) |
| **K 마이그레이션** | K | ✅ 구현완료 | **#120** (core) |
| **인프라 위생** | #1·#2·#3 | ✅ 구현완료 | **#121** (server) |
| **폴링 egress** | #5 | ✅ 구현완료 | **#122** (server+web) |
| **θ 종가청산 신뢰성** | θ·N1방어선·N2관측 | ✅ 구현완료 (2026-06-13) | **fix/close-cycle-reliability** (local+server+web, #118 위 스택) |
| (deferred) | #4 OOM·#6 Neon직결·/preview ETag·병6·η·WS-3후속 | 보류 | 다음 웨이브 |

**θ PR 구현 결과 (fix/close-cycle-reliability — 사양 대비 확정 선택):**
1. ✅ `liquidate_day_trades`에 `_wait_pending`(일반 cycle과 동일 60s/20s, 모의 ~27초 체결 흡수).
2. ✅ cron **재배치** 선택(신규 cron 추가 대신): `krx_settlement` 15:35→**15:50** — "정산은 모든
   KRX 종가창(선물 15:45) 이후" 불변식. catch-up 임계(catchup.py)도 15:50 정합.
3. ✅ **N1 방어선** = 종가청산 시작 시 `_resolve_pending` 선실행(미기록 진입 체결을 ledger 복원
   후 순회 — settlement과 동일 순서). **안전성 검토 결론: broker 초과보유 자동 매도는 금지** —
   체결 진실 없이 내는 주문은 사용자 외부 보유를 오인 매도할 수 있다(병1 불변식). 대신
   "당일청산 불능" decision(error)으로 명시 표면화.
4. ✅ **N2 관측**: 종가청산 마일스톤 3종(krx_close_stock 15:25·krx_close_futures 15:40·us_close
   close−5분) + kind-aware `_match_snapshot`(state_sync/정산/종가청산 push의 슬롯 교차 가장
   차단) + `n_pending_unresolved>0`→⚠ warning(장 마감 경계 이벤트 한정). day_trade_close
   push가 market/instrument_class를 실어 슬롯 매칭(기존 "ALL" 하드코딩 폐기, 구버전 push 호환).
5. ✅ 재현 테스트 15건 red→green: `test_theta_close_reliability.py`(4)·`test_close_ordering.py`(2)·
   `test_timeline_close_events.py`(9). local 370·server 196·루트 골든 390·web build green.
   인시던트: `docs/incidents/2026-06-12-futures-close-fill-unrecorded.md`.

---

## 5. 실행 로드맵 (Phase)

```
Phase 1  θ + N1방어선 + N2관측 구현 (local+server, TDD, 신규 브랜치)
           └ 결함재현 테스트 → _wait_pending+정산배치+마일스톤 → 골든보존 → draft PR + docs/incidents/

Phase 2  머지 웨이브 (무거래창 + 건별 승인)
           A. 로컬/코어 1릴리즈:  #118 → #120 → #123 → θ(local부분)
                                    → 로컬앱 zip 릴리즈 → 사용자 업데이트
           B. 서버 (Railway 재시작): #119 → #121 → θ(server부분) → #122
                                    무거래창=KRX마감(15:45)~US개장(22:30) 또는 주말

Phase 3  라이브 검증 게이트
           · 다음 미장(22:30):  #118 δ 체결기록 · #123 FX(1만원→0주 or 적정수량)
           · 다음 선물 종가(15:40): θ 자율 기록(수동 "지금 실행" 불필요)
           · 다음 07:30·18:15:    #119 "갱신중" 표시
           · 서버 배포 직후:       #121 [migrate] 무에러 · #122 304

Phase 4  잔재 정리
           · 방치 GOOG 261주 + 고아 pending(438·48796~99·448·40620·273)
             → #118 릴리즈 후 자동(GC+intent reconcile) 또는 KIS HTS 수동

Phase 5  Deferred 웨이브
           인프라 #4 OOM·#6 Neon직결 · /preview ETag(generated_at projection)
           · 병6 path-filtered deploy · η command bus · WS-3 후속(FRED last-good·재시작무관 preview)
```

**권장 머지 순서 근거**: 자금안전(δ·ε) 최우선 → K(파싱복원) → FX → θ → 서버(재시작 영향순). #118·#123이 `trader.py` 함께 수정하나 hunk 비중첩(검증됨)이라 어느 순서든 클린.

---

## 6. 머지·릴리즈 절차 & 검증 매트릭스

**가드레일(전 항목 불변).**
- 결함 재현 테스트 **선작성** · 골든 **byte-identical** 보존
- **서버변경 = 무거래창 머지**(Railway 재시작·#121 인덱스/pruning 쓰기블록)
- **머지·배포·push 건별 명시 승인** (분류기가 일괄/모호 승인 차단)
- KIS 자격증명·계좌·원시주문 **로컬 PC 전용**(서버 미유입)
- 로컬앱 변경은 **릴리즈 zip + 사용자 업데이트 후** 실효 → 라이브 게이트 필요

**테스트 신호(현 상태).** #118 local 364 green · #119 server 197 · #120 core245+루트골든390+local346 · #121 server198 · #122 server194 · #123 루트390+core245+local350 · θ(fix/close-cycle-reliability, #118 스택) local370+server196+루트골든390+web build.

---

## 7. 참고자료 (작업 시 직접 참조)

**코드 위치(원본=origin/main, 미머지 수정=각 worktree).**
- 자동매매 본체: `local/localapp/{trader,kis_broker,kis_futures_broker,broker_router,intraday_loop,killswitch,intents,analytics}.py`, `runner.py`(사이클 오케스트레이터), `scheduler.py`(KST cron)
- 사이징·엔진: `core/quant_core/ir_engine/{live,engine,spec}.py`, `exec_defaults.py`(통화·tol), `futures_contract.py`(코드↔심볼 dataset_for_contract)
- 서버: `server/app/{routers/{trading,commands,sync,preview},db,main,models}.py`, `preview_engine.py`
- 웹: `web/src/pages/Monitor.tsx`, `web/src/components/TradingTimeline.tsx`

**라이브 증거 데이터.** `~/.quant-platform/`: `ledger.json`(보유) · `pending_orders.json`(미체결·고아) · `cycles.jsonl`(사이클 요약·decisions) · `orders.jsonl`(발주/체결 이벤트) · `trades.jsonl`(정산손익) · `killswitch.json` · `intents.jsonl` · `preview_cache.json` · `logs/localapp.log(.1~.5)`

**진단 명령(추측 금지·직접 조회).**
- 로컬: ledger/pending/cycles/orders/trades JSON 직접 파싱(`python -X utf8`)
- 서버: `railway logs`(UTC+9=KST) · `gh pr view` · KIS KB `docs/kis-api/{INDEX,GOTCHAS}.md`
- 스케줄러 cron: `scheduler.py`(로컬 08:55·15:25·15:35·15:40 KRX / US 동적) · `main.py`(서버 07:30 dataset_global·18:15 dataset_kr→preview)

**관련 문서.**
- 메모리: `project-autotrading-failure-deepdive`(전 진행상황·근본원인 누적), `infra-neon-db-and-deploy`, `project-futures-autotrading`
- 핸드오프: `_wt-infra/docs/handoff/2026-06-12-server-infra-handoff.md`(#1~#6 self-contained)
- KIS 실측: `docs/kis-api/GOTCHAS.md`(δ 날짜창·예약 번호공간 2026-06-12 entry)
- 모듈 학습원장: `docs/modules/autotrade-engine.md`

**핵심 사실(검증됨).**
- KOSPI200 선물 승수 = **250,000원/pt** (06-12 정산 +24.9M = 33.2pt×3×25만)
- US 개장 = **22:30 KST**(EDT/서머타임), 종가 ~05:00 KST. 선물 종가 단일가 = 15:35~15:45
- 선물 단축코드 = **A01609**(9월물, ^A\d → dataset_for_contract → "코스피200선물"), 승수 검증
- 모의 체결 지연 ~**27초**(08:55:35→08:56:02), 종가청산 단일 resolve가 이를 놓침(θ)
- 현 프로덕션 = origin/main `53cdb0d`(+ NL 모델 Sonnet 4.6 #124 머지·배포완료), 로컬앱 = v0.9.36-beta

---

## 8. 검증으로 입증된 진단 모델 (2026-06-12~13 라이브)
- **국장 선물 진입**(08:55): 시장가 발주→체결→ledger 정상 = KR δ 면역 입증
- **국장 선물 종가청산**(15:40): θ 실패→18:54 수동복구(+24.9M) = θ·timing갭 입증
- **미장 GOOG**(22:10~05:05): FX 261주·δ 미기록·병1 무시(external_extras=1)·N1 방치 = US 복합결함 입증
- **preview**(07:30·18:15): 거짓 누락→자가회복 = P 슬롯판정 입증
- **equity**: cycles `post 10M vs pre 510M` = ε·D3-3 입증
→ **새 미지 결함은 없었다.** 모든 라이브 실패가 진단 모델과 일치 → 계획 확정 근거.
