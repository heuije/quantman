## 주요 변경 — 자동매매 투명성 + release 운영 개선

이번 release는 "지금 자동매매가 어떻게 돌아가는지" 사용자가 한눈에
파악할 수 있도록 GUI·웹앱에 타임라인을 추가했습니다. 또 v0.8.7에서
발견된 release 운영 이슈 3건을 함께 fix.

### 🆕 자동매매 일정 가시화

**로컬앱 GUI** — hero 카드 바로 아래 다음 자동매매 시각 2줄:

```
다음 KRX 사이클:  내일 08:55 KST  (7h 17m 후)
다음 US 사이클:    오늘 22:25 KST  (5h 28m 후)
```

매분 자동 갱신. scheduler 가동 중일 때만 표시.

**웹앱 트레이딩 페이지** (https://quantman.vercel.app/monitor) —
최상단 "자동매매 상태" 패널. 어제·오늘·내일 시간순 이벤트:

- ✓ 완료한 cycle (매수/매도 건수)
- ⏳ 다음 예정 시각 + 남은 시간
- ✗ 누락된 cycle (PC 꺼져 있었거나 grace 초과 — hover로 이유)
- — 휴장일

heartbeat (로컬앱 alive 여부)도 상단에 배지로.

### 🐛 인앱 업데이트 — 사용자가 창에 돌아올 때마다 재체크

**증상:** v0.8.7에선 앱 시작 시 1회만 GitHub 버전 조회. PC를 며칠 안 끄면
새 release가 publish돼도 인앱 알림이 영영 안 옴 — 사용자가 수동 재시작
해야 발견.

**Fix:** [gui.py](localapp/gui.py)에서 `<FocusIn>` 이벤트 binding 추가. 트레이
복원·alt-tab·다른 창에서 돌아옴 모두 자동 재체크. polling 없음 — 사용자가
보지 않을 땐 트리거 안 됨. 60s throttle로 위젯 사이 빠른 클릭 시 중복 차단.

### 🐛 release 운영 fix (v0.8.7 발견)

**zip 폴더명에 버전 명시**
이전엔 압축 풀면 `QuantPlatformLocal/` (무버전). 이제
`QuantPlatformLocal-v0.8.8-beta/` — 어느 버전 설치돼 있는지 폴더만 봐도 식별.

**pre-release도 자동 업데이트 알림 감지**
[`updater.py`](localapp/updater.py)가 `/releases/latest` 대신 `/releases` 전체 조회 +
SemVer 정렬. GitHub `/releases/latest`는 pre-release를 제외하는데, 우리가
release를 pre-release로 표시하면 인앱 알림이 안 떴음. 이제 그 한계 우회.

**v0.8.7-beta를 "Latest"로 승격, 옛 release 무버전 zip 정리** (GitHub 측 조치 완료)
v0.8.1까지 동반된 `QuantPlatformLocal.zip` (무버전) 삭제. 모든 release가
versioned asset만 보유. 웹앱 다운로드 버튼도 동적 versioned URL 사용으로 변경.

### 검증

- `tsc -b && vite build` 통과
- 서버 trading 라우터 import + route 등록 확인
- 로컬앱 gui.py syntax + `_format_next_run` 7가지 케이스 smoke test 통과
- 컴포넌트 visual 검증 (4 status 모두 OK)

### 업그레이드 방법

**v0.8.7 사용자:** 로컬앱 상단에 자동으로 amber 배너 노출 →
[지금 업데이트] 클릭. zip 다운로드·압축 해제·재시작 모두 자동.

**v0.8.1 이하 사용자:** [v0.8.8-beta release 페이지](https://github.com/MercKR/quantman-releases/releases/latest)
에서 zip 수동 다운로드. 이번 한 번만 수동이고, 이후 release는 자동.
