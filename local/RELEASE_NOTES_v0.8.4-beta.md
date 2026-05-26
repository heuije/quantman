## 주요 변경 — v0.8.3 정정 (hot-fix)

v0.8.3에서 KIS WebSocket prefix 의미를 잘못 추정해 GUI에 "미국 실시간 시세는
KIS HTS [7781] 별도 신청이 필요" 라는 부정확한 안내를 노출했습니다.

**KIS 공식 spec ([해외주식] 실시간시세.xlsx HDFSCNT0):**
- `D + 시장구분(NAS/NYS/AMS) + 종목코드` (예: `DNASAAPL`)
  → **무료시세, 미국 0분지연 = 사실상 실시간**. 신청 불필요.
- `R + 시장구분 + 종목코드` (예: `RNASAAPL`)
  → 유료시세. KIS 포럼 FAQ 별도 신청 시에만. 일반 사용자 사용 안 함.

즉, **D-prefix만으로 미국 실시간 시세를 무료로 자동 제공**.
HTS [7781] 신청은 미국에 대해 불필요 (아시아 국가에만 해당).

## 코드 변경

- `kis_websocket.py`: prefix를 D-prefix(DNAS/DNYS/DAMS) **단일**로 변경.
  - 기존 v0.8.2 이전의 `BAQ/BAY/BAA` (spec에 없는 형식) 제거.
  - v0.8.3의 `us_realtime_enabled` 분기 로직 제거.
- `gui.py`: `🇺🇸 미국 실시간 시세 사용` 체크박스 + "신청 방법" 버튼 제거.
  사용자 결정 불필요 (default가 곧 최적).
- `intraday_loop._check_us_realtime`: 메시지 단순화. tick 없을 때 KIS
  WebSocket 장애 / 네트워크 단절 가능성으로 안내 (신청 안내 제거).
- `user_settings.py`: `us_realtime_enabled` 옵션 제거.

## 사용자 액션
- **없음**. v0.8.3 사용자도 v0.8.4 zip을 덮어쓰면 자동으로 무료 실시간 동작.

## Server 호환
모든 server 버전 호환. 변경은 로컬앱 WebSocket prefix만.
