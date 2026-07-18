# CTCA0903R — 국내휴장일조회 [국내주식-040]

- **경로**: `GET /uapi/domestic-stock/v1/quotations/chk-holiday`
- **도메인**: 실전 `https://openapi.koreainvestment.com:9443` (모의 지원 여부 미확인 — 서버는 실전 키 사용)
- **용도**: 날짜별 영업일/거래일/개장일/결제일 구분 → **개장일(opnd_yn)로 KRX 휴장 판정**
- **KIS 권고**: "단시간 내 다수 호출 자제, 가급적 **1일 1회** 호출"

## Request

| 파라미터 | 필수 | 의미 |
|---|---|---|
| `BASS_DT` | Y | 기준일자 YYYYMMDD — 이 날짜부터의 표를 반환 |
| `CTX_AREA_FK` | N | 연속조회조건 (첫 호출 "") |
| `CTX_AREA_NK` | N | 연속조회키 (첫 호출 "") |

헤더: 표준 인증(appkey/appsecret/Bearer token) + `tr_id: CTCA0903R`, `custtype: P`.
연속조회 시 요청 헤더 `tr_cont: N` + 직전 응답의 ctx 두 값.

## Response (output[] 행)

| 필드 | 의미 |
|---|---|
| `bass_dt` | 날짜 YYYYMMDD |
| `wday_dvsn_cd` | 요일구분코드 |
| `bzdy_yn` | 영업일 여부 (은행 영업) |
| `tr_day_yn` | 거래일 여부 |
| `opnd_yn` | **개장일 여부 — 휴장 판정은 이 필드** |
| `sttl_day_yn` | 결제일 여부 |

페이지네이션: 응답 **헤더** `tr_cont` = `F`/`M`이면 다음 페이지 존재, `D`/`E`면 마지막.

## 우리 코드 위치

- `server/app/krx_holiday_source.py` — 일일 수집·볼륨 누적(`/data/calendars/krx_holidays.json`),
  회사 키 env `QP_KIS_APPKEY`/`QP_KIS_APPSECRET` (유저 자격증명과 무관).
- `server/app/calendar_cache.py::_apply_krx_overlay` — KR 세션 빌드에 권위 오버레이 + 이중신호 경보.

## ⚠ 실측 대기 (서버 키 설정 후 1회 확인)

- 페이지당 행 수·기본 반환 창 크기 (코드는 `_MAX_PAGES=12` 보수 상한)
- 과거 `BASS_DT` 허용 범위 (백필은 오늘−60일 1회 조회로 구현)
- 모의투자 도메인 지원 여부 (문서 출처: 공식 GitHub examples_llm/domestic_stock/chk_holiday, 2026-07-18 확인)
