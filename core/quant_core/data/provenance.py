"""데이터 계보 SSOT(single source of truth) — 각 데이터 범주의 소스·산출방법·커버리지.

챗봇이 자기 데이터 출처를 **정확히 알게**(메타인지) 시스템 프롬프트에 주입한다. 모델은
데이터 파이프라인의 ground truth를 모르면 추측한다(실측: "PER은 FnGuide" 오답 — 실제로는
전자공시). 이 표가 그 추측을 막는다. 내용은 실제 수집 코드 전수 확인(2026-06)에 근거:
  · 가격 KR=FinanceDataReader · US=yfinance · 크립토=Binance · KR선물=Investing.com+KIS
  · 기술지표=가격 파생계산(수집 아님)
  · trailing 밸류(PBR·PER·EV/EBITDA)·재무 KR=전자공시(OpenDART) · US=SEC EDGAR
  · 추정실적(forward)=FnGuide(현재 스냅샷·trailing과 별개)
  · 컨센서스(목표주가)=네이버 증권 리포트(reports_kr) · 수급=KRX(pykrx) · 섹터=FDR KRX-DESC · 매크로=FRED+yfinance
  · COT(투기순포지션·미결제약정 US선물)=CFTC Socrata · 실적캘린더(발표일·EPS)=yfinance on-demand
  · 뉴스=네이버

⚠ 큐레이션 레지스트리다 — 피드 소스가 바뀌면 여기도 함께 고친다(드리프트 방지).
"""

from __future__ import annotations

