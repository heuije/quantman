# 전 hour 기반 cron +9h 시프트 — CronTrigger가 스케줄러 tz(KST) 아닌 컨테이너 tz(UTC)에 앵커

- **일자:** 2026-05-20 ~ 2026-07-12 (도입 커밋 `fea36a9`부터 ~7.5주 상시), 확정·수정 2026-07-12
- **심각도:** High (전 데이터 파이프라인 발화 시각 무결성 상실 — 단, 마스킹 요인으로 체감 장애는 제한적)
- **상태:** 🟠 수정·테스트 완료 · 머지/배포 대기 (배포 후 첫날 발화 로그 대조 필요)

## 요약

`BackgroundScheduler(timezone="Asia/Seoul")`임에도 39개 cron 전부가 timezone 인자 없이
**미리 생성한 `CronTrigger(hour=…)` 인스턴스**로 `add_job`돼, apscheduler 3.x가 트리거를
tzlocal(=Railway 컨테이너 로컬 tz=UTC)에 앵커시켰다. 그 결과 hour/day 기반 cron **30개
job 전부가 KST 라벨 시각을 UTC로 해석해 +9h 시프트**로 돌았다 — 예: KIS 마스터 06:05
의도가 실제 15:05 KST(장중), KRX 종가 15:45 의도가 이튿날 00:45 KST.

## 발견

[2026-07-12 bonds_daily NameError 인시던트](2026-07-12-bonds-daily-nameerror-dup-fetch.md)
진단 중 "시도 1"이 07:40**Z**(=16:40 KST)에 발화한 것을 관찰(그 문서 '별건 관찰' 항목) →
본 작업에서 3계층 증거로 확정.

## 확정 증거 (3계층)

**① 라이브러리 소스** — 로컬 설치 3.10.4와 Railway가 해상하는 3.11.3(PyPI 최신 3.x) 동일:
- `CronTrigger.__init__`: `timezone=None`이면 `self.timezone = get_localzone()` (tzlocal 폴백).
- `BaseScheduler._create_trigger`: `isinstance(trigger, BaseTrigger)`면 **그대로 반환** —
  스케줄러 tz 주입(`trigger_args.setdefault('timezone', self.timezone)`)은 **문자열 형식
  (`add_job(fn, "cron", hour=…)`)에만** 적용. CronTrigger docstring의 "defaults to scheduler
  timezone"은 그 경로 얘기라 인스턴스 생성 시엔 성립하지 않는다(오독 유발 지점).

**② 프로덕션 로그** (2026-07-12, `railway logs <deployment-id> --json`, 배포 3건 전수):

| 배포(생존 구간 UTC) | 관측 | 판정 |
|---|---|---|
| `e514e65b` (05:18~06:33Z) | `[kis_master_1st] 성공` **06:05:04Z** (의도 06:05 KST → 실제 15:05 KST 장중) | UTC 앵커 ✓ |
| `9305b314` (07:35~08:32Z) | `[bonds_daily] 시도 1` **07:40:24Z** (+재시도 체인 07:45/08:00/08:31Z = backoff 5/15/30분 정합) | UTC 앵커 ✓ |
| `9305b314` | `[kospi_futures] 성공` **08:11:38Z** (08:10 라벨 job — KST 앵커였다면 익일 23:10Z가 최초 발화) | UTC 앵커 ✓ |
| `9305b314` | KST 앵커라면 발화했어야 할 시각 전부 침묵: `[naver]` 08:00Z(17:00 KST)·`[technical]` 08:15Z·`[kr_fundamentals]` 08:30Z (동명 startup 잡은 별도 마커 `startup job 시작:`으로만 존재) | KST 앵커 반증 ✓ |

컨테이너 tz: Dockerfile `python:3.12-slim`에 `ENV TZ` 없음 + `railway variables` 35개 중 TZ
없음 → glibc 기본 UTC.

**③ 로컬 재현** — `get_localzone()`→UTC 몽키패치(Railway 시뮬레이션) 하에
`server/tests/test_scheduler_timezone.py`가 수정 전 RED(kis_master_1st 다음 발화
06:05**Z** ≠ 21:05Z=06:05 KST) → 수정 후 GREEN.

## 영향

