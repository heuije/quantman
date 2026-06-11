# 자동매매 ultra 심층 리뷰 — 결함 대장 + 구조 해법 설계 (2026-06-11)

> 배경: [docs/incidents/2026-06-10-autotrading-week-retrospective.md](../../incidents/2026-06-10-autotrading-week-retrospective.md)
> 방법: 6차원 병렬 read-only 코드 리뷰(D1 사이클 임계경로 · D2 주문 수명주기 · D3 equity/킬스위치 ·
> D4 서버 경계 · D5 시장×자산 패리티 · D6 스케줄/관측) + py-spy 실측 + 운영 저널 대조 + KIS docs 대조.
> 각 finding의 상세 증거(file:line·코드 인용)는 세션 트랜스크립트의 에이전트 보고 원문 참조.
> 표기: ✔=코드/실측으로 확정, ~=추정(검증 방법 명시됨). PR-1~4 = 4원칙 위반 분류.

## 0. 사고 인과 요약 (전부 ✔)

- **무발주(락 컨보이)**: bundle 410(배포 직후 휘발 디스크) → manifest 폴백(23,522 parquet 직렬
  신선도 검사, 총 데드라인 없음) → `_REFRESH_LOCK` 수 시간 점유 → 모든 사이클 적체 →
  17:33 장외 제출·22:10 무발주. (D1-1·D1-2·D4-1, py-spy 물증)
- **거짓 킬스위치 → US 북 전량 청산**: 해외 잔고 조회 실패가 무음 0 강등(`kis_broker.py:265-273`)
  → equity 10.06M(KR만) vs day_start 655.65M → -98.5% → 발동 → 닫힌 KR 3회 반복 제출 +
  열린 US 4종 실제 청산. (D3-1·D3-2·D3-5)
- **체결 미기록 → 거짓 "수동 매도" 기록**: ①예약주문 번호공간 비호환(D2-1) ②해외 체결조회에
  KST 날짜 전송(KIS는 현지 날짜 기준, D2-2) → fill 영구 미감지 → reconcile이 자기 체결을
  "HTS/MTS 수동 매도 추정"으로 진입가 제거(D2-4) — 실현손익·기록 영구 소실.
- **후보 0(거짓 stale)**: 서버 preview의 KR 신선도 판정이 생성 시점 고정 `ref_anchor=today` —
  KR 수집(18:15) 전에 rebuild되면 전일 종가(최신 상태)를 "1일 지연"으로 오판 → KR 후보 전멸.
  (D4-2, preview_cache 실측 — 데이터 갱신 실패가 아니라 판정 결함)

## 1. 결함 대장 — 7축 (46건)

### 축A. 발주 임계경로의 데이터 의존 (정지점 제거)
| ID | 한줄 | 확신 |
|---|---|---|
| D1-1 | `_REFRESH_LOCK` acquire 무한 + 임계구역에 네트워크/수만 파일 I/O 포함. `_CYCLE_LOCK`·`intraday_loop._lock`도 같은 부류 | ✔ |
| D1-2/D4-1 | manifest 폴백 = 문서화된 ~114분 경로를 호환성 명목 보존, 유니버스 5배 성장으로 7h+. 410은 자기 서버 결함의 봉합 [PR-1] | ✔ |
| D1-6 | 락 컨보이: dataset cron 3개+사이클이 적체, 해제 후 각자 폴백 전체 재실행 | ✔ |
| D1-7 | 진입이 로컬 캐시 신선도 무검사(stale 종가로 사이징 가능) | ✔(위험은 ~) |
| D4-6 | boot+300s 고정 sleep이 부분 bundle 생성 → 클라 universe 치환(union 아님) 축소 위험 [PR-4] | ~ |

### 축B. 발주 시간창·거부 처리
| D1-3/D5-2/D6-6 | 발주 시각 가드 전무 — `is_session_open`이 core에 있는데 사용처 0(2026-05-23 풀리뷰 기권고). 늦은 사이클이 장외 제출→거부→무음 소멸 | ✔ |
| D5-5/D3-6 | KIS 거부에도 intent='submitted' 유지 → 당일 정당 재시도 전면 차단(비상청산 반복 패턴의 메커니즘) [PR-1] | ✔ |
| D3-5 | 비상청산 시장 세션 비인지(의도적) + 개장 시 재실행 큐 부재 → 닫힌 시장 반복 제출 | ✔ |
| D1-3b | 거부 분류·알림 채널 부재("장종료"와 "증거금 부족" 무구분) | ✔ |

