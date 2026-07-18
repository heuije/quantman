# KIS API Endpoint Index

전체 213 endpoint 색인 (주식 132 + 선물옵션 80 + 업종/기타 1). **작업 전 `grep -i <키워드> INDEX.md`로 후보 찾기.**
상세는 `endpoints/{category}/{TR_ID}_*.md` 참조.

## 국내주식 — 업종/기타 (1 endpoint, raw 미보유 — 공식 GitHub 확인)

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`CTCA0903R`](endpoints/CTCA0903R_국내휴장일조회.md) | 국내휴장일조회 (개장일=opnd_yn·1일1회 권고·서버 휴장일 신호 수집에 사용) | 미확인 |

## 국내주식 — 기본시세 (22 endpoints, [raw](raw/국내주식_기본시세.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`FHKST01010100`](endpoints/domestic-quote/FHKST01010100_주식현재가-시세.md) | 주식현재가 시세 | ✓ |
| [`FHPST01010000`](endpoints/domestic-quote/FHPST01010000_주식현재가-시세2.md) | 주식현재가 시세2 | ✗ |
| [`FHKST01010300`](endpoints/domestic-quote/FHKST01010300_주식현재가-체결.md) | 주식현재가 체결 | ✓ |
| [`FHKST01010400`](endpoints/domestic-quote/FHKST01010400_주식현재가-일자별.md) | 주식현재가 일자별 | ✓ |
| [`FHKST01010200`](endpoints/domestic-quote/FHKST01010200_주식현재가-호가-예상체결.md) | 주식현재가 호가/예상체결 | ✓ |
| [`FHKST01010900`](endpoints/domestic-quote/FHKST01010900_주식현재가-투자자.md) | 주식현재가 투자자 | ✓ |
| [`FHKST01010600`](endpoints/domestic-quote/FHKST01010600_주식현재가-회원사.md) | 주식현재가 회원사 | ✓ |
| [`FHKST03010100`](endpoints/domestic-quote/FHKST03010100_국내주식기간별시세-일-주-월-년.md) | 국내주식기간별시세(일/주/월/년) | ✓ |
| [`FHKST03010200`](endpoints/domestic-quote/FHKST03010200_주식당일분봉조회.md) | 주식당일분봉조회 | ✓ |
| [`FHKST03010230`](endpoints/domestic-quote/FHKST03010230_주식일별분봉조회.md) | 주식일별분봉조회 | ✗ |
| [`FHPST01060000`](endpoints/domestic-quote/FHPST01060000_주식현재가-당일시간대별체결.md) | 주식현재가 당일시간대별체결 | ✓ |
| [`FHPST02320000`](endpoints/domestic-quote/FHPST02320000_주식현재가-시간외일자별주가.md) | 주식현재가 시간외일자별주가 | ✓ |
| [`FHPST02310000`](endpoints/domestic-quote/FHPST02310000_주식현재가-시간외시간별체결.md) | 주식현재가 시간외시간별체결 | ✓ |
| [`FHPST02300000`](endpoints/domestic-quote/FHPST02300000_국내주식-시간외현재가.md) | 국내주식 시간외현재가 | ✗ |
| [`FHPST02300400`](endpoints/domestic-quote/FHPST02300400_국내주식-시간외호가.md) | 국내주식 시간외호가 | ✗ |
| [`FHKST117300C0`](endpoints/domestic-quote/FHKST117300C0_국내주식-장마감-예상체결가.md) | 국내주식 장마감 예상체결가 | ✗ |
| [`FHPST02400000`](endpoints/domestic-quote/FHPST02400000_ETF-ETN-현재가.md) | ETF/ETN 현재가 | ✗ |
| [`FHKST121600C0`](endpoints/domestic-quote/FHKST121600C0_ETF-구성종목시세.md) | ETF 구성종목시세 | ✗ |
| [`FHPST02440000`](endpoints/domestic-quote/FHPST02440000_NAV-비교추이-종목.md) | NAV 비교추이(종목) | ✗ |
| [`FHPST02440200`](endpoints/domestic-quote/FHPST02440200_NAV-비교추이-일.md) | NAV 비교추이(일) | ✗ |
| [`FHPST02440100`](endpoints/domestic-quote/FHPST02440100_NAV-비교추이-분.md) | NAV 비교추이(분) | ✗ |
| [`FHPST02400200`](endpoints/domestic-quote/FHPST02400200_ETF-현재가-호가.md) | ETF 현재가 호가 | ✗ |