- hour/day 기반 cron **30개 job 전부 +9h**: KIS 마스터 06:05→15:05 KST(장중)·18:58→이튿날
  03:58, KRX 종가 15:45→이튿날 00:45, NAVER 17:00→이튿날 02:00, dataset_kr 18:15→이튿날
  03:15, 재무 마감일 20:10→**이튿날** 05:10(날짜 경계 초과), 주간 job(일 08:00 US 시총 등)도
  +9h. `kospi_futures_am`(08:10 라벨 — 08:55 아침 사이클 신선도 게이트용 신설)은 실측 17:11
  KST에 돌고 있었다 — 본 수정으로 처음 라벨대로 발화하게 된다.
- **왜 7.5주간 체감 장애가 없었나(마스킹 3요인):** ① minute-only 청크 cron 14개(10/30분
  간격)는 tz 무관 정상 ② 잦은 배포마다 startup 1회성 잡이 주요 소스를 재수집 ③ 실패
  재시도는 tz-aware 절대시각(2026-06 수정)이라 정상. 즉 데이터는 대체로 갱신됐지만 "외부
  publish 시각(KIS 06:00·KRX 15:40 등)에 맞춘 의도 시각" 보장이 전무했고, 배포가 뜸한 날은
  최대 9h+ stale(예: KRX 종가가 자정 넘어 반영).

## 근본 원인

apscheduler 3.x의 두 사실이 겹침: (a) 트리거 **인스턴스**는 스케줄러 timezone을 상속하지
않고 (b) timezone 미지정 CronTrigger는 tzlocal로 폴백 — 컨테이너에 TZ가 없어 UTC.
같은 저장소 `local/localapp/scheduler.py`는 전 트리거에 `timezone="Asia/Seoul"`을 명시해
정상(정상 선례) — 서버 `_build_scheduler`만 누락. 개발 PC가 KST라 로컬에선
`get_localzone()==Asia/Seoul`이어서 결함이 재현되지 않았다.

## 대응

1. `server/app/main.py`: 모듈 상수 `_TZ_SEOUL = "Asia/Seoul"` 도입(왜 필요한지 주석 포함),
   **39개 CronTrigger 전수**(minute-only 포함 — 통일 불변식) + 스케줄러 + 재시도 ZoneInfo에
   명시. 문자열 형식을 쓴 이유: 3.10(pytz)·3.11(zoneinfo) 양쪽에서 `astimezone(str)`이 안전.
2. 회귀 테스트 `server/tests/test_scheduler_timezone.py`: UTC 컨테이너 시뮬레이션 하에
   ① 전 cron 트리거(미래 추가분 포함) KST 앵커 invariant ② 대표 5개 job(kis_master_1st·
   bonds_daily·krx_1st·naver·cot_weekly)의 다음 발화 절대시각이 KST 라벨과 일치 assert.
3. `server/requirements.txt`: `apscheduler>=3.10,<4` 캡 — 4.x는 `BackgroundScheduler` 제거
   등 API 전면 개편이라 unpinned 상태에서 4.0 stable 출시 시 다음 빌드가 부팅 불능이 된다.

## 결과 (해소 검증)

- 로컬: 신규 테스트 RED→GREEN, server 스위트 **591 passed·1 skipped(회귀 0)**, ruff clean.
- 프로덕션: **머지/배포 후 첫날 대조 필요** — 기대 시그니처(전부 `--json` UTC 타임스탬프):
  `[krx_1st] 성공` ≈06:45Z(15:45 KST) · `[naver] 성공` ≈08:00Z+수분(17:00 KST) ·
  `[dataset_kr]` ≈09:15Z(18:15 KST) · `[kis_master_2nd]` ≈09:58Z(18:58 KST) ·
  `[kis_master_1st]` ≈21:05Z(익일 06:05 KST). 구 스케줄 시각(06:05Z KIS·07:40Z bonds 등)엔
  침묵해야 한다.
- 배포 전환일 특성: 새 스케줄러는 과거 시각을 소급 실행하지 않으므로 미발화 갭은 없고
  (startup 잡이 부팅 직후 주요 수집 1회 수행), 일부 job이 구(UTC)·신(KST) 시각으로 하루 2회
  돌 수 있으나 수집 함수가 멱등이라 무해(어차피 매 배포 startup으로 이중 실행돼 왔음).

## 재발 방지

- invariant 테스트가 **미래에 추가되는 트리거**도 KST 앵커를 강제(누락 시 CI RED).
- `_TZ_SEOUL` 상수 주석이 add_job 문자열/인스턴스 경로 차이를 코드 옆에서 설명.
- 교훈 distill → [docs/modules/data-engine.md](../modules/data-engine.md) §교훈·함정.
