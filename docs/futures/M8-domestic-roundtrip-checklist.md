# M8 — 국내선물(KOSPI200) 라이브 라운드트립 체크리스트

자동매매 #4 계층(선물)을 라이브로 켜기 전, **국내선물 모의계좌로 주문 라운드트립을 실증**하는
단계. 이 검증이 통과해야 **M1a(선물 라이브 게이트 개방)** 를 안전하게 진행할 수 있다
(미검증 라이브 경로를 프로덕션에 노출하지 않는다 — 4원칙 "검증된 해결책만").

> 진단 스크립트: `local/futures_preflight.py` (읽기전용 + `--order` 라운드트립). 출력의
> `[OK]/[!]` 를 아래 항목과 대조한다. **장중(09:00~15:45)** 에 실행해야 체결된다.

## 사전 준비
- [ ] 한국투자증권 **선물옵션 모의계좌**(예: `60044290`) + KIS Developers 모의 앱키 발급
- [ ] 선물 자격증명 등록(keyring, 로컬 PC 전용 — 서버 미유입):
  ```bash
  python -c "from localapp.secrets_store import save_kis_futures; \
             save_kis_futures('APPKEY','APPSECRET','60044290-03', virtual=True)"
  ```
- [ ] `cd platform/local`

## 읽기전용 점검 — `python futures_preflight.py`
- [ ] **1) OAuth 토큰** 발급 OK (모의 도메인 `openapivts...:29443`, virtual=True 확인)
- [ ] **2) front-month 해석** — `코스피200선물 → A0xxxx (만기 YYYY-MM-DD)`. 만기가 2번째 목요일인지 상식 점검
- [ ] **3) 잔고/증거금** 조회 OK — 주문가능현금·증거금합계 출력(컬럼형 VTFO6118R output2)
- [ ] **4) 시세** 조회 OK — front-month 현재가 > 0 (장중)
- [ ] **5) dry-run 지정가 라운딩** — limit이 **0.05 틱 그리드**(예: 353.45)로 떨어지는지(C3)

## 라운드트립 — `python futures_preflight.py --order` (장중)
- [ ] **매수 발주** — `success=True`, order_no 발급(zero-padded). 거부 시 메시지(증거금·장시간·계약코드) 확인
- [ ] **체결조회** — `status=filled`, 체결 1계약, 체결가 출력(VTTO5201R inquire-ccnl output1)
- [ ] **청산(매도) 발주** — `success=True`. 잔고에서 포지션 해소 확인(재실행 시 보유 0)
- [ ] 미체결이면: 지정가가 호가 밖일 수 있음 → 재시도, 또는 KIS 마감 자동취소 확인

## 4계층 정합 확인(라운드트립 후)
- [ ] 발주된 **계약코드**가 front-month와 일치(A0xxxx)
- [ ] 체결가·정산이 **승수(250,000)** 반영 의도와 맞는지(웹 표시는 M9에서 교정)
- [ ] reconcile이 선물 포지션을 **고아 오삭제하지 않음**(M7 — 잔고 병합·정규화). 모의 cycle 1회 후
      ledger·KIS 잔고 일치 확인

## 통과 후 다음(M1a — 별도 작업)
- [ ] `server/app/symbols.py::tradable_symbols()`(또는 `_assert_live_tradable`)에 **KOSPI200 선물만** 추가
      → 선물 전략 라이브 승격 허용. (해외는 M10 실거래 검증 후 별도)
- [ ] M9: 웹 평가금액 승수 교정 + 로컬앱 릴리즈

## 주의
- 모의계좌라 실제 자금이 아니다. 단, **실거래 계좌로는 이 스크립트를 돌리지 말 것**
  (preflight는 `broker.virtual` 확인 후 모의만 진행하도록 가드).
- 국내선물 모의는 지원되나 **해외선물은 모의 미지원** → 해외는 M10(사용자 첫 실거래)로만 검증.