# (범주, 소스, 산출방법, 주의/커버리지). 소스는 자산군(KR/US/크립토)별로 다르면 명시.
DATA_PROVENANCE: list[dict[str, str]] = [
    {"category": "가격(OHLCV·거래량)",
     "source": "KR=FinanceDataReader · US=yfinance · 크립토=Binance · KR선물=공식 KRX Open API(만기물 패널→연속물)",
     "method": "원시 일봉 수집(정규장 종가 기준). KR선물은 KRX fut_bydd_trd 만기물 패널서 연속물 파생(S4)",
     "note": "거래일 PIT"},
    {"category": "기술지표(이동평균·RSI·모멘텀·변동성·ATR 등)",
     "source": "— (수집 아님·파생계산)",
     "method": "가격 OHLCV에서 계산(순수 함수)", "note": ""},
    {"category": "밸류에이션(PBR·PER·EV/EBITDA)·재무(마진·ROIC·부채·시총)",
     "source": "KR=전자공시(OpenDART) · US=SEC EDGAR",
     "method": "분기 재무제표 → TTM(최근 4분기 합) · 밸류=종가÷펀더(예 PER=종가÷주당 TTM순익)",
     "note": "공시 접수일 PIT. **FnGuide 아님**. KR 과거 이력은 백필 진행 중(점진 확장)"},
    {"category": "추정실적(forward 추정 EPS·매출·영업이익·forward PER·성장률)",
     "source": "FnGuide",
     "method": "애널리스트 컨센서스 현재 스냅샷(연 5년 확정 + 3년 추정E)",
     "note": "위 trailing 밸류와 **별개 소스·별개 용도**. 과거 추정 아카이브 없음(백테스트 부적합)"},
    {"category": "애널 컨센서스(목표주가·투자의견·상승여력·괴리율)",
     "source": "네이버 증권 리포트(reports_kr 집계)",
     "method": "증권사 리포트 집계(180일 신선도 standing)",
     "note": "KR 한정(US 미제공). 소형주는 sparse"},
    {"category": "수급(기관·외국인 순매수)",
     "source": "KRX(pykrx)", "method": "일별 순매수 대금", "note": "KR 한정"},
    {"category": "KR 선물·ETF 투자자별 수급(외국인·기관 순매수)",
     "source": "KRX 정보데이터시스템 MDC(로그인 getJsonData)",
     "method": "일별추이 원시 시계열(파생 13102·ETF 04802 — 일별 순매수 대금·원). "
               "매크로형 심볼 6종(예 '코스피200선물외국인순매수'·'KRETF기관순매수')",
     "note": "KR. KRX_ID/PW 필요·미설정 시 비활성. 개별 주식 수급(위 pykrx 종목별)과 별개 — "
             "선물은 상품 단위·ETF는 시장 전체 집계. 크로스에셋 신호로 참조 가능"},
    {"category": "공매도 잔고(잔고비중 short_balance_ratio)",
     "source": "KRX(pykrx)", "method": "일별 잔고비중(상장주식수 대비 %) — 진짜 short interest",
     "note": "KR 한정. US 공매도 *잔고*는 미보유(아래 거래량 비중과 별개)"},
    {"category": "공매도 거래량(short_volume_ratio)",
     "source": "FINRA Reg SHO daily",
     "method": "일별 off-exchange 공매도 거래량 비중(%) — 잔고 아님",
     "note": "US 한정·2018-08~"},
    {"category": "기관 13F 보유(보유기관수·주식수·전분기 증감)",
     "source": "SEC Form 13F Data Sets",
     "method": "전 신고자 합산 분기 시계열(as_of=분기말+45일 PIT)",
     "note": "US 한정·2013Q2~. 분기 데이터라 급변 신호 부적합"},
    {"category": "섹터·업종 분류",
     "source": "FinanceDataReader KRX-DESC",
     "method": "KSIC 업종 → 테마 매핑(예 '반도체 제조업'→'반도체')",
     "note": "현행 분류만(과거 변동 미추적). US는 미분류"},
    {"category": "매크로(금리·환율·지수·VIX·신용스프레드 등)",
     "source": "FRED + yfinance",
     "method": "원시 시계열 + 파생(VIX 기간구조·구리금비율·버핏지수 등)",
     "note": "월간은 발표지연 보정 PIT"},
    {"category": "KR 시장지표(V-KOSPI·옵션풋콜비율·KRX채권지수·국고채3/10년·선물 미결제약정·ETF AUM/순자금유입)",
     "source": "공식 KRX Open API(data-dbg.krx.co.kr)",
     "method": "원시 일별 시계열(매크로형 브로드캐스트 심볼)",
     "note": "KR. AUTH_KEY 필요·미설정 시 비활성. 크로스에셋 신호로 참조 가능"},
    {"category": "COT(투기순포지션·미결제약정, US 선물)",
     "source": "CFTC 공식 Socrata API",
     "method": "주간 포지셔닝(비상업=투기자 롱−숏) + 미결제약정. 매크로형 심볼(예 '원유선물투기순포지션'·'금선물미결제약정')",
     "note": "US 선물 8시장(WTI·천연가스·금·은·구리·나스닥·S&P500·비트코인)·1986~·화 마감 금 공개(PIT). inspect/describe로 조회·크로스에셋 신호 참조"},
    {"category": "실적캘린더(발표일·추정/확정 EPS·서프라이즈)",
     "source": "yfinance",
     "method": "종목별 on-demand 조회 — 과거 확정 발표일+EPS 실적+서프라이즈, 미래 예정일",
     "note": "US·KR. describe 리포트에 facet 포함('실적 언제·다음 실적'). 현시점 스냅샷(과거 예정일 아카이브 없음)"},
    {"category": "뉴스(헤드라인)",
     "source": "네이버 뉴스 API",
     "method": "종목명 검색 최신순(on-demand)", "note": "현시점 참고용"},
]


def data_provenance() -> list[dict[str, str]]:
    """데이터 계보 레지스트리(구조화). UI·진단·프롬프트 공용."""
    return DATA_PROVENANCE


def provenance_for_prompt() -> str:
    """챗 시스템 프롬프트용 compact 표 — 챗봇이 데이터 출처·산출법·커버리지를 정확히 답하게."""
    out = []
    for p in DATA_PROVENANCE:
        line = f"- {p['category']}: **{p['source']}**"
        if p["method"]:
            line += f" — {p['method']}"
        if p["note"]:
            line += f" ({p['note']})"
        out.append(line)
    return "\n".join(out)
