# Phase 8 — System Trading 도메인 + 새 surface

## 골든 백테스트 (baseline)

```
pytest tests/golden_backtest.py -v → 15 passed, 1 skipped, 2 warnings in 213.84s
```

직전 0252 baseline과 동일 (회귀 0). 9fc742b "한국 비용 모델 정확화" cycle에서 baseline 재생성 → 그 이후 안정.

## QUANT_DOMAIN_CHECKLIST 7 카테고리

| # | 카테고리 | 결과 | 근거 |
|---|---|---|---|
| 1 | 백테스트 정확성 (look-ahead, 슬리피지, 배당, 분할) | ✅ | 9fc742b 한국 비용 모델 + golden 15 pass. tick rounding은 `core/quant_core/exec_defaults.py:round_to_tick`에 존재 |
| 2 | 시그널→주문 idempotency | ✅ | `local/localapp/intents.py` append-only journal + trader.py L-01 멱등 보호 (오버셀 방지 안전망) |
| 3 | 모의↔실전 일관성 | ✅ | mode 토글 + Dashboard 모드별 데이터 필터 확인 (Phase 3 navigate) |
| 4 | 리스크 관리 (kill switch, drawdown, 단일종목 한도) | ✅ | Q5(c18fc49) 장중 killswitch + L-10(b9e6180) pct_cash 단일종목 한도 + tier1/tier2 |
| 5 | KIS↔ledger 정합성 | ✅ | L-04 oversell clamp (`intraday_stop.py:171`) + L-09 dedup |
| 6 | 자격증명 분리 | ✅ | Phase 7 결과 — 서버 0건, 로컬 1개 모듈만 |
| 7 | 한국 시장 특수성 (시간·휴장·호가) | ✅ | Q2+Q8 캘린더 자동갱신(993e819) + S-02 ZoneInfo(4072738) + 호가 라운딩 모듈 |

## 새 surface (목표 명시 보완 항목)

| Surface | 검증 결과 |
|---|---|
| **calendar_cache · routers/calendars** (Q2+Q8) | `server/app/calendar_cache.py:103 refresh() -> dict` 정상. 03:00 KST cron + `_run_with_retry` retry 호출 (R-01 fix 후). |
| **로컬 sync retry thread** (Q1) | `local/localapp/sync_retry.py` 단일 thread + Event. 잔고 push 실패 시 지수 백오프 — race 없음 |
| **WS → REST 폴링 fallback** (Q3) | `kis_websocket.py` 끊김 감지 → REST 폴링 전환. 53710d5 적용 |
| **인트라데이 killswitch** (Q5) | `killswitch.py` 모듈 + 9개 파일 사용처. Tier 1+2 + 미체결 cancel + cycle lock |
| **DAY 단일정책** (Q7) | 5분 timeout 제거. DAY order 단일 |
| **intent ledger atomic** | append-only `json.dumps + \n` line journal. POSIX line append는 atomic, 다만 Windows 보장 부분적 — ⚠️ 잠재 위험 (deferred 검토) |
| **oversell clamp** (L-04) | `intraday_stop.py:171` KIS 실 잔고로 클램프 |
| **pct_cash 단일종목 한도** (L-10) | `trader.py` 적용. b9e6180. 섹터 한도는 미구현 — 차후 surface |

## 발견 결함

### **Q-01 (Low, 잠재 위험)** — intent ledger Windows append atomicity

`json.dumps(rec) + "\n"` append는 POSIX `O_APPEND` 보장에 의존. Windows에서 다중 프로세스 동시 append 시 line interleaving 가능성 — 현재는 단일 프로세스라 무방.

- 처리: 단일 프로세스 가정이 유지되는 한 안전. Phase 9 surface (잠재 위험만 기록).

### **Q-02 (Medium, 도메인 갭)** — 섹터 한도 미구현

DESIGN.md §자동매매 정책 4번에 "단일종목 비중 자본의 10% 클램프"는 있으나 **섹터 한도는 명시 없음**. 분산 투자 관점에서 도메인 갭.

- 처리: Phase 9 권장 후보. Phase B 입력에 포함.

### **Q-03 (Medium, 도메인 갭)** — 백테스트 vs 라이브 비용 일치성

DESIGN.md §자동매매 정책 5번에 "라이브에서는 의도가 vs 체결가를 bps로 누적 측정해 백테스트 가정과 비교"는 있으나 그 측정·비교 UI가 노출되지 않음.

- 처리: Phase 9 권장 후보. 사용자 신뢰 향상 큰 영향.

## 4원칙 자기검토 (Phase 8)

| 원칙 | 자기검토 |
|---|---|
| 근본원인 | Q-02·Q-03은 표면 픽스 아닌 도메인 갭 — 사용자 결정 게이트 필요 ✅ |
| Over-eng | 섹터 한도 신규 도입은 게이트 (자동 금지) ✅ |
| Over-think | 7 카테고리 체크리스트 표 1개로 완결 ✅ |
| 검증된 해결책 | golden 15 pass + 코드 inspection 신호 ✅ |
