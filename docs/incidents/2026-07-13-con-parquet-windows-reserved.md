# 2026-07-13 — CON.parquet Windows 예약명 번들 추출 크래시 → 실유저 무발주

## 발생
실전계좌 연동 유저(mwmw·LS 국내선물·예수금 5.65억)의 자동매매가 **설치 이래 발주 0**.
장중 사이클마다 전 전략이 `skip_no_data`("전일 종가 없음 — 서버 dataset 갱신 대기").
서버·번들 빌드·데이터는 모두 정상으로 찍혀 며칠간 원인 미상.

## 원인 (실측 확정)
- 서버 dataset 번들(tar.zst)에 **`CON.parquet`·`PRN.parquet`** 포함. "CON"(Concentra Group,
  NYSE)·"PRN"은 실재 티커이지만 **Windows 예약 장치명**(CON·PRN·AUX·NUL·COM1-9·LPT1-9).
- 파일명이 `{symbol}.parquet`로 **무검증** 파생(`data_fetcher._parquet_path`는 `/`만 치환,
  `parquet_io.write_parquet_atomic`엔 이름검증 없음). 데이터엔진은 Linux라 정상 저장·번들.
- **일부 Windows 환경**에서 `tar.extract`가 CON을 콘솔 장치로 해석 → `[WinError 6]`.
  추출 루프(`sync_client.py`)에 **per-member 격리가 없어** 그 지점에서 전체 추출이 중단 →
  dataset 129종 중 1종만 로드 → 전 전략 skip → 발주 0. etag가 루프 뒤에 저장돼
  **미저장→매 사이클 같은 번들 재다운로드·재크래시(영구 poison-pill)**.
- ⚠ 크래시는 **환경 의존**: 조사 머신(Win11 26100·Py3.11/3.12 모두)에선 CON이 실제 파일로
  생성돼 **재현 불가**. mwmw의 Windows 환경 고유. floo(조대표)는 CON 유입 전 완전 추출본을
  보유(추출은 additive)해 무사. → **콘솔/Python버전 가설은 실측으로 모두 기각**.

## 대응 (PR: fix/reserved-filename-safe)
1. **core `sanitize_fs_name`(SSOT)** — 정상 이름 byte-identical, 예약명·금지문자·말미점공백만 remap.
2. **서버 번들 arcname 안전화**(`build_bundle`) — 예약명을 안전 이름으로만 배포. **앱 업데이트
   없이** mwmw 다음 사이클 복구(디스크 원본은 Linux서 유효 → write경로·재수집 불필요).
3. **클라 per-member 격리 + 안전이름 write**(`sync_client`) — 한 멤버 실패가 전체를 못 죽이게 +
   스트리밍 tar desync 방지(extractfile로 바이트 선소비) + 실패를 결과에 표면화(텔레메트리).
4. **CI 테스트 게이트**(build-local.yml) — build-only였던 CI에 회귀 테스트 잡 추가. 크래시 재현이
   아니라 "예약명이 번들에 안 실림"을 assert(OS·버전 무관 결정적).

## 결과
실측 검증 통과(실 `build_bundle`로 예약명 제거 확인·클라 격리 재현·정상 심볼 byte-identical·
기존 스위트 회귀 없음, 30 tests green). 배포 대기(사용자 승인).

## 교훈
- **기전 추론 연속 오류**(번들 레이스·구버전·프리즈·콘솔·Py버전 — 전부 실측으로 기각). 유저 로그와
  실 재현만 신뢰. 못 재현하는 크래시는 "재현" 대신 "위험 요소 제거"를 assert로 검증.
- **플랫폼 비대칭**: 산출물 소비자는 Windows인데 서버·CI·개발이 Linux. 배포 경계에서 소비자 OS
  안전성을 강제.
- **blast-radius 비대칭**: READ 경로엔 per-file 격리(`read_parquet_safe`)가 있는데 EXTRACT엔 없었음.
  같은 부류 방어는 전 지점에 대칭 적용.
- **책임전가 메시지·조용한 폴백·텔레메트리 부재**가 진단을 며칠 지연. (후속: skip 사유 재작성 +
  클라 실패의 서버 스냅샷 노출 — 별도.)
