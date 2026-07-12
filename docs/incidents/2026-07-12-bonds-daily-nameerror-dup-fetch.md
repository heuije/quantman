# bonds_daily cron NameError — 수집 성공 직후 매번 실패·최대 5회 중복 재수집

- **일자:** 2026-07-07 ~ 2026-07-12 (PR #325 배포 시점부터 상존)
- **심각도:** Medium (유저 가시 장애 없음 — 외부 API 중복 부하·에러 로그 소음·재무 캐시 무효화 dead화)
- **상태:** 🟠 수정·검증 완료 · 머지/배포 대기

## 요약

PR #325(d46815c, 07-07 머지)가 `_refresh_bonds` 함수를 `_refresh_financials`의
`refresh_all()`과 `financials.clear_cache()` **사이**(한 줄 이른 위치)에 삽입해, 원래
`_refresh_financials`의 마지막 줄이던 `clear_cache()`가 `_refresh_bonds`의 꼬리로 편입됐다.
`_refresh_bonds` 스코프에는 `financials`가 없어(ruff F821) bonds_daily cron이 **수집을 전부
성공시킨 직후** NameError로 실패 처리되고, `_run_with_retry`가 backoff[5,15,30,60,120]분으로
FRED/MOF/ECB 전체 수집을 최대 5회 중복 실행했다.

## 발견

다른 세션(claude/nervous-bardeen-f777e5)이 QP_SKIP_STARTUP_JOBS 작업 중 ruff F821로 부수
발견(`main.py:1030 financials — Undefined name`) → 별도 작업 칩으로 분리 → 본 세션이 수정.

## 영향

Railway 프로덕션 로그 실측(2026-07-12, UTC):

```
07:40:24Z  ERROR [bonds_daily] 시도 1 실패: name 'financials' is not defined
07:45:34Z  ERROR [bonds_daily] 시도 2 실패: name 'financials' is not defined
08:00:44Z  ERROR [bonds_daily] 시도 3 실패: name 'financials' is not defined  (#4 17:30 KST·#5 예약)
INFO [altdata] 국채금리 수집(FRED/MOF/ECB): {'US': 16114, 'JP': 13248, 'EU': 5583, 'KR': 425, 'CN': 317}  ×3회 동일
```

- **중복 fetch:** 시도마다 `bonds.refresh_all()`이 완주(동일 건수 ×3 실측) 후 NameError —
  발화당 최대 5회 전체 수집. 볼륨 parquet 쓰기는 예외 **이전**에 완료되므로 국채 탭 서빙
  데이터는 정상(유저 가시 장애 없음).
- **재무 캐시 무효화 dead화:** `_refresh_financials`가 의도한 당일 메모리 캐시 무효화를 상실.
  단, 재무 cron(분기 마감일 20:10 — 3/31·5/15·8/14·11/14)은 결함 기간(07-07~) 중 발화한 적이
  없어 실영향 0. 다음 발화(8/14) 전 복구.
- **별건 관찰(추정·별도 칩 분리):** 위 시도 1이 07:40 **UTC**(=16:40 KST)에 발화 — cron 의도는
  07:40 KST. `BackgroundScheduler(timezone="Asia/Seoul")`이어도 timezone 인자 없이 **미리
  생성한** `CronTrigger`는 컨테이너 로컬 tz(UTC)에 앵커되는 apscheduler 3.x 동작이 의심된다
  (hour 기반 cron 22곳 전수 +9h 시프트 가능성). 본 인시던트와 별개 결함이라 별도 작업으로 분리.

## 근본 원인

함수 삽입 위치 실수(코드 이력으로 확정):

- b9dc2a1: `financials.clear_cache()`를 `_refresh_financials` 마지막 줄로 추가(함수 내
  `from . import financials`가 있어 유효).
- d46815c(PR #325): `_refresh_bonds`를 그 줄 **바로 위**에 삽입 — `git log -L`에서 해당 줄이
  unchanged로 남아 새 함수 꼬리로 편입된 것이 확인된다. 결과적으로 ① bonds에 미정의 이름
  호출이 생기고 ② financials는 무효화를 잃는 이중 결함.

## 대응

`financials.clear_cache()`를 원소유 함수(`_refresh_financials`) 끝으로 원복 — import 추가(의미상
틀린 호출 존치)도 단순 삭제(의도 기능 소실)도 아닌 위치 복구가 근본 수정.

## 결과 (해소 검증)

- 회귀 테스트 RED→GREEN: `server/tests/test_refresh_jobs.py` 2건(① `_refresh_bonds` 무예외 완주
  ② `_refresh_financials` 캐시 무효화)이 수정 전 정확히 실패, 수정 후 통과.
- `ruff check --select F821 server/app` 수정 전 1건 검출 → 수정 후 clean.
- server 전체 스위트 green (커밋 시점 실행).
- 배포 후 확인 예정: 다음 bonds_daily 발화에서 `[bonds_daily] 성공 (시도 1)` 1회만 기록되는지.

## 재발 방지

- `server/tests/test_refresh_jobs.py` — 두 결함 각각의 직접 회귀 가드(외부 fetch 전부 mock).
- ruff F821은 이 결함류를 정적으로 잡는다(`server/ruff.toml` 존재) — 단 자동 실행 인프라(CI
  lint)가 없어 6일간 미검출. 서버 CI에 `ruff check` 도입 검토 제언(팀 결정 사항).
- 교훈: 함수 삽입 시 이전 함수의 **마지막 줄 경계**를 확인 — diff에서 삽입 지점 직후 줄이
  unchanged면 그 줄의 소속이 바뀌었는지 본다.