## 국내주식 — 실시간시세 (29 endpoints, [raw](raw/국내주식_실시간시세.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`H0STCNT0`](endpoints/domestic-realtime/H0STCNT0_국내주식-실시간체결가--KRX.md) | 국내주식 실시간체결가 (KRX) | ✓ |
| [`H0STASP0`](endpoints/domestic-realtime/H0STASP0_국내주식-실시간호가--KRX.md) | 국내주식 실시간호가 (KRX) | ✓ |
| [`H0STCNI0`](endpoints/domestic-realtime/H0STCNI0_국내주식-실시간체결통보.md) | 국내주식 실시간체결통보 | ✓ |
| [`H0STANC0`](endpoints/domestic-realtime/H0STANC0_국내주식-실시간예상체결--KRX.md) | 국내주식 실시간예상체결 (KRX) | ✗ |
| [`H0STMBC0`](endpoints/domestic-realtime/H0STMBC0_국내주식-실시간회원사--KRX.md) | 국내주식 실시간회원사 (KRX) | ✗ |
| [`H0STPGM0`](endpoints/domestic-realtime/H0STPGM0_국내주식-실시간프로그램매매--KRX.md) | 국내주식 실시간프로그램매매 (KRX) | ✗ |
| [`H0STMKO0`](endpoints/domestic-realtime/H0STMKO0_국내주식-장운영정보--KRX.md) | 국내주식 장운영정보 (KRX) | ✗ |
| [`H0STOAA0`](endpoints/domestic-realtime/H0STOAA0_국내주식-시간외-실시간호가--KRX.md) | 국내주식 시간외 실시간호가 (KRX) | ✗ |
| [`H0STOUP0`](endpoints/domestic-realtime/H0STOUP0_국내주식-시간외-실시간체결가--KRX.md) | 국내주식 시간외 실시간체결가 (KRX) | ✗ |
| [`H0STOAC0`](endpoints/domestic-realtime/H0STOAC0_국내주식-시간외-실시간예상체결--KRX.md) | 국내주식 시간외 실시간예상체결 (KRX) | ✗ |
| [`H0UPCNT0`](endpoints/domestic-realtime/H0UPCNT0_국내지수-실시간체결.md) | 국내지수 실시간체결 | ✗ |
| [`H0UPANC0`](endpoints/domestic-realtime/H0UPANC0_국내지수-실시간예상체결.md) | 국내지수 실시간예상체결 | ✗ |
| [`H0UPPGM0`](endpoints/domestic-realtime/H0UPPGM0_국내지수-실시간프로그램매매.md) | 국내지수 실시간프로그램매매 | ✗ |
| [`H0EWASP0`](endpoints/domestic-realtime/H0EWASP0_ELW-실시간호가.md) | ELW 실시간호가 | ✗ |
| [`H0EWCNT0`](endpoints/domestic-realtime/H0EWCNT0_ELW-실시간체결가.md) | ELW 실시간체결가 | ✗ |
| [`H0EWANC0`](endpoints/domestic-realtime/H0EWANC0_ELW-실시간예상체결.md) | ELW 실시간예상체결 | ✗ |
| [`H0STNAV0`](endpoints/domestic-realtime/H0STNAV0_국내ETF-NAV추이.md) | 국내ETF NAV추이 | ✗ |
| [`H0UNCNT0`](endpoints/domestic-realtime/H0UNCNT0_국내주식-실시간체결가--통합.md) | 국내주식 실시간체결가 (통합) | ✗ |
| [`H0UNASP0`](endpoints/domestic-realtime/H0UNASP0_국내주식-실시간호가--통합.md) | 국내주식 실시간호가 (통합) | ✗ |
| [`H0UNANC0`](endpoints/domestic-realtime/H0UNANC0_국내주식-실시간예상체결--통합.md) | 국내주식 실시간예상체결 (통합) | ✗ |
| [`H0UNMBC0`](endpoints/domestic-realtime/H0UNMBC0_국내주식-실시간회원사--통합.md) | 국내주식 실시간회원사 (통합) | ✗ |
| [`H0UNPGM0`](endpoints/domestic-realtime/H0UNPGM0_국내주식-실시간프로그램매매--통합.md) | 국내주식 실시간프로그램매매 (통합) | ✗ |
| [`H0UNMKO0`](endpoints/domestic-realtime/H0UNMKO0_국내주식-장운영정보--통합.md) | 국내주식 장운영정보 (통합) | ✗ |
| [`H0NXCNT0`](endpoints/domestic-realtime/H0NXCNT0_국내주식-실시간체결가--NXT.md) | 국내주식 실시간체결가 (NXT) | ✗ |
| [`H0NXASP0`](endpoints/domestic-realtime/H0NXASP0_국내주식-실시간호가--NXT.md) | 국내주식 실시간호가 (NXT) | ✗ |
| [`H0NXANC0`](endpoints/domestic-realtime/H0NXANC0_국내주식-실시간예상체결--NXT.md) | 국내주식 실시간예상체결 (NXT) | ✗ |
| [`H0NXMBC0`](endpoints/domestic-realtime/H0NXMBC0_국내주식-실시간회원사--NXT.md) | 국내주식 실시간회원사 (NXT) | ✗ |
| [`H0NXPGM0`](endpoints/domestic-realtime/H0NXPGM0_국내주식-실시간프로그램매매--NXT.md) | 국내주식 실시간프로그램매매 (NXT) | ✗ |
| [`H0NXMKO0`](endpoints/domestic-realtime/H0NXMKO0_국내주식-장운영정보--NXT.md) | 국내주식 장운영정보 (NXT) | ✗ |

