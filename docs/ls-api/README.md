# LS API Knowledge Base

LS증권(구 이베스트투자증권) OpenAPI 호출·결함 진단·새 TR 사용 시 **작업 전 반드시 참조**.
시행착오를 코드에 반복하지 않기 위한 single source of truth.

> **현재 상태:** 초안(A2). 키 미발급 상태에서 공개 문서·커뮤니티 wrapper 소스로 구축.
> 🟢 = 공개 소스로 확인된 필드. ⚠️ = 키 발급 후 라이브 확정 필요.

---

## LS증권 OpenAPI 개요

### 포털 및 기본 정보

| 항목 | 값 |
|---|---|
| 포털 | `https://openapi.ls-sec.co.kr` |
| REST Base URL | `https://openapi.ls-sec.co.kr:8080` |
| WebSocket URL | `wss://openapi.ls-sec.co.kr:9443/websocket` |
| 인증 방식 | OAuth2 client_credentials |
| 토큰 엔드포인트 | `POST /oauth2/token` |
| 토큰 유효기간 | **신청일로부터 익일(다음날) 07:00 KST까지** |

### OAuth2 토큰 발급

**Request** (`POST https://openapi.ls-sec.co.kr:8080/oauth2/token`):

```
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&appkey=<YOUR_APP_KEY>
&appsecretkey=<YOUR_APP_SECRET>
&scope=oob
```

**Response** (JSON):
```json
{
  "access_token": "...",
  "token_type": "Bearer",
  "expires_in": <초단위 잔여시간>
}
```

- 토큰 만료 = 익일 07:00 KST. `expires_in` 값을 파싱해서 갱신 시점 계산.
- ⚠️ `token_type` 필드 정확 값 미검증.

### api-id(TR) 기반 요청 구조

LS OpenAPI는 KIS(경로 기반)와 달리 **tr_cd 헤더**로 TR을 구분한다:

```python
header = {
    "content-type": "application/json; charset=utf-8",
    "authorization": f"Bearer {ACCESS_TOKEN}",
    "tr_cd": "CSPAT00601",   # TR 코드
    "tr_cont": "N",          # 연속 조회: N=초회, Y=연속
    "tr_cont_key": "",       # 연속 조회 키 (연속 시 이전 응답의 값 사용)
}
body = {
    "CSPAT00601InBlock1": {  # tr_cd + "InBlock1"
        "IsuNo": "A005930",
        ...
    }
}
```

**응답 Envelope**:
```json
{
  "rsp_cd": "00000",       // 성공 코드 (⚠️ 값은 howto-sample 확인, 공식 docs 불일치 가능성 있음)
  "rsp_msg": "정상적으로 처리되었습니다",
  "CSPAT00601OutBlock1": { ... },  // tr_cd + "OutBlock1" (단일 객체)
  "CSPAT00601OutBlock2": { ... }   // tr_cd + "OutBlock2" (주문 결과)
}
```

### 모의/실전 키 라우팅

**LS는 KIS와 달리 단일 도메인**을 쓴다 — 모의/실전 모두 `openapi.ls-sec.co.kr:8080`.
환경은 **appkey/appsecretkey 자체**가 결정한다(모의용 키 따로 발급).

> ⚠️ 단일 도메인 동작 확인 상태: 커뮤니티 wrapper(`ebest` 패키지)에서 `api.is_simulation`
> 로 서버 구분하는 것이 확인됨. 실전 도메인 별도 존재 여부는 키 발급 후 확정.

### 응답 코드 범례

| rsp_cd | 의미 |
|---|---|
| `00000` | 정상 (⚠️ 가정 — howto-sample 예시에서 확인, 공식 문서 직접 미확인) |
| 그 외 | 오류 — rsp_msg 참고 |

---

## 구조

