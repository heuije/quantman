## 주요 변경

### Phase 58-C — Dataset bundle 방식 (114분 → 1분)
- **단일 tar.zst 다운로드**: server가 모든 parquet을 단일 압축 파일로 packaging.
  종목별 4445 req 직렬 다운로드(~114분) → **1분 단축**.
- server cron 07:45/18:30 KST에서 dataset update 직후 자동 packaging.
- 로컬앱 08:00/08:20/08:40 KST cron + 기동 시 1회 자동 sync.
- ETag 비교로 변경 없으면 304 skip (불필요한 다운로드 안 함).
- 메모리에서 stream 압축 해제 — 사용자 디스크에 zip 안 남음.
- 실패 시 기존 manifest 종목별 다운로드 fallback (구 server 호환).

### Phase 58 — Heartbeat (정규장 외 alive 신호)
- 로컬앱이 5분 주기로 `/sync/heartbeat`에 lightweight ping.
- KIS API 호출 없음 — 단순 alive 신호.
- 새벽 등 cycle 외 시간에도 웹앱 "끊김 — 자동매매 중단됨" 표시 회피.
- 웹앱 HealthCard: snapshot received_at과 last_heartbeat_at 중 최신 사용.

### 의존성
- `zstandard>=0.22` 추가 (dataset bundle 압축 해제).

## 사용자 액션
- **이전 v0.8.0-beta 사용자**: 정규장 종료 후(15:30 이후) zip 덮어쓰기 권장.
  cycle 진행 중 process kill하면 dataset 다운로드 무효화 위험.
- KIS 토큰·DB(`~/.quant-platform/`) 그대로 유지 (호환).
- 첫 가동 후 logs/localapp.log에 "dataset bundle 적용: N parquet, X.Xs" 라인
  확인 시 sync 정상 동작.

## Server 호환
이 release는 server **commit 5d062e1** 이상과 호환 (bundle endpoint 포함).
구 server에서도 manifest fallback 경로로 정상 동작.

## 알려진 한계
- 첫 가동 시 server 측 bundle이 아직 packaging 안 됐으면 manifest fallback
  (~114분). 다음 cron(07:45 또는 18:30) 후엔 bundle 정상.
- Railway disk ephemeral — server 컨테이너 재시작 시 bundle 손실. 다음 cron
  까지 410 응답 (manifest fallback 자동).