## 국내주식 — 주문/계좌 (23 endpoints, [raw](raw/국내주식_주문_계좌.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`(매도) TTTC0011U (매수) TTTC0012U`](endpoints/domestic-order/(매도) TTTC0011U (매수) TTTC0012U_주식주문-현금.md) | 주식주문(현금) | ✓ |
| [`(매도) TTTC0051U (매수) TTTC0052U`](endpoints/domestic-order/(매도) TTTC0051U (매수) TTTC0052U_주식주문-신용.md) | 주식주문(신용) | ✗ |
| [`TTTC0013U`](endpoints/domestic-order/TTTC0013U_주식주문-정정취소.md) | 주식주문(정정취소) | ✓ |
| [`TTTC0084R`](endpoints/domestic-order/TTTC0084R_주식정정취소가능주문조회.md) | 주식정정취소가능주문조회 | ✗ |
| [`(3개월이내) TTTC0081R (3개월이전) CTSC9215R`](endpoints/domestic-order/(3개월이내) TTTC0081R (3개월이전) CTSC9215R_주식일별주문체결조회.md) | 주식일별주문체결조회 | ✓ |
| [`TTTC8434R`](endpoints/domestic-order/TTTC8434R_주식잔고조회.md) | 주식잔고조회 | ✓ |
| [`TTTC8908R`](endpoints/domestic-order/TTTC8908R_매수가능조회.md) | 매수가능조회 | ✓ |
| [`TTTC8408R`](endpoints/domestic-order/TTTC8408R_매도가능수량조회.md) | 매도가능수량조회 | ✗ |
| [`TTTC8909R`](endpoints/domestic-order/TTTC8909R_신용매수가능조회.md) | 신용매수가능조회 | ✗ |
| [`CTSC0008U`](endpoints/domestic-order/CTSC0008U_주식예약주문.md) | 주식예약주문 | ✗ |
| [`(예약취소) CTSC0009U (예약정정) CTSC0013U`](endpoints/domestic-order/(예약취소) CTSC0009U (예약정정) CTSC0013U_주식예약주문정정취소.md) | 주식예약주문정정취소 | ✗ |
| [`CTSC0004R`](endpoints/domestic-order/CTSC0004R_주식예약주문조회.md) | 주식예약주문조회 | ✗ |
| [`TTTC2202R`](endpoints/domestic-order/TTTC2202R_퇴직연금-체결기준잔고.md) | 퇴직연금 체결기준잔고 | ✗ |
| [`TTTC2201R(기존 KRX만 가능), TTTC2210R (KRX,NXT/SOR)`](endpoints/domestic-order/TTTC2201R-기존-KRX만-가능---TTTC2210R--KRX-NXT-SOR_퇴직연금-미체결내역.md) | 퇴직연금 미체결내역 | ✗ |
| [`TTTC0503R`](endpoints/domestic-order/TTTC0503R_퇴직연금-매수가능조회.md) | 퇴직연금 매수가능조회 | ✗ |
| [`TTTC0506R`](endpoints/domestic-order/TTTC0506R_퇴직연금-예수금조회.md) | 퇴직연금 예수금조회 | ✗ |
| [`TTTC2208R`](endpoints/domestic-order/TTTC2208R_퇴직연금-잔고조회.md) | 퇴직연금 잔고조회 | ✗ |
| [`TTTC8494R`](endpoints/domestic-order/TTTC8494R_주식잔고조회_실현손익.md) | 주식잔고조회_실현손익 | ✗ |
| [`CTRP6548R`](endpoints/domestic-order/CTRP6548R_투자계좌자산현황조회.md) | 투자계좌자산현황조회 | ✗ |
| [`TTTC8708R`](endpoints/domestic-order/TTTC8708R_기간별손익일별합산조회.md) | 기간별손익일별합산조회 | ✗ |
| [`TTTC8715R`](endpoints/domestic-order/TTTC8715R_기간별매매손익현황조회.md) | 기간별매매손익현황조회 | ✗ |
| [`TTTC0869R`](endpoints/domestic-order/TTTC0869R_주식통합증거금-현황.md) | 주식통합증거금 현황 | ✗ |
| [`CTRGA011R`](endpoints/domestic-order/CTRGA011R_기간별계좌권리현황조회.md) | 기간별계좌권리현황조회 | ✗ |

