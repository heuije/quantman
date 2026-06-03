"""IR 상위 구조(유니버스·진입·사이징·방향·청산·시뮬)의 **의미(semantics) 자기서술**.

catalog_spec()이 신호 블록의 *문법*(op·params)을 self-describing하듯, 이 모듈은 포지션·
시뮬 구성요소가 **무엇을 달성하는가(does)** 와 **어떤 전략 의도에 쓰는가(use_for)** 를
기계판독 가능하게 노출한다. NL→IR 컴파일러가 "목표 베타·상수 레버리지·종가 리밸런싱"
같은 전략 의도를 올바른 프리미티브(always+leverage 등)로 매핑하는 1차 근거.

값(value)은 spec.py의 Literal과 일치해야 한다(엔진이 단일 출처 — 모드 추가 시 여기 설명 추가).
"""
from __future__ import annotations


def capability_spec() -> dict:
    """포지션·시뮬 구성요소의 의미·용례 명세. 컴파일러 시스템 프롬프트의 <capabilities> 입력."""
    return {
        "universe_kind": [
            {"value": "single", "does": "종목 1개",
             "use_for": "단일 자산 매매 · 레버리지 ETF 복제 · 지수 추종"},
            {"value": "list", "does": "지정한 여러 종목 바스켓",
             "use_for": "소수 종목 고정 바스켓 (세부조건으로 2차 선별 가능)"},
            {"value": "all", "does": "데이터 보유 전체 종목",
             "use_for": "전체 유니버스 팩터/포트폴리오 (scheduled·always 진입과 함께)"},
        ],
        "screener": {
            "field": "universe.screener",
            "does": "선택 종목에 얹는 자격 필터 — 필터+횡단순위 condition. refresh로 동적/정적.",
            "use_for": "고른 종목을 거래대금·시총·밸류 등 조건으로 2차 선별. "
                       "refresh=each_rebalance(매 리밸런싱 재선별)·once_at_start(시작시점 바스켓 고정).",
        },
        "entry_mode": [
            {"value": "on_signal",
             "does": "신호(condition)가 참인 날 진입하고 exit 규칙으로 청산",
             "use_for": "이벤트/룰 기반 단발 매매 — 돌파 매수, 과매도 반등, 골든크로스 등. 단일·소수 종목."},
            {"value": "scheduled",
             "does": "rebalance 주기마다 신호 점수(score) 상위 top_n/top_pct(또는 threshold)를 보유·교체",
             "use_for": "정기 리밸런싱 팩터/포트폴리오 — 월간 모멘텀 상위 N, 분기 밸류 상위 % 등. all 유니버스 또는 세부조건과."},
            {"value": "always",
             "does": ("매일 리밸런싱 — 보유 비중을 매일 목표로 되감는다. leverage와 결합하면 "
                      "노출 = leverage × 순자산 을 매일 유지(상승 후 매수·하락 후 매도). exit 규칙은 무시."),
             "use_for": ("상수 레버리지 / 목표 베타 유지, 레버리지 ETF(2x·3x) 복제, 상시 풀투자, "
                         "변동성 타게팅 상시 보유. 신호는 '보유 마스크'로 동작(항상 참 신호면 매일 보유). "
                         "'노출/베타를 N배로 매일 맞춘다'·'종가 부근에 선물 매수/매도로 비중 조정' = 이 모드.")},
        ],
        "leverage": {
            "field": "simulation.leverage",
            "does": "목표 그로스 노출 배수 (sum|비중| = leverage). always와 결합 시 매일 목표노출로 리밸런싱 = 상수 레버리지.",
            "use_for": ("목표 베타 N배 · 레버리지 ETF. funding_cost_pct로 차입비용, "
                        "maintenance_margin_pct로 마진콜 모델. 1 초과는 백테스트 전용(모의/실전 차단)."),
        },
        "sizing_mode": [
            {"value": "equal_weight", "does": "보유 종목 동일가중",
             "use_for": "기본. 단일 종목이면 100%(×leverage)."},
            {"value": "signal_proportional", "does": "신호 점수(score)에 비례 배분",
             "use_for": "팩터 점수가 클수록 큰 비중."},
            {"value": "vol_inverse", "does": "변동성 역가중(vol_window 창)",
             "use_for": "리스크 패리티식 — 변동성 큰 종목 비중 축소."},
            {"value": "target_vol", "does": "종목별 목표 연변동성(target_vol_pct)에 맞춰 비중(레버리지 동반)",
             "use_for": "변동성 타게팅 — 각 종목을 목표 변동성으로 스케일."},
            {"value": "fixed_weight", "does": "사용자 지정 per-symbol 비중(weights)",
             "use_for": "정적 자산배분(예: 60/40)."},
            {"value": "fixed_amount", "does": "종목당 고정 금액(amount_krw)",
             "use_for": "이벤트 진입 예산 — 신호당 일정 금액 매수."},
            {"value": "pct_cash", "does": "자본 대비 %(amount_pct)",
             "use_for": "이벤트 진입 예산 — 신호당 자본의 X% 매수."},
        ],
        "direction": [
            {"value": "long", "does": "매수만", "use_for": "일반 롱 전략."},
            {"value": "short", "does": "매도(공매도)만", "use_for": "하락 베팅 · 인버스."},
            {"value": "long_short", "does": "threshold 기준 양수=롱·음수=숏 (시장중립 지향)",
             "use_for": "롱숏 팩터 · 시계열 모멘텀(TSMOM). entry.threshold로 부호 경계."},
        ],
        "exit": {
            "field": "position.exit",
            "does": "on_signal·scheduled 진입의 청산 규칙(OR 결합, 가장 먼저 닿는 것). always는 무시.",
            "knobs": {
                "take_profit": "익절(+%, 양수)", "stop_loss": "손절(-%, 음수)",
                "hold_days": "보유기간(일, ≥1)", "trail_pct": "트레일링 스탑(+%)",
                "trail_atr_mult": "ATR 배수 트레일링", "condition": "매도 신호(condition 블록)",
            },
        },
        "fill": [
            {"value": "next_open", "does": "익일 시가 체결(기본, look-ahead 방지)",
             "use_for": "일반 룰 전략."},
            {"value": "close", "does": "당일 종가 체결",
             "use_for": "'종가 부근' 체결 · 일일 리밸런싱(상수 레버리지)."},
            {"value": "typical", "does": "당일 (고+저+종)/3 (일봉 VWAP 근사)",
             "use_for": "체결가 보수적 근사."},
        ],
        "overlays": {
            "field": "position.overlays",
            "does": "전역 오버레이 — vol_target(연율 변동성 타겟%), max_drawdown_stop(낙폭 완전청산%), "
                    "max_drawdown_soft(디리스킹 시작%), max_group_pct(그룹 노출 캡, group_label 필요).",
            "use_for": "포트폴리오 리스크 제어 — 낙폭 디리스킹, 섹터 노출 상한.",
        },
        "rebalance": [
            {"value": "daily", "does": "매일 리밸런싱"},
            {"value": "weekly", "does": "매주"},
            {"value": "monthly", "does": "매월"},
            {"value": "quarterly", "does": "분기마다"},
            {"value": "annual", "does": "매년"},
            {"value": "every_n_days", "does": "N거래일마다(entry.every_n_days로 N 지정)"},
        ],
        "refill": [
            {"value": "cash", "does": "중간 청산 후 빈 슬롯을 현금으로 유지(다음 리밸런스까지)",
             "use_for": "기본 — 신호 빠지면 비중 줄임."},
            {"value": "replace", "does": "빈 슬롯을 차순위 종목으로 즉시 충원",
             "use_for": "항상 top_n 종목 수를 채워 풀투자 유지."},
        ],
        "period_split": [
            {"value": "single", "does": "단일 구간 1회 백테스트(기본)"},
            {"value": "walk_forward", "does": "워크포워드 — 학습→검증 구간을 롤링",
             "use_for": "'시간이 지나도 견고한가' 강건성 검증."},
            {"value": "oos", "does": "표본외 — 앞 구간 학습·뒤 구간 검증으로 분할"},
            {"value": "kfold", "does": "K겹 교차검증 — 여러 구간으로 갈라 교차"},
        ],
        # 펼침/분석(sweep) — axis(어떻게 나눠 볼까)와 target(무엇을 측정할까)은 직교.
        # 기본은 단일 백테스트(axis='none'·target='return'). 분석 펼침은 명시 요청 시에만.
        "sweep_axis": [
            {"value": "none", "does": "펼침 없이 단일 백테스트(기본)"},
            {"value": "parameter", "does": "param_grid의 점경로별 값 격자로 반복",
             "use_for": "'기간·비용 등을 바꿔가며 성과 비교'(민감도·최적화). 축 2개+면 데카르트곱."},
            {"value": "condition",
             "does": "1회 백테스트 결과를 label 블록으로 그룹 분할해 그룹별 성과·유의차 비교",
             "use_for": "'산업·섹터별로 어디서 잘 되나'(label=attribute), '요일·월별'(label=calendar), "
                        "'점수 구간별'(label=bucket). label 필수, target='return'."},
            {"value": "asset", "does": "assets 목록의 종목별 개별 성과",
             "use_for": "'종목마다 따로 성과를 본다'."},
            {"value": "time", "does": "이벤트 시점 기준 forward 수익 분포(event·windows)",
             "use_for": "'이벤트 발생 후 N일 수익'(이벤트 스터디). event 미지정 시 signal 사용."},
        ],
        "sweep_target": [
            {"value": "return", "does": "전략 일별수익(기본). axis로 분할·반복.",
             "use_for": "손익·성과가 답일 때. target_node 불필요."},
            {"value": "signal", "does": "임의 score 노드 *값*의 분포를 (선택)라벨별로 — 신호 자체 연구",
             "use_for": "신호 반감기·레짐별 분포. sweep.target_node(score 또는 condition) 필요."},
            {"value": "relation", "does": "factor 노드와 forward수익의 횡단 IC 시계열 — 예측력·팩터 타이밍",
             "use_for": "팩터 예측력 측정. target_node·windows 필요, universe.kind!=single."},
        ],
        "sweep_relation_kind": [
            {"value": "ic", "does": "횡단 정보계수(Information Coefficient) — 팩터값과 forward수익의 순위상관"},
        ],
        "sweep_event_basis": [
            {"value": "close", "does": "종가→종가 수익(time축 기본)"},
            {"value": "intraday", "does": "시가→종가 수익(당일 반등 포착)"},
            {"value": "excess", "does": "시장 대비 초과수익(universe.kind!=single 필요)"},
        ],
    }
