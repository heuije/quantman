# 2026-07-07 · 프로덕션 볼륨 디스크 풀 → HOME 종목조회 500·행

**심각도**: High · **상태**: ✅ 해소(리사이즈·검증 완료)

## 요약
Railway 프로덕션 볼륨 `/srv/data`(5GB)가 꽉 차(`5007MB/5000MB`) **모든 디스크 쓰기가
실패**(`OSError: [Errno 28] No space left on device`)했다. 그 결과 HOME(개별종목분석)에서
종목 조회 시 피드 캐시·번들 쓰기가 실패해 `/market/kr` 500·`/market/symbol` ~59초(클라 포기
499)가 발생. 볼륨을 10GB로 리사이즈해 해소.

## 발견
- **유저 신고**: 웹앱 HOME에서 동화약품(000020) 조회 시 로딩이 한참 걸림.
- **Railway HTTP 로그**(04:50 KST경, 실측):
  ```
  GET /market/symbol/000020 499 59064ms   (×4·클라 포기)
  GET /market/kr/000020     500 3089ms
  GET /market/profile/000020 200 5414ms
  ```
- **500 traceback**(실측):
  ```
  File "/srv/server/app/routers/market.py", line 517, in kr_extras
  OSError: [Errno 28] Error writing bytes to file. Detail: No space left on device
  ```
- **볼륨 실측**: `railway volume list` → `Storage used: 5007MB/5000MB` (100% 초과).

## 영향
- HOME 개별종목분석 조회가 **간헐 500·수십초 행**(디스크 쓰기 의존 경로: 피드 캐시·번들 `.tmp`).
- **번들 빌드 실패** — build_bundle이 `.tmp`를 못 써(No space) 빌드가 완료되지 않음 →
  재배포 후에도 번들 미갱신. (디스크풀 기간 `압축 시작`/`갱신` 로그가 관측 창에 0이던 이유.)
- 디스크풀 기간 조용한 degrade(쓰기 실패가 500/행으로만 표출).

## 근본 원인
**볼륨 용량(5GB)이 데이터 성장을 못 따라감 — near-full 상태에서 오늘 추가분이 한도를 넘김.**
- **유력 트리거(실측 뒷받침)**: 2026-07-07 머지된 **#331 'full' 번들 스코프**가 두 번째 대형
  번들 `dataset-bundle-full.tar.zst`(**실측 1093MB·71,503 files**)를 볼륨에 추가 → 번들 저장이
  trading(726MB) 단독에서 **~1.8GB로 ~2.5배**. 이미 꽉 찬 5GB를 초과시킨 것으로 **추정**.
- 부수 성장: 공매도 잔고 피드(#329)·enrichment 피드·KR/US OHLCV 누적.
- (별개지만 얽힘) build_bundle의 CPU 독점(`threads=-1`)은 [PR#333]에서 `threads=0`로 수정 —
  디스크풀이 그 빌드를 실패시켜 두 문제가 동시 표출.

## 대응
1. **볼륨 리사이즈 5GB → 10GB** (사용자 실행·Railway 대시보드·비파괴). 즉시 쓰기 정상화.
2. build_bundle `threads=-1` → `threads=0`([PR#333] `7a6497c`) — 압축 CPU 경합 완화(별건이나 동반).

## 결과 (해소 검증)
- `railway volume list` → **`5007MB/10000MB`(50%)** — 여유 확보.
- **번들 빌드 정상 완료 실측**(04:12Z): `bundle(trading) 726MB 8.9s` · `bundle(full) 1093MB 48.0s`
  — 디스크풀 땐 불가능하던 `.tmp` 쓰기가 정상화됐음을 증명.
- **프로브**(`[probe] summary`): 리사이즈 후 33~120ms(스파이크 0), 압축 2회 창에서도 ≤120ms
  ([PR#333] threads=0 검증 겸).
- 잔여: 유저의 동화약품 재조회 정상 확인.

## 재발 방지
1. **볼륨 사용량 모니터/알림** — full 번들 1.1GB가 주기적 재充하므로 임계(예 80%) 알림 필요.
2. **build_bundle 실패·중단 시 `.tmp` 정리 + tar.add 동시성 가드** — 중단된 빌드의 orphaned
   `.tmp`(대형)가 누적되면 디스크를 잠식. (dataset.py 후속.)
3. **번들 저장 최적화 검토** — full 스코프의 필요성·중복(trading⊂full)·보관 정책 재검토.
   dev 전용 full 번들을 볼륨에 상주시킬지 vs 온디맨드 빌드.
4. 배포 후 볼륨 여유를 배포 게이트/스모크에 포함.