### 축C. 주문 상태기계·체결 감지
| D2-2 | 해외 체결조회 날짜 = PC KST(KIS는 미국 현지) + 단일일 조회창 → KST 자정 이후 체결 전부 미감지 | ✔ |
| D2-1/D5-1 | US 예약주문 번호공간 비호환(예약 ODNO ≠ ccnl odno), 예약조회 TR(TTTT3039R) 미배선·모의 미지원 → 영구 pending + ledger 미반영 + 전략 청산 대상 제외 | ✔ |
| D2-3 | fill 적용·영속 분리 + Trader 다중 인스턴스 → 3중 filled 실측. 일일 한도 과집계→거짓 매수 차단 가능 | ✔ |
| D2-4 | reconcile이 자기 체결 미감지분을 "수동 매도 추정"으로 진입가 제거 — 거짓 기록 실측 | ✔ |
| D2-5 | stale pending이 진입을 안 막음(pending 미참조) + 일경계 GC 부재 → 좀비 영구 | ✔ |
| D2-6 | 해외/선물 잔고 부분실패 시 reconcile이 해당 시장 ledger 전량 orphan 오삭제 가능(잠재 대형) | ~(경로는 ✔) |
| D5-7 | KR선물 체결통보 WS(H0IFCNI, 모의 지원) 미배선 — 4경로 중 REST 단독 | ✔ |
| D2-7 | 웹 CANCEL_ORDER가 BrokerRouter 우회(선물 취소 오라우팅) | ✔ |

### 축D. equity 단일 산출기·신뢰성
| D3-1 | 구성요소 부분 실패의 무음 0 강등(해외/USD행/선물 3분기) — 안전장치 입력 오염 [PR-1] | ✔ |
| D3-2 | 킬스위치 발동에 산출 신뢰성 가드 부재(-98.5%가 무검증 통과) | ✔ |
| D3-3 | 산출기 4개 분열(trader/intraday사본/자산곡선/total_eval) + 웹·서버는 분자(국내만)/분모(통합) 혼합 — US 보유자는 항상 -98% 표시 | ✔ |
| D3-4 | day_start_equity 시점·품질 무결성 부재(자정 직후 임의 사이클 값) | ✔ |
| D3-7 | 발동·해제 감사 이력 부재(절대값·구성요소 미기록) | ✔ |
| D3-8 | 야간 선물 잔고 TR(CTFN6118R) 미사용 — 야간 무음 누락 의심 | ~ |

### 축E. 숏 패리티 (directional 라이브 선결)
| D5-3 | 비상청산이 KR선물 숏을 raw side 비교로 롱 취급 → 청산 대신 숏 2배 [수정=norm_side 1줄] | ✔ |
| D5-4 | intraday stop이 숏 무인지 → 선물 숏 장중 보호 전무 + "외부 매도 추정" 거짓 로그 | ✔ |

### 축F. 서버 가용성·신선도·명령 신뢰성
| D4-2 | KR preview 신선도 판정 결함(생성 시점 고정) → 거짓 stale 후보 전멸 | ✔(실측) |
| D4-3 | pull_strategies 실패 무음 다운그레이드 — backoff 루프 밖 + 표면화 0 | ✔ |
| D4-4 | dataset 갱신 실패/stale의 사용자 가시 신호 0 — "후보 0(신호없음)"과 "후보 0(데이터 지연)" 무구분 | ✔ |
| D4-5 | SSE ~15분 절단(Railway edge ~900s 추정) × at-most-once 전달(yield 시점 delivered 마킹, 환원 없음) → 절단 직전 명령(LIQUIDATE_ALL 포함) 영구 유실 창 | ✔(절단 원인만 ~) |
| D1-5/D6-8 | RUN_CYCLE_NOW가 명령 수신 스레드에서 동기 실행 → 블록 시 후속 명령(비상청산) 전부 봉쇄 | ✔ |
| D3-5b | 명령 버스 TTL 부재 — 몇 시간 전 명령이 재연결 시 일괄 배달 | ✔ |