## 국내주식 — 순위분석 (22 endpoints, [raw](raw/국내주식_순위분석.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`FHPST01710000`](endpoints/domestic-ranking/FHPST01710000_거래량순위.md) | 거래량순위 | ✗ |
| [`FHPST01700000`](endpoints/domestic-ranking/FHPST01700000_국내주식-등락률-순위.md) | 국내주식 등락률 순위 | ✗ |
| [`FHPST01720000`](endpoints/domestic-ranking/FHPST01720000_국내주식-호가잔량-순위.md) | 국내주식 호가잔량 순위 | ✗ |
| [`FHPST01730000`](endpoints/domestic-ranking/FHPST01730000_국내주식-수익자산지표-순위.md) | 국내주식 수익자산지표 순위 | ✗ |
| [`FHPST01740000`](endpoints/domestic-ranking/FHPST01740000_국내주식-시가총액-상위.md) | 국내주식 시가총액 상위 | ✗ |
| [`FHPST01750000`](endpoints/domestic-ranking/FHPST01750000_국내주식-재무비율-순위.md) | 국내주식 재무비율 순위 | ✗ |
| [`FHPST01760000`](endpoints/domestic-ranking/FHPST01760000_국내주식-시간외잔량-순위.md) | 국내주식 시간외잔량 순위 | ✗ |
| [`FHPST01770000`](endpoints/domestic-ranking/FHPST01770000_국내주식-우선주-괴리율-상위.md) | 국내주식 우선주/괴리율 상위 | ✗ |
| [`FHPST01780000`](endpoints/domestic-ranking/FHPST01780000_국내주식-이격도-순위.md) | 국내주식 이격도 순위 | ✗ |
| [`FHPST01790000`](endpoints/domestic-ranking/FHPST01790000_국내주식-시장가치-순위.md) | 국내주식 시장가치 순위 | ✗ |
| [`FHPST01680000`](endpoints/domestic-ranking/FHPST01680000_국내주식-체결강도-상위.md) | 국내주식 체결강도 상위 | ✗ |
| [`FHPST01800000`](endpoints/domestic-ranking/FHPST01800000_국내주식-관심종목등록-상위.md) | 국내주식 관심종목등록 상위 | ✗ |
| [`FHPST01820000`](endpoints/domestic-ranking/FHPST01820000_국내주식-예상체결-상승-하락상위.md) | 국내주식 예상체결 상승/하락상위 | ✗ |
| [`FHPST01860000`](endpoints/domestic-ranking/FHPST01860000_국내주식-당사매매종목-상위.md) | 국내주식 당사매매종목 상위 | ✗ |
| [`FHPST01870000`](endpoints/domestic-ranking/FHPST01870000_국내주식-신고-신저근접종목-상위.md) | 국내주식 신고/신저근접종목 상위 | ✗ |
| [`HHKDB13470100`](endpoints/domestic-ranking/HHKDB13470100_국내주식-배당률-상위.md) | 국내주식 배당률 상위 | ✗ |
| [`FHKST190900C0`](endpoints/domestic-ranking/FHKST190900C0_국내주식-대량체결건수-상위.md) | 국내주식 대량체결건수 상위 | ✗ |
| [`FHKST17010000`](endpoints/domestic-ranking/FHKST17010000_국내주식-신용잔고-상위.md) | 국내주식 신용잔고 상위 | ✗ |
| [`FHPST04820000`](endpoints/domestic-ranking/FHPST04820000_국내주식-공매도-상위종목.md) | 국내주식 공매도 상위종목 | ✗ |
| [`FHPST02340000`](endpoints/domestic-ranking/FHPST02340000_국내주식-시간외등락율순위.md) | 국내주식 시간외등락율순위 | ✗ |
| [`FHPST02350000`](endpoints/domestic-ranking/FHPST02350000_국내주식-시간외거래량순위.md) | 국내주식 시간외거래량순위 | ✗ |
| [`HHMCM000100C0`](endpoints/domestic-ranking/HHMCM000100C0_HTS조회상위20종목.md) | HTS조회상위20종목 | ✗ |

## 해외주식 — 기본시세 (14 endpoints, [raw](raw/해외주식_기본시세.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`HHDFS76200200`](endpoints/overseas-quote/HHDFS76200200_해외주식-현재가상세.md) | 해외주식 현재가상세 | ✗ |
| [`HHDFS76200100`](endpoints/overseas-quote/HHDFS76200100_해외주식-현재가-호가.md) | 해외주식 현재가 호가 | ✗ |
| [`HHDFS00000300`](endpoints/overseas-quote/HHDFS00000300_해외주식-현재체결가.md) | 해외주식 현재체결가 | ✓ |
| [`HHDFS76200300`](endpoints/overseas-quote/HHDFS76200300_해외주식-체결추이.md) | 해외주식 체결추이 | ✗ |
| [`HHDFS76950200`](endpoints/overseas-quote/HHDFS76950200_해외주식분봉조회.md) | 해외주식분봉조회 | ✗ |
| [`FHKST03030200`](endpoints/overseas-quote/FHKST03030200_해외지수분봉조회.md) | 해외지수분봉조회 | ✗ |
| [`HHDFS76240000`](endpoints/overseas-quote/HHDFS76240000_해외주식-기간별시세.md) | 해외주식 기간별시세 | ✓ |
| [`FHKST03030100`](endpoints/overseas-quote/FHKST03030100_해외주식-종목-지수-환율기간별시세-일-주-월-년.md) | 해외주식 종목/지수/환율기간별시세(일/주/월/년) | ✓ |
| [`HHDFS76410000`](endpoints/overseas-quote/HHDFS76410000_해외주식조건검색.md) | 해외주식조건검색 | ✓ |
| [`CTOS5011R`](endpoints/overseas-quote/CTOS5011R_해외결제일자조회.md) | 해외결제일자조회 | ✗ |
| [`CTPF1702R`](endpoints/overseas-quote/CTPF1702R_해외주식-상품기본정보.md) | 해외주식 상품기본정보 | ✗ |
| [`HHDFS76370000`](endpoints/overseas-quote/HHDFS76370000_해외주식-업종별시세.md) | 해외주식 업종별시세 | ✗ |
| [`HHDFS76370100`](endpoints/overseas-quote/HHDFS76370100_해외주식-업종별코드조회.md) | 해외주식 업종별코드조회 | ✗ |
| [`HHDFS76220000`](endpoints/overseas-quote/HHDFS76220000_해외주식-복수종목-시세조회.md) | 해외주식 복수종목 시세조회 | ✗ |

