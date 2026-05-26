## 주요 변경 — 로컬앱 mini timeline 가시성 fix + 누락 알림

v0.8.8에서 mini timeline 구현은 들어갔는데 placement 버그로 화면에 안 보였음.
또 사용자가 "웹앱에는 누락 cycle 알림 보이는데 로컬앱은 왜 없냐"고 지적 — 같이 보강.

### 🐛 mini timeline placement fix

**증상:** v0.8.8 로컬앱에서 "다음 자동매매" 카드가 안 보임.

**원인:** [gui.py:201](localapp/gui.py:201)의 `next_frame`이 `_build`에서
.pack() 안 부르고 `refresh_status`에서만 호출. Tk pack은 호출 순서대로 배치
하므로, 다른 모든 위젯(setup_bar·kf·pf·af 등)이 먼저 packed된 *후* next_frame이
packed → root 가장 아래(스크롤 영역 밖) 위치 → 사용자 눈에 안 보임.

**Fix:** `_build`에서 hero 직후 미리 .pack()으로 위치 확보 + `pack_forget()`
으로 일단 숨김. `refresh_status`의 repack은 `after=self.hero` 옵션으로 위치 강제.

### 🆕 누락 cycle 알림

**기능:** hero 아래 mini timeline 카드에 빨간색 1줄 추가 — 오늘 예정 cycle이
지났는데 cycles.jsonl에 기록 없으면:

```
⚠ 오늘 누락된 cycle: KRX
```

판정 로직: KRX 평일 08:55, US는 캘린더에서 24시간 안의 직전 세션 open-5분.
각 sched 시각 ±30min 안에 cycles.jsonl entry 없으면 누락. PC 꺼져 있었거나
grace 초과 시 즉시 사용자에게 보이게.

누락 없으면 알림 줄 자체가 숨김 — 정상 운영 시엔 깔끔.

### v0.8.8 사용자에게 자동 도달

v0.8.8의 focus event 기반 update check가 GitHub에 v0.8.9 publish 직후
감지 → amber 배너 → [지금 업데이트] 클릭으로 자동 적용 (이번엔 터미널 안 뜨고
재시작 팝업 없이 깔끔).
