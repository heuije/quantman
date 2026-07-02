"""데이터 수집 정책 상수 — 단일 출처(SSOT).

**대원칙 3(일관 깊이):** Core 데이터(가격 OHLCV·펀더멘털·시총·매크로·선물)는 자산군
무관하게 같은 floor에서 시작한다. 백테스트가 여러 데이터 종류를 결합할 때 사용가능
구간은 *가장 얕은* 데이터가 결정하므로(교집합), floor가 흩어져 있으면 자산군별 깊이
편차가 그대로 백테스트 비교 불공정으로 이어진다(실측: US 신규종목 2015 캡 → 2010
도달 3%뿐 vs KR 2010 백필).

이 상수를 바꾸면 전 수집 경로(fetch 기본 start·깊이 백필 floor·KRX 날짜커서 floor)가
함께 움직인다. 개별 경로에 floor 리터럴을 하드코딩하지 말 것 — 그 드리프트가 이 모듈이
없앤 결함의 뿌리였다.

Enrichment(수급·컨센서스·13F·COT 등)는 소스의 자연 floor를 따르며 이 상수에 묶지
않는다 — 소스가 못 주는 깊이를 위장하지 않는다(spec.py의 피드별 floor 필드가 실측
대비 목표를 정직 노출). 소스 자체가 CORE_FLOOR보다 얕으면(예: Binance 2017~) 그대로
받는다 — floor는 "최소 목표"지 truncate가 아니다.
"""

from __future__ import annotations

# Core 데이터 통일 시작 floor (ISO). 깊이 백필·신규 fetch 기본 start의 단일 기준.
CORE_FLOOR = "2010-01-01"

# 같은 값의 compact 표기(%Y%m%d) — KRX/pykrx 계열 API가 요구하는 포맷.
CORE_FLOOR_COMPACT = CORE_FLOOR.replace("-", "")