## 해외주식 — 실시간시세 (4 endpoints, [raw](raw/해외주식_실시간시세.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`HDFSASP0`](endpoints/overseas-realtime/HDFSASP0_해외주식-실시간호가.md) | 해외주식 실시간호가 | ✗ |
| [`HDFSASP1`](endpoints/overseas-realtime/HDFSASP1_해외주식-지연호가-아시아.md) | 해외주식 지연호가(아시아) | ✗ |
| [`HDFSCNT0`](endpoints/overseas-realtime/HDFSCNT0_해외주식-실시간지연체결가.md) | 해외주식 실시간지연체결가 | ✗ |
| [`H0GSCNI0`](endpoints/overseas-realtime/H0GSCNI0_해외주식-실시간체결통보.md) | 해외주식 실시간체결통보 | ✓ |

## 해외주식 — 주문/계좌 (18 endpoints, [raw](raw/해외주식_주문_계좌.xlsx))

| TR_ID | API 명 | 모의 |
|---|---|---|
| [`(미국매수) TTTT1002U  (미국매도) TTTT1006U (아시아 국가 하단 규격서 참고)`](endpoints/overseas-order/(미국매수) TTTT1002U  (미국매도) TTTT1006U (아시아 국가 하단 규격서 참고)_해외주식-주문.md) | 해외주식 주문 | ✓ |
| [`(미국 정정·취소) TTTT1004U (아시아 국가 하단 규격서 참고)`](endpoints/overseas-order/(미국 정정·취소) TTTT1004U (아시아 국가 하단 규격서 참고)_해외주식-정정취소주문.md) | 해외주식 정정취소주문 | ✓ |
| [`(미국예약매수) TTTT3014U  (미국예약매도) TTTT3016U   (중국/홍콩/일본/베트남 예약주문) TTTS3013U`](endpoints/overseas-order/(미국 예약주문 취소접수) TTTT3017U (아시아국가 미제공)_해외주식-예약주문접수취소.md) | 해외주식 예약주문접수 | ✓ |
| [`(미국 예약주문 취소접수) TTTT3017U (아시아국가 미제공)`](endpoints/overseas-order/(미국 예약주문 취소접수) TTTT3017U (아시아국가 미제공)_해외주식-예약주문접수취소.md) | 해외주식 예약주문접수취소 | ✓ |
| [`TTTS3007R`](endpoints/overseas-order/TTTS3007R_해외주식-매수가능금액조회.md) | 해외주식 매수가능금액조회 | ✓ |
| [`TTTS3018R`](endpoints/overseas-order/TTTS3018R_해외주식-미체결내역.md) | 해외주식 미체결내역 | ✗ |
| [`TTTS3012R`](endpoints/overseas-order/TTTS3012R_해외주식-잔고.md) | 해외주식 잔고 | ✓ |
| [`TTTS3035R`](endpoints/overseas-order/TTTS3035R_해외주식-주문체결내역.md) | 해외주식 주문체결내역 | ✓ |
| [`CTRP6504R`](endpoints/overseas-order/CTRP6504R_해외주식-체결기준현재잔고.md) | 해외주식 체결기준현재잔고 | ✓ |
| [`(미국) TTTT3039R (일본/중국/홍콩/베트남) TTTS3014R`](endpoints/overseas-order/미국--TTTT3039R--일본-중국-홍콩-베트남--TTTS3014R_해외주식-예약주문조회.md) | 해외주식 예약주문조회 | ✗ |
| [`CTRP6010R`](endpoints/overseas-order/CTRP6010R_해외주식-결제기준잔고.md) | 해외주식 결제기준잔고 | ✗ |
| [`CTOS4001R`](endpoints/overseas-order/CTOS4001R_해외주식-일별거래내역.md) | 해외주식 일별거래내역 | ✗ |
| [`TTTS3039R`](endpoints/overseas-order/TTTS3039R_해외주식-기간손익.md) | 해외주식 기간손익 | ✗ |
| [`TTTC2101R`](endpoints/overseas-order/TTTC2101R_해외증거금-통화별조회.md) | 해외증거금 통화별조회 | ✗ |
| [`(주간매수) TTTS6036U (주간매도) TTTS6037U`](endpoints/overseas-order/(주간매수) TTTS6036U (주간매도) TTTS6037U_해외주식-미국주간주문.md) | 해외주식 미국주간주문 | ✗ |
| [`TTTS6038U`](endpoints/overseas-order/TTTS6038U_해외주식-미국주간정정취소.md) | 해외주식 미국주간정정취소 | ✗ |
| [`TTTS6058R`](endpoints/overseas-order/TTTS6058R_해외주식-지정가주문번호조회.md) | 해외주식 지정가주문번호조회 | ✗ |
| [`TTTS6059R`](endpoints/overseas-order/TTTS6059R_해외주식-지정가체결내역조회.md) | 해외주식 지정가체결내역조회 | ✗ |

