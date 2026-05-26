## 주요 변경

### Phase 57 — 다종목 portfolio 백테스트 (web)
- **매수 후보 N개 동시 시뮬레이션**: 단일 cash + 종목별 position dict, 매수 신호 동시 발생 시 입력 순서 우선, 자본·동시보유한도(전역 30) 부족 시 가능한 것까지, partial close once-per-trade, equal-weight buy-and-hold benchmark
- 백테스트 결과에 종목 컬럼 추가 (종목별 거래 분리 표시)
- 호환성 게이트: 기존 1종목 백테스트 결과 그대로 (golden 15개 회귀 PASS)

### Phase 57-B — 자동선택(screener) 백테스트 (web)
- rebalance 주기(daily/weekly/monthly/every_n_days)마다 historical 평가 → 후보 동적 갱신
- mode: off (첫 평가 lock-in) / hold (빈 슬롯만 신규) / replace (탈락 매도 + 신규 매수)
- lazy aligned·mask 캐시로 4445 종목 universe 19초 응답
- 펀더멘털·52주(market_cap, per, pbr, dividend_yield 등) 사용 시 pre-flight 명시 에러 — historical 부재

### Phase 56 — 매도 룰별 sell_pct (로컬앱)
- **TP·SL·trail·ATR·hold_days 각각 sell_pct% 설정 가능** — TP partial 같은 표준 패턴 지원
- once-per-trade: 같은 룰이 한 거래에 한 번만 trigger (TP 50% 후 다시 TP trigger 안 함)
- `intraday_stop.py` tick trigger 시 룰별 sell_pct 자동 적용

### 매수 사이징 단순화 (v0.7.0-beta 반전)
- **SizingModifier 제거**: 조건별 ×배수 (Phase 47에서 추가됐던 기능). 사용처 적고 사용자 혼란 → 제거
- **분할매수(SplitBuy) 제거**: 베이스 매수액 N차 분할. 추적성·운용 복잡도 vs 효용 비교 시 제거 결정
- 매수 사이징 4지(정률·정액·균등·ATR risk)는 그대로 유지
- Strategy.amount_pct default 100 → 10 (분산 원칙·max_position_pct cap과 일치)

### 매도 UI·기능 개선
- 매도 카테고리 2종(실시간·시가) 토글 다중선택 — 명확한 mental model
- 보유기간을 매도 ConditionBuilder의 "보유기간(일)" indicator로 통합 (별도 row 제거)
- 매도 비율 라벨 단순화

### 매수 단계 progressive disclosure
- ①매수조건 → ②매수가격 → ③매수규모 → ④요약 1문장 → 매도 조건
- 각 단계 완료 시 다음 단계 노출 (사용자 학습 부담↓)
- 매수후보 모달 적용 버튼, 매수가격 tolerance 강제 입력, 매수규모 값 강제 입력

### 기타
- 한국 주식 호가단위(KRX 2023.1.30 개편) + ±30% 가격제한폭 자동 검증
- 킬스위치 UI 문구 정확화 (강제 청산 명시)
- 동시 보유 한도 설정 제거 → 전역 30 cap
- 자동 선택 리밸런싱 N일 주기 옵션
- 전략 만들기 작업 상태 localStorage persist (탭 이동·새로고침 후 복원)

## 사용자 액션
- **새 zip 압축 해제 후 기존 폴더 위에 덮어쓰기**
- KIS 토큰·DB(`~/.quant-platform/`) 그대로 유지 (호환)
- **기존 v0.7.0-beta에서 분할매수·SizingModifier를 쓰던 전략은** 빌더에서 수정 필요 (해당 UI 제거 + 매도 룰별 sell_pct로 대체)
- **paper 모드에서 새 기능 동작 우선 검증 권장**:
  - 매도 룰 sell_pct < 100 설정 시 partial 청산 → 잔여 shares 다음 trigger까지 정확히 추적
  - 다종목 백테스트 vs 자동선택 백테스트 결과 비교

## Server 호환
이 release는 server **commit 1d91edd** 이상과 호환 (Phase 57 portfolio·screener 백테스트 endpoint 포함). 이전 server에서도 단일 종목 백테스트는 정상 동작.

## 알려진 한계
- screener 백테스트는 기술지표·OHLCV·trade_value 룰만 지원. 펀더멘털(market_cap, per, pbr 등) historical 데이터 없음 — 추후 phase 별도 인프라
- 매도 룰별 sell_pct 실거래 검증은 paper 모드에서 우선 진행 권장 (자금 안전 경로)
