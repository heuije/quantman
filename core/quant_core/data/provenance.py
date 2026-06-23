"""데이터 계보 SSOT(single source of truth) — 각 데이터 범주의 소스·산출방법·커버리지.

챗봇이 자기 데이터 출처를 **정확히 알게**(메타인지) 시스템 프롬프트에 주입한다. 모델은
데이터 파이프라인의 ground truth를 모르면 추측한다(실측: "PER은 FnGuide" 오답 — 실제로는
전자공시). 이 표가 그 추측을 막는다. 내용은 실제 수집 코드 전수 확인(2026-06)에 근거:
  · 가격 KR=FinanceDataReader · US=yfinance · 크립토=Binance · KR선물=Investing.com+KIS
  · 기술지표=가격 파생계산(수집 아님)
  · trailing 밸류(PBR·PER·EV/EBITDA)·재무 KR=전자공시(OpenDART) · US=SEC EDGAR
  · 추정실적(forward)=FnGuide(현재 스냅샷·trailing과 별개)
  · 컨센서스(목표주가)=한경 · 수급=KRX(pykrx) · 섹터=FDR KRX-DESC · 매크로=FRED+yfinance
  · 뉴스=네이버

⚠ 큐레이션 레지스트리다 — 피드 소스가 바뀌면 여기도 함께 고친다(드리프트 방지).
"""

from __future__ import annotations

# (범주, 소스, 산출방법, 주의/커버리지). 소스는 자산군(KR/US/크립토)별로 다르면 명시.
DATA_PROVENANCE: list[dict[str, str]] = [
    {"category": "가격(OHLCV·거래량)",
     "source": "KR=FinanceDataReader · US=yfinance · 크립토=Binance · KR선물=Investing.com+KIS",
     "method": "원시 일봉 수집(정규장 종가 기준)", "note": "거래일 PIT"},
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
     "source": "한경컨센서스",
     "method": "증권사 리포트 집계(180일 신선도 standing)", "note": "KR. 소형주는 sparse"},
    {"category": "수급(기관·외국인 순매수)",
     "source": "KRX(pykrx)", "method": "일별 순매수 대금", "note": "KR 한정"},
    {"category": "섹터·업종 분류",
     "source": "FinanceDataReader KRX-DESC",
     "method": "KSIC 업종 → 테마 매핑(예 '반도체 제조업'→'반도체')",
     "note": "현행 분류만(과거 변동 미추적). US는 미분류"},
    {"category": "매크로(금리·환율·지수·VIX·신용스프레드 등)",
     "source": "FRED + yfinance",
     "method": "원시 시계열 + 파생(VIX 기간구조·구리금비율·버핏지수 등)",
     "note": "월간은 발표지연 보정 PIT"},
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