---

# 선물옵션 (국내·해외) — 2026-06-05 추가

> raw xlsx에서 추출. detail `endpoints/*.md`는 README 규칙대로 **endpoint 사용 시 작성**(Phase 0은 색인까지).
> **한계·주의는 반드시 [GOTCHAS.md](GOTCHAS.md) 2026-06-05 entry 참조** — 특히 해외 전 API 모의 미지원·CME/SGX 유료시세·데이터 깊이. ⭐ = 백테스트 데이터용.

## 국내선물옵션 — 기본시세 (9 endpoints, [raw](raw/국내선물옵션_기본시세.xlsx))

path 접두 `/uapi/domestic-futureoption/v1`. 도메인은 주식과 동일.

| TR_ID | API 명 | 모의 | path (quotations/…) |
|---|---|---|---|
| `FHMIF10000000` | 선물옵션 시세 | ✓ | inquire-price |
| `FHMIF10010000` | 선물옵션 시세호가 | ✓ | inquire-asking-price |
| ⭐`FHKIF03020100` | 선물옵션기간별시세(일/주/월/년) | ✓ | inquire-daily-fuopchartprice |
| ⭐`FHKIF03020200` | 선물옵션 분봉조회 | ✗ | inquire-time-fuopchartprice |
| `FHPIF05110100` | 선물옵션 일중예상체결추이 | ✗ | exp-price-trend |
| `FHPIO056104C0` | 국내옵션전광판_옵션월물리스트 | ✗ | display-board-option-list |
| `FHPIF05030000` | 국내선물 기초자산 시세 | ✗ | display-board-top |
| `FHPIF05030100` | 국내옵션전광판_콜풋 | ✗ | display-board-callput |
| `FHPIF05030200` | 국내옵션전광판_선물 | ✗ | display-board-futures |

⭐ 백테스트 OHLCV: `FHKIF03020100` output2[] = `stck_bsop_date`·`futs_oprc/hgpr/lwpr/prpr`·`acml_vol` (일/주/월/년, **모의 OK**). `FHKIF03020200` = 분/초봉, `FID_PW_DATA_INCU_YN=Y`로 과거 포함.

## 국내선물옵션 — 실시간시세 (20 endpoints, [raw](raw/국내선물옵션_실시간시세.xlsx))

웹소켓. 실전 `ws://ops.koreainvestment.com:21000`, 모의 `:31000`. 체결통보만 모의(`H0IFCNI9`).

| TR_ID | API 명 | 모의 |
|---|---|---|
| `H0IFCNT0` | 지수선물 실시간체결가 | ✗ |
| `H0IFASP0` | 지수선물 실시간호가 | ✗ |
| `H0IOCNT0` | 지수옵션 실시간체결가 | ✗ |
| `H0IOASP0` | 지수옵션 실시간호가 | ✗ |
| `H0IFCNI0` | 선물옵션 실시간체결통보 | ✓ (`H0IFCNI9`) |
| `H0CFCNT0` | 상품선물 실시간체결가 | ✗ |
| `H0CFASP0` | 상품선물 실시간호가 | ✗ |
| `H0ZFCNT0` | 주식선물 실시간체결가 | ✗ |
| `H0ZFASP0` | 주식선물 실시간호가 | ✗ |
| `H0ZFANC0` | 주식선물 실시간예상체결 | ✗ |
| `H0ZOCNT0` | 주식옵션 실시간체결가 | ✗ |
| `H0ZOASP0` | 주식옵션 실시간호가 | ✗ |
| `H0ZOANC0` | 주식옵션 실시간예상체결 | ✗ |
| `H0EUASP0` | KRX야간옵션 실시간호가 | ✗ |
| `H0EUCNT0` | KRX야간옵션 실시간체결가 | ✗ |
| `H0EUANC0` | KRX야간옵션 실시간예상체결 | ✗ |
| `H0EUCNI0` ⚠ | KRX야간옵션 실시간체결통보 | ✗ |
| `H0MFASP0` | KRX야간선물 실시간호가 | ✗ |
| `H0MFCNT0` | KRX야간선물 실시간종목체결 | ✗ |
| `H0MFCNI0` ⚠ | KRX야간선물 실시간체결통보 | ✗ |

