# 인시던트 — Neon 데이터 전송 쿼터 초과로 프로덕션 DB 연결 실패

- **일자:** 2026-06-10
- **심각도:** High (프로덕션 DB 의존 기능 전면 실패)
- **상태:** ✅ 해소(Resolved)
- **영향 범위:** 서버(Railway) ↔ Neon Postgres 전 구간 — 로그인·전략·동기화·자동매매

## 요약
A1 백필 커버리지를 확인하려 Railway 런타임 로그를 보다가, prod 서버의 Neon DB
연결이 전부 거부되고 있음을 발견. Neon이 **데이터 전송(egress) 월 쿼터 초과**로
연결을 막아, DB를 건드리는 모든 요청이 실패하고 있었다.

## 발견 (Detection)
- 계기: "A1 백필 완료됐는지 확인" 요청 처리 중 Railway 로그 점검.
- 로그 신호(발견 시점 기준 최근 45분 내 활성, 반복):
  ```
  ERROR: Your project has exceeded the data transfer quota. Upgrade your plan to increase limits.
  (Neon ep-gentle-thunder-aoym1to9-pooler.c-2.ap-southeast-1.aws.neon.tech / 다수 IP)
  sqlalchemy.exc.OperationalError: (psycopg.OperationalError) connection is bad ...
  ```
- HTTP 실측(발견 시점):
  | 점검 | 결과 | 해석 |
  |--|--|--|
  | `/`·`/symbols`·`/auth/me` | 404/401 **0.48s** | 서버 프로세스는 살아있음(빠름) |
  | DB 연결 | 전부 거부 | DB 의존 기능 전면 실패 |

## 영향 (Impact)
- 서버 프로세스 자체는 정상 응답(비-DB 경로). 그러나 **DB를 쿼리하는 모든 경로 = 실패**:
  - 로그인·회원가입(인증) → 브라우저 로그인 불가
  - 전략 조회/저장, 동기화(sync), NL 컴파일
  - **자동매매**: heartbeat·sync·commands(SSE) 실패 → 로컬앱 끊김 표시·명령 미전달 위험
- 부수 영향: A1 백필 커버리지 측정 자체가 불가(인증 경로가 DB 의존이라 막힘).

## 근본 원인 (Root cause)
- Neon 서버리스 Postgres의 **현 요금제 월 데이터 전송(egress) 한도 소진** → 신규/기존 연결 거부.
- 누적 egress의 주요 동인(추정): 로컬앱의 **sync/heartbeat/commands 폴링 + 크론 DB 읽기**가
  한 달간 쌓인 것. (관련 메모리의 "sync-poll spam" 맥락과 일치 — 확정은 egress 계측 필요.)

## 대응 (Response)
1. Railway 로그(`railway logs`)로 쿼터 초과 에러 확인 → prod 장애로 판단, 사용자에게 즉시 보고.
2. 빌링/요금제 변경은 사용자 영역(금융 행위) — 사용자가 **Neon 요금제 업그레이드** 수행.

## 결과 (Result) — 해소 검증
업그레이드 직후 실측:
- Railway 로그(최근 8분): `/sync/snapshot 304`·`/sync/timeline 200`·`/market/context 200`·
  `/auth/devices 200`·`/trading/timeline 200`·`/preview/next-day 200` — 쿼터 에러 소멸.
- DB 직접 연결 프로브(`railway run python`): `SELECT 1 = 1`, `compilelog` 32→**38행**(트래픽 재개 확인).
- → **프로덕션 DB 정상 복구.**

## 재발 방지 (Follow-up)
- [ ] **[근본]** DB egress 절감 — 로컬앱 sync/heartbeat/commands 폴링 빈도·DB 왕복 축소,
  heartbeat를 DB 밖(메모리/캐시)으로, 변경 없는 sync는 304 최대 활용. (egress 계측 선행)
- [ ] **[모니터]** Neon 사용량(전송/컴퓨트) 주기 점검 — 한도 근접 시 사전 경고.
- [ ] A1 백필 커버리지 확인은 이 장애로 보류 → 복구됐으므로 로그인된 세션에서 재개.

## 재발 방지 구현 (2026-06-10, 폴링 endpoint egress 절감)

코드 리딩으로 egress 주요 동인 3건(D1·D2·D3)을 확정하고 4개 수정으로 닫았다.

### 동인 → 수정 매핑