### 축G. 스케줄·관측 정합
| D6-1 | US 사이클 시각 이원화: 로컬 open−20(의도) vs 서버 timeline open−5(stale) — UI 22:25 오표시 + 성공 사이클 "missed" 오분류 윈도우 | ✔ |
| D6-2/D1-4 | 사이클 시작 저널 부재 — stall 식별 불가, catchup 이중 트리거 구멍, heartbeat는 거짓 green [PR-4] | ✔ |
| D6-3 | 저널 타임존 불일치(orders=UTC, intents=KST) | ✔ |
| D6-4 | APScheduler 미스파이어·스킵·예외 완전 무로그(로거 핸들러 미부착) | ✔ |
| D6-5 | run_cycle 트리거 5종(cron/수동/웹/catchup/dead code) 상호배제·출처 식별 없음 | ✔ |
| D6-7 | executor 풀 10 공유 — 블록 잡 누적 시 기아 가능 구조 | 구조✔ |

## 2. 구현 패키지 (직렬, 부류당 1 PR)

| PR | 내용 | 계층 | 효과 시점 |
|---|---|---|---|
| **α 서버 지혈** | F: D4-2 KR 신선도 판정 수정 · bundle 빌드를 refresh 완료 이벤트에 결합(+영속 볼륨 채택 시 410 창 소멸) · D6-1 timeline open−20 정렬 | server | **머지 즉시** (로컬앱 업데이트 불필요) — 단 머지 자체가 410 창을 만드므로 10:00~15:00 사이 권장 |
| **β 정지점 제거** | A: manifest 폴백 제거(410=캐시 진행) · 락 timeout+다운로드 임계구역 분리 · G1 cycle_id 시작/종료 저널 · G2 APScheduler 리스너 · G4 타임존 KST 단일화 · D4-3 strategies 재시도+경보 | local | v0.9.32 |
| **γ 시간창·거부·숏** | B: 발주 직전 단일 세션 게이트(is_session_open+마진) · 거부 시 intent 해제 · 비상청산 개장 큐 · 거부 분류 알림 + E: norm_side·intraday 숏 인지 (**directional 모의 전 필수**) | local | v0.9.32 |
| **δ 체결 감지** | C: 해외 조회 현지날짜·구간화 · fill 단일 진입점+멱등+즉시 영속 · pending 일경계 GC · reconcile 안전화(부분실패 시장 제외+pending 교차확인) · 진입 pending 게이트 · 예약주문 해소 모델(결정 필요) | local | v0.9.32 |
| **ε equity 단일화** | D: equity.py 단일 산출기{value, components, complete} · 킬스위치/day_start/drawdown는 complete만 소비 · payload unified 필드로 웹 -98% 수정 · 발동 이력 저널 | local+server+web | v0.9.32 |
| **η 명령 신뢰성** | F: at-least-once(delivered 미ack 환원+dedupe) · TTL · RUN_CYCLE_NOW 비동기 spawn | server+local | v0.9.32 |
| 후순위 | D5-7 선물 체결 WS · D5-6 US 당일매매 종가청산(또는 게이트 차단) · D3-8 야간 TR 실측 · D2-7 라우터 통일 · D6-5 dead code 제거 | | 후속 |

## 3. 검증 계획 (4원칙 ④)

- 결함별 **재현 테스트 선작성**(SimBroker/Mock): 락 타임아웃 폴스루 · 부분 equity 발동 보류 ·
  거부 후 재시도 가능 · 숏 청산 방향 · 예약 매칭 · fill 멱등(3중 재적용 거부) · 거짓 stale 판정.
- 백테스트 골든 14 byte-identical · 로컬 시나리오 전체 green.
- 배포 후: 다음 국장 08:55·미장 사이클을 새 저널(cycle_id)로 관찰 — "무발주 1회 = 원인 로그 1건" 계약 확인.

## 4. 결정 대기 (설계 게이트)

1. **US 예약주문 해소 모델** — (b) 예약 유지 + 개장후 ccnl (symbol,side,qty,현지날짜) 매칭으로
   본주문 채택(+실전은 TTTT3039R) ← 권장(backtest next_open 패리티 유지) / (a) 모의는 즉시주문 전환.
2. **Railway 영속 볼륨** — 채택 시 410 창 자체 소멸(권장, 비용·단일 replica 트레이드오프) /
   미채택 시 이벤트 기반 재생성만(빈 디스크 재수집 ~1.5h 창 잔존, β의 폴백 제거가 로컬 영향은 차단).