⚠ 야간 체결통보 TR_ID↔path 표기 불일치·중복(시트상 H0MFCNI0/path H0EUCNI0) — GOTCHAS 참조, 사용 전 실측.

## 국내선물옵션 — 주문/계좌 (15 endpoints, [raw](raw/국내선물옵션_주문_계좌.xlsx))

path 접두 `/uapi/domestic-futureoption/v1`. 주문은 **hashkey 헤더 불필요**(주식과 다름). 모의(`V…`)는 핵심 5종만.

| TR_ID (실전 / 모의 · 야간) | API 명 | 모의 | path |
|---|---|---|---|
| `TTTO1101U` / `VTTO1101U` · 야간 `STTN1101U` | 선물옵션 주문 (SLL_BUY_DVSN 매수/매도) | ✓ | trading/order |
| `TTTO1103U` / `VTTO1103U` · 야간 `STTN1103U` | 선물옵션 정정취소주문 | ✓ | trading/order-rvsecncl |
| `TTTO5201R` / `VTTO5201R` | 선물옵션 주문체결내역조회 | ✓ | trading/inquire-ccnl |
| `CTFO6118R` / `VTFO6118R` | 선물옵션 잔고현황 (+예수금·증거금총액) | ✓ | trading/inquire-balance |
| `TTTO5105R` / `VTTO5105R` | 선물옵션 주문가능 | ✓ | trading/inquire-psbl-order |
| `STTN5201R` (구 JTCE5005R) | (야간)주문체결내역조회 | ✗ | trading/inquire-ngt-ccnl |
| `CTFN6118R` (구 JTCE6001R) | (야간)잔고현황 | ✗ | trading/inquire-ngt-balance |
| `STTN5105R` (구 JTCE1004R) | (야간)주문가능 조회 | ✗ | trading/inquire-psbl-ngt-order |
| `CTFN7107R` (구 JTCE6003R) | (야간)증거금 상세 | ✗ | trading/ngt-margin-detail |
| `CTFO6117R` | 선물옵션 잔고정산손익내역 | ✗ | trading/inquire-balance-settlement-pl |
| `CTRP6550R` | 선물옵션 총자산현황 | ✗ | trading/inquire-deposit |
| `CTFO6159R` | 선물옵션 잔고평가손익내역 | ✗ | trading/inquire-balance-valuation-pl |
| `CTFO5139R` | 선물옵션 기준일체결내역 | ✗ | trading/inquire-ccnl-bstime |
| `CTFO6119R` | 선물옵션기간약정수수료일별 | ✗ | trading/inquire-daily-amount-fee |
| `TTTO6032R` | 선물옵션 증거금률 | ✗ | quotations/margin-rate |

## 해외선물옵션 — 기본시세 (20 endpoints, [raw](raw/해외선물옵션_기본시세.xlsx))

path 접두 `/uapi/overseas-futureoption/v1`. **전 API 모의 미지원(실전 전용)**, **CME·SGX 시세 유료**. 시세 수치는 종목마스터 `sCalcDesz` 소수점 적용 필요.

선물:
| TR_ID | API 명 | path (quotations/…) | 비고 |
|---|---|---|---|
| `HHDFC55010000` | 해외선물 종목현재가 | inquire-price | |
| ⭐`HHDFC55010100` | 해외선물 종목상세 | stock-detail | `tick_sz`·`tick_val`·`trst_mgn`·`crc_cd`·`sttl_date` — **계약사양 소스** |
| `HHDFC86000000` | 해외선물 호가 | inquire-asking-price | |
| ⭐`HHDFC55020400` | 해외선물 분봉조회 | inquire-time-futurechartprice | 120/콜, `QRY_TP=P` 페이징 |
| ⭐`HHDFC55020200` | 해외선물 체결추이(틱) | tick-ccnl | 40/콜 |
| ⭐`HHDFC55020100` | 해외선물 체결추이(일간) | daily-ccnl | |
| ⭐`HHDFC55020000` | 해외선물 체결추이(주간) | weekly-ccnl | |
| ⭐`HHDFC55020300` | 해외선물 체결추이(월간) | monthly-ccnl | |
| `HHDFC55200000` | 해외선물 상품기본정보 | search-contract-detail | 최대 32종목 계약사양 |
| `HHDDB95030000` | 해외선물 미결제추이(CFTC) | investor-unpd-trend | |
| `OTFM2229R` | 해외선물옵션 장운영시간 | market-time | |