| ID | 동인 (코드 리딩 확정) | 수정 |
|--|--|--|
| **D1** | `/trading/timeline`(웹 60s)·`/sync/timeline`(로컬앱 60s, 24/7)이 둘 다 `_snapshots_in_window`로 ±24h 윈도우의 **full SyncSnapshot row(수십~수백KB payload JSON 포함)**를 SELECT한 뒤 `cycle_summary`(+최신 `next_day_preview`)만 사용. 폴 1회당 수백KB, 일 ~2400폴. | **Fix A** — `_snapshots_in_window`를 `cycle_summary` 필드만 DB-level projection(SQLite=json_extract / PG=`->`)으로 변경, `SnapLite`(received_at·payload) 경량 행 반환. preview는 `_latest_preview_in_window`로 `next_day_preview` 필드만 projection. consumers·이벤트 동등성 무변경. 두 timeline endpoint 공통 적용. |
| **D2** | `/trading/timeline`에 ETag 부재 — 폴 1회마다 full rebuild+response. | **Fix B** — tag-first ETag(`W/"<data_ms>-<bucket>"`). 윈도우 조회 *전에* scalar 2개(최신 received_at·last_hb)로 ETag 계산, If-None-Match 매칭 시 윈도우 조회 0회로 304. 300s 버킷으로 로컬앱 사망 시에도 stale ≤5분(scheduled→missed 전이 보장). 웹은 `api.ts` etagCache가 이미 처리 → 웹 API 변경 0. |
| **D3** | `/sync/snapshot`(웹 15s)이 ETag 계산 *전에* full snapshot row(payload 포함)를 로드 — 304 응답조차 매 폴마다 payload를 Neon에서 읽음. | **Fix C** — received_at scalar로 ETag를 먼저 계산, 304 경로에서 payload 컬럼 미열람. ETag 공식·값은 종전과 byte-identical(기존 클라이언트 캐시 유효). |
| — | 304로 `data.now`가 응답 사이 stale → 상대 시각("4h 29m 후") 동결. | **Fix D** — `TradingTimeline.tsx`가 상대 시각을 서버 now가 아닌 클라이언트 시계(30s 갱신)로 계산. 폴링은 60s 유지, 절대 시각은 서버 응답 그대로. |

### 기대 효과
- timeline 경로: 변경 없는 폴은 304(body 0·윈도우 조회 0) → 이 경로 egress **~99% 절감**.
- `/sync/snapshot`: payload 읽기가 **실제 변경(snapshot push·heartbeat) 시에만** 발생.

### 의도적 후속(이번 범위 제외)
- **로컬앱 `/sync/timeline` If-None-Match**: 로컬앱 클라이언트가 아직 미송신 → ETag 추가는 **로컬앱 릴리즈**와 함께. 단 Fix A projection은 이 경로에도 이미 적용됨.
- **payload 다이어트**(스냅샷 payload 자체 축소): 스키마·로컬앱 양쪽을 건드리는 침습적 변경 → egress가 여전히 문제일 때만.

### 검증
- 서버: `test_timeline_egress.py` 5건 추가(projection json_extract·이벤트 동등성·ETag 304/버킷·304 무-payload·shape 무변경) + 전체 스위트 그린.
- 웹: `tsc -b` 통과·`eslint TradingTimeline.tsx` 무오류.

### D4 — 교차출처 ETag 미노출(라이브 검증 중 실측, PR #76)
PR #75 머지·배포 후 로그인 세션에서 라이브 검증하니 `fetch('/trading/timeline').headers.get('ETag')` = **null**.
원인: **CORSMiddleware에 `expose_headers` 미설정**. ETag는 CORS-safelisted 응답 헤더가 아니라 expose
없이는 교차출처(vercel→railway) 브라우저 JS가 읽지 못한다. → `web/src/api.ts`의 etagCache가 ETag를
저장 못 해 **If-None-Match를 한 번도 송신하지 않았고**, 기존 P0-1·이번 PR#75의 ETag/304 최적화가
**프로덕션 웹에서 0회 동작**(모든 폴이 full payload 200)이었다 — egress 폭증의 숨은 공범.
- **Fix(PR #76)**: `expose_headers=["ETag"]` 1줄 + Origin 동반 요청으로 `Access-Control-Expose-Headers`를
  검증하는 테스트. **교훈: ETag류 헤더 최적화는 반드시 교차출처 라이브로 검증** — same-origin TestClient는 이 부류를 못 잡는다.

### 라이브 검증 결과 (2026-06-10, 배포 60dbf8e, 로그인 세션)
| 경로 | 1차 | If-None-Match 재요청 |
|--|--|--|
| `/trading/timeline` | 200 · ETag `W/"<data_ms>-<bucket>"` · 1.3KB | **304 · body 0** |
| `/sync/snapshot` | 200 · ETag `W/"<data_ms>"`(기존 공식) · **30.4KB** | **304 · body 0**(payload 미전송) |

→ 30KB 스냅샷이 변경 없을 때 0바이트 304로 종결, ETag 교차출처 read 정상. egress 절감 실증 완료.

## 관련
- 인프라/배포 토폴로지: 메모리 `infra_neon_db_and_deploy.md`
- 진단 채널(CLI): `CLAUDE.md` §7
