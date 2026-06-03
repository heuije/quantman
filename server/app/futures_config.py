"""선물 분석 대시보드 — 종목별 설정 레지스트리.

종목 차이(데이터키·계약사양·임계범위·라벨·매크로)를 한 곳에 모은다.
라우터(routers/futures.py)는 symbol → INSTRUMENTS[symbol] 조회만 한다.
"종목 추가 = 여기 1줄"이 성립.
"""
from __future__ import annotations

from dataclasses import dataclass

from quant_core.oil_futures import ContractSpec


@dataclass(frozen=True)
class ThresholdRange:
    """히트맵 임계 그리드 [lo, hi] 를 step 간격으로. 부동소수 누적오차 방지."""

    lo: float
    hi: float
    step: float

    def values(self) -> list[float]:
        count = int(round((self.hi - self.lo) / self.step))
        return [round(self.lo + i * self.step, 10) for i in range(count + 1)]


@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str          # 라우트 키 "oil"
    name: str            # 표시명 "원유 (WTI)"
    data_key: str        # data_fetcher 캐시 키 "원유선물"
    source: str          # latest-price source 라벨 "yahoo-cl=f"
    spec: ContractSpec   # tick/multiplier
    shorts: ThresholdRange   # 상단 임계(위로 첫 터치=매도)
    longs: ThresholdRange    # 하단 임계(아래로 첫 터치=매수)
    unit: str            # 단위 라벨 "배럴"
    eyebrow: str         # "CRUDE OIL · NYMEX"
    roll_note: str       # 롤/콘탱고 짧은 설명
    currency: str = "USD"           # 손익 표시 통화 ("USD"|"KRW") — 웹이 금액 라벨에 사용
    static_path: str | None = None  # 정적 스냅샷 CSV(server/app 상대경로). 설정 시 라이브 캐시 대신 이 파일을 읽음


# 계약명세: CME/NYMEX/COMEX 표준. 임계범위·step: 최근 다년 거래범위 기준 기본값(튜닝 가능).
INSTRUMENTS: dict[str, InstrumentConfig] = {
    "oil": InstrumentConfig(
        symbol="oil", name="원유 (WTI)", data_key="원유선물", source="yahoo-cl=f",
        spec=ContractSpec(tick=0.01, multiplier=1000),
        shorts=ThresholdRange(80, 150, 1), longs=ThresholdRange(10, 60, 1),
        unit="배럴", eyebrow="CRUDE OIL · NYMEX",
        roll_note="실물 인수도 → 만기마다 강제 롤오버",
    ),
    "nasdaq": InstrumentConfig(
        symbol="nasdaq", name="나스닥100 (NQ)", data_key="나스닥선물", source="yahoo-nq=f",
        spec=ContractSpec(tick=0.25, multiplier=20),
        shorts=ThresholdRange(16000, 24000, 250), longs=ThresholdRange(8000, 16000, 250),
        unit="지수", eyebrow="NASDAQ-100 · CME",
        roll_note="분기 만기 금융선물(현금정산)",
    ),
    "natgas": InstrumentConfig(
        symbol="natgas", name="천연가스 (NG)", data_key="천연가스선물", source="yahoo-ng=f",
        spec=ContractSpec(tick=0.001, multiplier=10000),
        shorts=ThresholdRange(4.0, 10.0, 0.10), longs=ThresholdRange(1.5, 4.0, 0.10),
        unit="MMBtu", eyebrow="NATURAL GAS · NYMEX",
        roll_note="계절성으로 롤 변동 큼",
    ),
    "gold": InstrumentConfig(
        symbol="gold", name="금 (GC)", data_key="금선물", source="yahoo-gc=f",
        spec=ContractSpec(tick=0.10, multiplier=100),
        shorts=ThresholdRange(2400, 3600, 25), longs=ThresholdRange(1600, 2400, 25),
        unit="온스", eyebrow="GOLD · COMEX",
        roll_note="일반적으로 콘탱고",
    ),
    "silver": InstrumentConfig(
        symbol="silver", name="은 (SI)", data_key="은선물(COMEX)", source="yahoo-si=f",
        spec=ContractSpec(tick=0.005, multiplier=5000),
        shorts=ThresholdRange(26, 40, 0.5), longs=ThresholdRange(12, 26, 0.5),
        unit="온스", eyebrow="SILVER · COMEX",
        roll_note="일반적으로 콘탱고",
    ),
    "bitcoin": InstrumentConfig(
        symbol="bitcoin", name="비트코인 (BTC)", data_key="비트코인선물", source="yahoo-btc=f",
        spec=ContractSpec(tick=5, multiplier=5),
        shorts=ThresholdRange(70000, 120000, 2500), longs=ThresholdRange(15000, 70000, 2500),
        unit="BTC", eyebrow="BITCOIN · CME",
        roll_note="CME 현금정산 선물",
    ),
    # 정적 스냅샷 종목 — yfinance에 KOSPI 선물 심볼이 없고 KRX 공식은 로그인 벽이라,
    # 사용자가 확보한 연속 선물 일봉 CSV를 1회 번들. 라이브가 아니라 갱신 안 됨(roll_note 명시).
    # 손익은 KRX 계약사양상 KRW(1pt=₩250,000, 틱 0.05). 임계는 전구간(과거 90%가 407 이하인 추세장).
    "kospi": InstrumentConfig(
        symbol="kospi", name="코스피200 선물", data_key="",
        static_path="data/static/kospi200_futures.csv",
        source="investing.com-snapshot", currency="KRW",
        spec=ContractSpec(tick=0.05, multiplier=250000),
        shorts=ThresholdRange(250, 1450, 25), longs=ThresholdRange(200, 1450, 25),
        unit="지수", eyebrow="KOSPI 200 FUTURES · KRX (정적 스냅샷)",
        roll_note="정적 스냅샷 2026-06-02 기준 · 갱신 안 됨 · 과거 90%가 407 이하인 추세장 · 손익 KRW",
    ),
}