옵션:
| TR_ID | API 명 | path (quotations/…) | 비고 |
|---|---|---|---|
| `HHDFO55010000` | 해외옵션 종목현재가 | opt-price | |
| `HHDFO55010100` | 해외옵션 종목상세 | opt-detail | |
| `HHDFO86000000` | 해외옵션 호가 | opt-asking-price | |
| `HHDFO55020400` | 해외옵션 분봉조회 | inquire-time-optchartprice | CLOSE_DATE_TIME 무시 |
| `HHDFO55020200` | 해외옵션 체결추이(틱) | opt-tick-ccnl | 40/콜 |
| `HHDFO55020100` | 해외옵션 체결추이(일간) | opt-daily-ccnl | **최근 120건만** |
| `HHDFO55020000` | 해외옵션 체결추이(주간) | opt-weekly-ccnl | **최근 120건만** |
| `HHDFO55020300` | 해외옵션 체결추이(월간) | opt-monthly-ccnl | **최근 120건만** |
| `HHDFO55200000` | 해외옵션 상품기본정보 | search-opt-detail | |

## 해외선물옵션 — 실시간시세 (4 endpoints, [raw](raw/해외선물옵션_실시간시세.xlsx))

웹소켓 `/tryitout/{TR_ID}`. `approval_key` 필요. 출력 `^` 구분 String. 모의 미지원.

| TR_ID | API 명 | tr_key |
|---|---|---|
| `HDFFF020` | 해외선물 실시간체결가 | 종목코드 |
| `HDFFF010` | 해외선물 실시간호가 | 종목코드 |
| `HDFFF1C0` | 해외선물 실시간주문내역통보 | HTSID |
| `HDFFF2C0` | 해외선물 실시간체결내역통보 | HTSID |

## 해외선물옵션 — 주문/계좌 (12 endpoints, [raw](raw/해외선물옵션_주문_계좌.xlsx))

path 접두 `/uapi/overseas-futureoption/v1/trading`. **전 API 모의 미지원**. Body key 대문자. 통화별(USD/KRW/EUR/HKD/CNY…) 조회.

| TR_ID | API 명 | path (trading/…) | 비고 |
|---|---|---|---|
| `OTFM3001U` | 해외선물옵션 주문 | order | PRIC_DVSN 1지정/2시장/3STOP/4S-L |
| `OTFM3002U` 정정 / `OTFM3003U` 취소 | 해외선물옵션 정정취소주문 | order-rvsecncl | 원주문 ORD_DT+ODNO |
| `OTFM3304R` | 해외선물옵션 주문가능조회 | inquire-psamount | 신규/청산 가능수량 |
| `OTFM1412R` | 미결제내역조회(잔고) | inquire-unpd | 평균체결가·평가손익·청산가능 |
| `OTFM3116R` | 당일주문내역조회 | inquire-ccld | |
| `OTFM3122R` | 일별 체결내역 | inquire-daily-ccld | |
| `OTFM3120R` | 일별 주문내역 | inquire-daily-order | |
| `OTFM3118R` | 기간계좌손익 일별 | inquire-period-ccld | 청산/미결제 손익 |
| `OTFM3114R` | 기간계좌거래내역 | inquire-period-trans | 입출금·결제 |
| `OTFM1411R` | 예수금현황 | inquire-deposit | 증거금·위험율·**환전요청액** |
| `OTFM3115R` | 증거금상세 | margin-detail | SPAN 증거금 |

---

## 도메인 routing

| 용도 | 모의 사용자 | 실전 사용자 |
|---|---|---|
| 주문·계좌 (`self.base`) | `openapivts.koreainvestment.com:29443` | `openapi.koreainvestment.com:9443` |
| **시세 (`self.quote_base`)** | **`openapi.koreainvestment.com:9443`** ← 항상 실전 | `openapi.koreainvestment.com:9443` |

⚠ doc상 "모의 미지원" 표시여도 quote_base가 실전 도메인이라 동작 가능한 경우 있음. GOTCHAS.md 참조.

## 빠른 사용 가이드

- **해외주식 시초가**: `HHDFS76200200` (모의·실전 둘 다 OK)
- **국내주식 시초가**: `FHKST01010100`의 `stck_oprc`
- **해외주식 현재가**: `HHDFS00000300` (last·base만, OHLC ✗)
- **해외주식 일별 OHLC**: `HHDFS76240000` (모의) / `FHKST03030100` (실전만)
- **국내주식 주문**: `TTTC0011U` (매도) / `TTTC0012U` (매수)
- **해외주식 주문**: `TTTT1002U` (미국 매수) / `TTTT1006U` (미국 매도)
- **국내선물 일봉(백테스트)**: `FHKIF03020100` output2 (모의 OK)
- **해외선물 일봉(백테스트)**: `HHDFC55020100` daily-ccnl (실전·유료시세, `QRY_TP=P` 페이징)
- **선물 계약사양(tick·승수·통화·만기)**: 해외 `HHDFC55010100`/`HHDFC55200000`; 국내는 KRX 명세(KOSPI200 ≈ 250,000원/pt — 코드에 박기 전 확인)
- **선물 주문**: 국내 `TTTO1101U`(주간)/`STTN1101U`(야간)/`VTTO1101U`(모의), 해외 `OTFM3001U`(모의 없음)