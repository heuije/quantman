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

## 관련
- 인프라/배포 토폴로지: 메모리 `infra_neon_db_and_deploy.md`
- 진단 채널(CLI): `CLAUDE.md` §7
