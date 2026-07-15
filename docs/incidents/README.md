# 인시던트 로그

프로덕션/인프라 장애의 **발생·대응·결과**를 항상 여기에 기록한다(차후 참고용).
운영 중 발견한 장애는 해소 직후 파일 1개로 남긴다 — 같은 문제 재발 시 빠르게 참조.

## 작성 규칙
- 파일명: `YYYY-MM-DD-<짧은-슬러그>.md` (예: `2026-06-10-neon-data-transfer-quota.md`)
- 필수 섹션: **요약 · 발견 · 영향 · 근본 원인 · 대응 · 결과(해소 검증) · 재발 방지**
- 추측 금지 — 실제 로그·실측 근거를 인용. 미검증은 "추정"으로 명시.

## 인덱스
| 일자 | 심각도 | 제목 | 상태 |
|--|--|--|--|
| 2026-07-14 | Critical | [stale 선물 참조가(1210.5) → phantom 손익·불필요 매도·원장 분기](2026-07-14-stale-futures-ref-price.md) | 🟠 지혈 구현(미릴리스)·목표수렴 재설계 진행중 |
| 2026-07-12 | High | [전 hour 기반 cron +9h 시프트 — CronTrigger가 컨테이너 tz(UTC)에 앵커](2026-07-12-cron-utc-anchor-9h-shift.md) | 🟠 수정·테스트 완료·머지/배포 대기 |
| 2026-07-12 | Medium | [bonds_daily cron NameError — 수집 성공 직후 매번 실패·최대 5회 중복 재수집](2026-07-12-bonds-daily-nameerror-dup-fetch.md) | 🟠 수정·검증 완료·머지 대기 |
| 2026-07-07 | Critical | [자동매매 전 사이클 크래시 — Close-only 국채 시리즈 × ATR 무가드](2026-07-07-close-only-series-cycle-crash.md) | 🟠 수정·실데이터 검증 완료·릴리스 대기 |
| 2026-07-07 | High | [프로덕션 볼륨 디스크 풀 → HOME 종목조회 500·수십초 행](2026-07-07-volume-full-disk-space.md) | ✅ 해소(리사이즈·빌드/프로브 검증) |
| 2026-07-06 | Critical | [비상청산이 원장에 반대방향 유령 포지션 생성 (sid-미스매치·R6)](2026-07-06-emergency-liquidation-sid-orphan.md) | 🟠 수정·로컬검증 완료·모의 재검증 대기 |
| 2026-07-03 | Critical | [선물 원장↔브로커 분기 — LS 잔고코드 정규화 실패 + reconcile 파괴적 오정정](2026-07-03-futures-ledger-divergence.md) | 🟠 수정구현·모의 재검증 대기 |
| 2026-07-03 | Critical | [챗봇 전면 장애 — Sonnet 5 상향의 thinking·temperature 계약 변경](2026-07-03-chat-sonnet5-thinking-temperature.md) | ✅ 해소 |
| 2026-06-12 | High | [선물 종가청산 체결 미기록 (θ: 단일 resolve + 정산 cron 역순)](2026-06-12-futures-close-fill-unrecorded.md) | ✅ 해소(머지 대기) |
| 2026-06-10 | Critical | [자동매매 다중일 무발주 — 주간 회고 원장 (락 컨보이·거짓 kill-switch)](2026-06-10-autotrading-week-retrospective.md) | 🔴 미해소 |
| 2026-06-10 | High | [Neon 데이터 전송 쿼터 초과 → prod DB 연결 실패](2026-06-10-neon-data-transfer-quota.md) | ✅ 해소 |
