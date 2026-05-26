## 주요 변경 — KIS spec 종합 정정 (해외 주문 + WebSocket)

### 🚨 자금 안전 — 해외 미국 실전 주문 tr_id 정정

| 기능 | v0.8.5 이전 | v0.8.6 (KIS 공식 spec) |
|---|---|---|
| 미국 매수 (실전) | `JTTT1002U` | **`TTTT1002U`** (J→T) |
| 미국 매도 (실전) | `JTTT1001U` | **`TTTT1006U`** (완전 다름) |
| 미국 매수/매도 (모의) | `VTTT1002U` / `VTTT1001U` | 동일 (변경 없음) ✓ |

v0.8.5 이전의 J-prefix는 spec에 없는 형식. 모의 사용자는 영향 없었으나
실전 사용자가 등장하면 미국 매매 즉시 실패 위험이었음.

### 🛡 해외 미체결 모의 가드

`inquire-nccs` API는 KIS 공식 spec상 **모의투자 미지원**. v0.8.5 이전엔
`VTTS3018R` 호출했으나 spec 미명시 — 호출 시 실패 가능.
v0.8.6부터 `virtual=True`면 빈 결과 반환.

### 📡 WebSocket 정확도 개선

**1. 다중 체결 파싱 (`data_cnt > 1`)**
KIS spec: 한 메시지에 N개 체결 묶여 올 수 있음. v0.8.5 이전엔 첫 record만
파싱해서 두 번째 이후 체결 누락. v0.8.6부터 spec 명시 필드 개수
(국내 46, 해외 26) 단위로 분할해 모든 record `on_tick` 호출.

**2. application-level PINGPONG 처리**
KIS spec ([wikidocs/164066](https://wikidocs.net/164066)): 서버가 application
JSON `tr_id="PINGPONG"` 메시지를 보내면 클라가 echo back으로 pong 응답해야
세션 유지. v0.8.5 이전엔 무시 — KIS가 일정 시간 후 끊을 가능성.
v0.8.6부터 받은 메시지를 그대로 echo. 시세 WebSocket + 체결통보 WebSocket 모두.

### 🔍 검토 후 변경 없음 (참고)

- **국내 H0STCNT0 KRX-only**: 통합(H0UNCNT0)/NXT(H0NXCNT0)는 모의 미지원이라
  베타 단계 유지. 실전 사용자 50명+ 시점에 도입 검토.
- **SUBSCRIBE_MAX = 20**: 블로그 사례엔 41건 가능 명시되지만 KIS 공식
  spec엔 미확정. 보수적으로 20 유지. 향후 KIS Developers 공식 확인 후 조정.
- **시장가 OVRS_ORD_UNPR=0**: KIS spec 권장이나 해외 매수에 시장가
  ORD_DVSN 자체가 없음(매도만 31:MOO/33:MOC). 우리 시스템은 즉시 체결
  필요한 손절 위주라 "현재가 대체 + 지정가" 패턴이 합리적. 유지.

## 사용자 액션
1. zip 덮어쓰기 후 로컬앱 재시작.
2. **모의투자에서 "지금 한 번 실행" 1회 클릭** 정상 동작 확인.
3. cycle 로그·주문 로그에 `rt_cd=0` 응답 확인.
4. 해외 미체결 조회가 모의에서 더 이상 호출 안 됨 — 정상.

## Server 호환
모든 server 버전 호환. 변경은 로컬앱 KIS REST/WebSocket 호출만.