```
docs/ls-api/
├── README.md                # 이 파일 — 개요·OAuth·TR 모델
├── INDEX.md                 # ⭐ 전체 TR 한 줄 색인 (grep용)
├── GOTCHAS.md               # ⭐ 알려진 함정 — 공식 docs와 다른 동작·가정
├── CHANGELOG.md             # 발견 시계열
└── endpoints/               # TR별 상세
    ├── CSPAT00601_현물신규주문.md
    ├── CSPAT00701_현물정정주문.md
    ├── CSPAT00801_현물취소주문.md
    ├── t0424_주식잔고조회2.md
    ├── t0425_주식미체결조회.md
    └── t1102_주식현재가.md
```

## 사용 흐름 — Claude·개발자 모두

1. **INDEX.md에서 TR 후보 찾기** — `grep -i "잔고" INDEX.md`
2. **`endpoints/{tr_cd}_*.md` 읽기** — request/response/모의실전/한계
3. **GOTCHAS.md 한 번 훑기** — 알려진 함정
4. 필요 시 공개 소스 직접 확인 (아래 출처 섹션)

## 작업 중 발견 시 즉시 기록

| 발견 종류 | 기록 위치 |
|---|---|
| 새 TR 사용 | `endpoints/{tr_cd}_*.md` 새 파일 |
| 공식 doc·실측 불일치 | `GOTCHAS.md` 상단 (최신순) |
| 릴리즈 fix | `CHANGELOG.md` entry |
| 우리 코드 사용 위치 | endpoint .md의 `우리 코드 위치` |

## 검증 상태 범례

| 아이콘 | 의미 |
|---|---|
| 🟢 확인 | 공개 공식 문서 또는 커뮤니티 wrapper 소스에서 직접 확인된 값 |
| ⚠️ 미검증 | 추론·간접 확인. 키 발급 후 라이브 실측으로 확정 필요 |
| ⚠️ 가정 | 구조 유추 또는 xingAPI legacy 기반 추정. 반드시 실측 필요 |

---

## 출처 (A2 조사 시 접근한 URL)

| 구분 | URL | 접근 결과 |
|---|---|---|
| 🟢 LS 공식 샘플 페이지 | `https://openapi.ls-sec.co.kr/howto-sample` | 접근 성공. t1301·t1101·t0424·CSPAQ12300·NWS 필드 확인 |
| 🟢 LS 공식 포털 메뉴 | `https://openapi.ls-sec.co.kr/apiservice` | 접근 성공. 카테고리 목록 확인(TR 상세는 로그인 게이트) |
| 🟢 커뮤니티 샘플 repo | `https://github.com/teranum/ls-openapi-samples` | 접근 성공. Python 샘플 `14. 주식 잔고-미체결-주문.py` 등 직독 |
| 🟢 커뮤니티 종합 래퍼 | `https://github.com/xorrhks0216/LsApiHelper` | OAuth2 + **364 TR 전체 카탈로그** + 34 카테고리 REST 래퍼 + WebSocket. **Phase C 필드 대조·Phase 3 WS 시 1차 교차참조** |
| 🟢 토큰 발급 가이드 | `https://wikidocs.net/230259` (올바른 투자 로봇) | OAuth 토큰 발급·로그인 단계별 한국어 가이드 |
| 🟢 xingAPI res 파일 | `https://github.com/ermaker/xingAPI/blob/master/ext/xingAPI/Res/CSPAT00600.res` | 접근 성공. CSPAT00600(CSPAT00601 전신) InBlock/OutBlock 필드 확인 |
| ⚠️ LS 공식 API 상세 | `https://openapi.ls-sec.co.kr/apiservice?api_id=...` | 카테고리 메뉴만 표시. TR 상세는 로그인 필요 |
| ⚠️ 토큰 엔드포인트 직접 | `https://openapi.ls-sec.co.kr/oauth2/token` | 404 (POST만 허용, GET은 없음 — 정상) |

---

## 4원칙 측면

이 KB는 **PR-4 (검증 누락) 예방** 도구. 키 미발급 상태라 필드 수준은 ⚠️ 표시가 많다 —
이것이 정직한 현재 상태다. B6(LsBroker 구현) 시 ⚠️ 필드는 실측 후 🟢로 전환하고
GOTCHAS에 실측 결과를 기록한다.
