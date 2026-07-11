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
            {"value": "portfolio", "does": "내 보유 종목 집합(진단 대상). universe.weights로 비중(없으면 동일가중)",
             "use_for": "포트폴리오 진단 — 집중도(HHI)·섹터 노출·가중 밸류·포트 변동성. query=describe와 함께."},
        ],
        "screener": {
            "field": "universe.screener",
            "does": "선택 종목에 얹는 자격 필터 — 필터+횡단순위 condition. refresh로 동적/정적.",
            "use_for": "고른 종목을 거래대금·시총·밸류 등 조건으로 2차 선별. "
                       "refresh=each_rebalance(매 리밸런싱 재선별)·once_at_start(시작시점 바스켓 고정). "
                       "시장(거래소) 분리('코스닥 종목만'·'나스닥만')는 is_in(attribute(\"Market\")) — "
                       "라벨은 KOSPI·KOSDAQ·NASDAQ·NYSE 4값(개별 주식 전용, ETF·지수 제외).",
        },
        "entry_mode": [
            {"value": "on_signal",
             "does": "신호(condition)가 참인 날 진입하고 exit 규칙으로 청산",
             "use_for": "이벤트/룰 기반 단발 매매 — 돌파 매수, 과매도 반등, 골든크로스 등. 단일·소수 종목."},
            {"value": "scheduled",
             "does": "rebalance 주기마다 신호로 종목 선택·교체. score 신호는 상위 top_n/top_pct(또는 threshold) 선택; "
                     "condition 신호는 참인 종목 전부 보유(top_n 무시 — boolean이라 랭킹 불가).",
             "use_for": "정기 리밸런싱 팩터/포트폴리오 — '월간 모멘텀 상위 N'(score), 'RSI<30 전부 보유'(condition). "
                        "all 유니버스 또는 세부조건과. ⚠ 상위 N개만 원하면 신호를 score(랭킹 가능)로 짜야 함."},
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
                        "maintenance_margin_pct로 마진콜 모델. 1 초과는 백테스트 전용(모의/실전 차단). "
                        "⚠ scheduled·always 전용 — on_signal(이벤트)은 leverage를 무시한다(sizing_note 참조). "
                        "선물은 증거금으로 내재 레버리지가 이미 있어 leverage=1이 기본 — scheduled·always에서 "
                        "명목 노출을 더 키울 때만 >1(on_signal 선물은 증거금 내재 레버리지만 적용)."),
        },
        "sizing_mode": [
            {"value": "equal_weight", "does": "보유 종목 동일가중(scheduled·always 경로). 단일 종목이면 100%.",
             "use_for": "기본."},
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
        "sizing_note": ("⚠ 사이징 모드(equal_weight·signal_proportional·vol_inverse·target_vol·fixed_weight)는 "
                        "scheduled·always 경로에서만 실효. on_signal(이벤트) 다종목은 모드와 무관하게 종목당 "
                        "amount_pct(또는 fixed_amount의 amount_krw) 예산으로 진입한다. leverage도 scheduled·always 전용."),
        # ── 선물(futures) — 일부 심볼은 선물 상품. 엔진이 카탈로그로 자동 인식(IR에 자산클래스 표시 불필요). ──
        "instruments": {
            "field": "universe.symbols",
            "does": ("심볼 중 일부는 선물 상품 — 카탈로그가 승수·증거금·만기·통화를 안다(엔진 자동 인식, "
                     "IR에 자산클래스 표시 불필요). 선물 심볼: 코스피200선물·원유선물·천연가스선물·금선물·"
                     "은선물(COMEX)·나스닥선물·비트코인선물. 주식처럼 universe.symbols에 이름만 넣는다."),
            "use_for": ("선물 = 내재 레버리지·증거금 보유 포지션. 단발 디렉셔널(single+on_signal): 돌파/추세 "
                        "진입 후 보유, take_profit/stop_loss(%)·hold_days로 청산 — 보유형이라 일일 리밸런싱 "
                        "vol drag 없음. 추세추종/팩터: always(보유마스크)·scheduled. 숏은 scheduled+long_short "
                        "(on_signal은 롱 전용). 만기 롤·연속물 조정은 KOSPI200 등 만기물 패널 보유 선물에 "
                        "적용(기본 at_expiry·roll_method로 조절). 통화환산은 미적용(단일통화 가정). 신호·청산은 "
                        "주식과 동일(가격% 기준). 자본=증거금·손익=가격변화×승수×계약수(엔진 처리)."),
        },
        # 선물 연속물 구성 — 선물 심볼에만 적용. 미지정이면 상품 카탈로그 기본값(보통 명시 불필요).
        "roll_method": [
            {"value": "at_expiry", "does": "만기일까지 근월 보유 후 롤(무가공 기본 — 만기 정산 꼬리 포함)"},
            {"value": "days_before_5", "does": "만기 5영업일 전 근월→차월 롤(왜곡된 만기 꼬리 회피)"},
            {"value": "days_before_1", "does": "만기 1영업일 전 롤(만기 직전까지 근월 보유)"},
            {"value": "volume_cross", "does": "거래량이 차월물로 역전될 때 롤(유동성 추종)"},
            {"value": "oi_cross", "does": "미결제약정이 차월물로 역전될 때 롤"},
        ],
        "series_adjust": [
            {"value": "none", "does": "원본 이어붙임(롤 시점 가격 갭 유지 — 무가공)"},
            {"value": "back_adjust", "does": "과거를 차감 조정(가격차 보존)"},
            {"value": "ratio", "does": "비율 조정(수익률 정확 보존·양수 보존)"},
        ],
        "account_currency": [
            {"value": "KRW", "does": "원화 기준 손익(국내선물 — 무환산)"},
            {"value": "USD", "does": "달러 기준(해외선물). ⚠ 현재 엔진은 환율 환산 미구현 — 단일통화 백테스트만 정합."},
        ],
        # ⚠ 정직성 — roll_method·series_adjust는 만기물 패널 보유 선물(KOSPI200)에 적용된다(S4/E2).
        # roll_cost_pct는 패널의 실제 베이시스를 쓰므로 무시(데이터 우선). account_currency(FX)는
        # 아직 미적용(환율 환산 미구현). 임의로 채워 넣지 말고 사용자 명시 요청 시만 설정한다.
        "futures_continuous_note": ("roll_method·series_adjust는 만기물 패널 보유 선물(KOSPI200)에 "
                                    "적용된다(기본 at_expiry+none). roll_cost_pct는 실제 베이시스 우선이라 "
                                    "무시. account_currency(FX 환산)는 아직 미적용(단일통화 백테스트만 정합). "
                                    "임의 추가 말고 사용자 명시 요청 시만 설정."),
        "direction": [
            {"value": "long", "does": "매수만", "use_for": "일반 롱 전략 · 선물 롱."},
            {"value": "short", "does": "매도(공매도)만", "use_for": "하락 베팅 · 인버스 · 선물 숏(차입 불필요, 대칭)."},
            {"value": "long_short", "does": "threshold 기준 양수=롱·음수=숏 (시장중립 지향)",
             "use_for": "롱숏 팩터 · 시계열 모멘텀(TSMOM) · 선물 추세추종. entry.threshold로 부호 경계."},
        ],
        "exit": {
            "field": "position.exit",
            "does": "on_signal·scheduled 진입의 청산 규칙(OR 결합, 가장 먼저 닿는 것). always는 무시.",
            "knobs": {
                "take_profit": "익절(+%, 양수)", "stop_loss": "손절(-%, 음수)",
                "hold_days": "보유기간(거래일, ≥0; 0=당일 종가 청산)", "trail_pct": "트레일링 스탑(+%)",
                "trail_atr_mult": "ATR 배수 트레일링", "condition": "매도 신호(condition 블록)",
            },
        },
        "fill": [
            {"value": "next_open", "does": "익일 시가 체결(기본, look-ahead 방지)",
             "use_for": "일반 룰 전략."},
            {"value": "close", "does": "당일 종가 체결",
             "use_for": "'종가 부근' 체결 · 일일 리밸런싱(상수 레버리지)."},
            {"value": "typical", "does": "당일 (고+저+종)/3 (일봉 VWAP 근사) — on_signal(이벤트) 경로 전용",
             "use_for": "체결가 보수적 근사. ⚠ scheduled·always 경로에선 미적용(종가 체결)."},
        ],
        "overlays": {
            "field": "position.overlays",
            "does": "전역 오버레이 — vol_target(연율 변동성 타겟%), turnover_damp(비중변동 억제 임계), "
                    "max_drawdown_stop(낙폭 완전청산%), max_drawdown_soft(디리스킹 시작%), "
                    "max_group_pct(그룹 노출 캡, group_label 필요).",
            "use_for": "포트폴리오 리스크 제어 — 낙폭 디리스킹, 턴오버 억제, "
                       "섹터 노출 상한(group_label=attribute(Sector)+max_group_pct).",
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
        # 질문(query) — '무엇을 묻는가'의 동사. 기본은 simulate(손익 백테스트).
        # describe·relate는 분석 질문(신호 분포·예측력)으로, study.target_node 등 분석 입력을 동반.
        "query": [
            {"value": "simulate", "does": "전략 모의매매(손익)",
             "use_for": "백테스트 — 기본"},
            {"value": "select",
             "does": "as-of 스냅샷에서 score를 횡단 랭크해 상위 종목을 선별(시계열 시뮬 없음)",
             "use_for": "저평가주·고배당주 등 '조건 맞는 상위 N개 종목' 스크리닝. "
                        "signal=랭킹 score(예: 낮은 PBR), universe.screener로 섹터·자격 필터, "
                        "select.top_n/top_pct·descending·display(근거 지표)."},
            {"value": "describe", "does": ("살펴보기 — 대상에 따라: 단일종목(universe.kind=single)=가격·수익·"
                                           "리스크·밸류·섹터 360 리포트; 포트폴리오(kind=portfolio)=집중·섹터노출·"
                                           "리스크 진단; 종목군(kind=all/list)=임의 score 노드 값의 분포·요약(study.target_node)"),
             "use_for": "'이 종목 어때'(single)·'내 포트폴리오 진단'(portfolio)·신호 분포 연구(all/list+target_node)."},
            {"value": "relate", "does": "factor↔forward수익 횡단 IC(또는 event 지정 시 이벤트 스터디)",
             "use_for": "예측력·이벤트 반응. IC=study.target_node·windows·universe.kind!=single; "
                        "이벤트 스터디=study.event·windows. (종목 간 상관행렬=relation_kind=correlation.)"},
            {"value": "prescribe",
             "does": "포트폴리오 비중 최적화 — 위험기반(최소분산·리스크패리티·동일가중)+최대샤프 동시 산출",
             "use_for": "'포트폴리오 비중 추천'·'어떻게 배분'. universe.kind=list(종목 2+), "
                        "prescribe.max_weight(집중 상한)·window(추정기간). 결과=비중 트리맵+포트 지표."},
            {"value": "breadth",
             "does": "시장 폭(breadth) — 종목군의 등락 비율·MA 상회 비율·섹터 분산(최신 바)",
             "use_for": "'시장/코스피 어떤가·왜 빠지나'(what). universe.kind=all/list. 상승하락 수·평균수익·"
                        "20/60일선 상회·상위하위·섹터별. why(거시·뉴스)는 사이드카·해석 보강."},
            {"value": "rotation",
             "does": "섹터 순환매 — 섹터(행)×최근 월(열) 평균수익률 히트맵(자금이 어느 업종으로 도는가)",
             "use_for": "'순환매·섹터 로테이션·업종 순환·어느 업종으로 자금이 도나'. universe.kind=all/list. "
                        "월별 섹터 리더십 이동을 발산색 히트맵으로. 데이터=거래소 종가+업종분류(뉴스 아님)."},
        ],
        # SELECT 동사 전용 설정 — query="select"일 때만. as-of 단면 랭킹 스크리닝의 모양 제어.
        "select": {
            "field": "select",
            "does": "SELECT 동사 설정 — as_of(기준시점·기본 latest)·top_n|top_pct·descending·display(근거 지표)·mode.",
            "use_for": "스크리닝 결과 모양 제어. 저PBR=descending:false, 고배당=descending:true 등.",
        },
        "select_mode": [
            {"value": "rank", "does": "score를 횡단 랭크해 상위 N 선별(기본 — 스크리닝)",
             "use_for": "조건 맞는 상위 종목 선별."},
            {"value": "compare",
             "does": "지정 종목을 display 지표로 나란히 비교(랭킹 아님·score·top 불요)",
             "use_for": "'피어·A vs B vs C 비교'. universe.kind=list + select.display=[지표] + sort_by(가독 정렬). "
                        "종목마다 describe로 폭발하던 표형 비교를 1콜로 접는다."},
        ],
        # 스터디(study) — 질문을 한 축(axis)으로 펼치고 환원(reduction)한다. axis·reduction은 직교.
        # 기본은 단일 실행(axis='none'·reduction='enumerate'). 펼침은 명시 요청 시에만.
        "study_axis": [
            {"value": "none", "does": "펼침 없이 단일 실행(기본)",
             "use_for": "한 번만 돌릴 때."},
            {"value": "parameter", "does": "param_grid의 점경로별 값 격자로 재실행",
             "use_for": "'기간·비용 등을 바꿔가며 성과 비교'(민감도·최적화). 축 2개+면 데카르트곱."},
            {"value": "entity", "does": "assets 목록의 종목별 개별 성과",
             "use_for": "'종목마다 따로 성과를 본다'. assets(종목 목록) 필요."},
            {"value": "label",
             "does": "1회 실행의 종목별 기여(비중×수익)를 임의 라벨로 사후 그룹 분할해 그룹별 비교",
             "use_for": "라벨이 종목 함수면 '섹터·업종별'(label=attribute('Sector'·'Industry'))·'종목별'; "
                        "일 함수면 '시장 국면별'(label=bucket(임의 신호) — 예: S&P가 20일선 위/아래)·"
                        "'요일·월별'(label=calendar)·'점수 구간별'(label=bucket). 섹터×국면 조합도 가능. "
                        "label은 기존 블록(bucket·calendar·attribute + 임의 신호 조립)으로 자유 구성. label 필수."},
            {"value": "time_fold", "does": "1회 실행 후 수익을 시간순 폴드로 나눠 구간별 성과 일관성 확인(재학습 없음)",
             "use_for": "'시간이 지나도 성과가 일관적인가' 강건성 점검(OOS). reduction=consistency와 함께. "
                        "**'연도별/연간/매년'은 split_period='year'**(달력 연 단위, 키=2015·2016…). "
                        "folds는 시간순 등분 수(기본 4)·split_dates는 명시 경계."},
        ],
        # 달력 주기 분할 — '연도별' 등을 folds 추측 없이 엔진이 실데이터 날짜로 그룹. split_period.
        "study_split_period": [
            {"value": "year", "does": "수익을 달력 연 단위로 그룹(키='2015'·'2016'…)",
             "use_for": "'연도별/연간/매년 성과'. folds 추측(예: 252)하지 말고 이걸 쓴다 — 엔진이 실데이터 연도로 분할."},
            {"value": "quarter", "does": "달력 분기 단위 그룹(키='2015Q1'…)", "use_for": "'분기별 성과'."},
            {"value": "month", "does": "달력 월 단위 그룹(키='2015-01'…)", "use_for": "'월별 성과'."},
        ],
        "study_reduction": [
            {"value": "enumerate", "does": "축의 모든 점을 그대로 나열(각 셀의 성과)"},
            {"value": "contrast", "does": "축의 그룹들을 대조하고 차이의 통계 검정",
             "use_for": "label축 그룹 비교 — 국면·섹터 간 성과 차이가 유의한가."},
            {"value": "consistency", "does": "폴드 간 성과의 일관성 요약",
             "use_for": "time_fold축 — 구간이 바뀌어도 성과가 유지되는가."},
            {"value": "extremize", "does": "축의 셀 중 목적함수(objective)를 최대/최소화하는 최적 셀 선택 + OOS 과최적화 가드",
             "use_for": "파라미터·종목 최적해 — '샤프 최대 파라미터'·'가장 나은 종목'. axis=parameter(+param_grid) "
                        "또는 entity(+assets) + study.objective와 함께. (enumerate=모든 셀 나열과 구분 — 최적 1개 선택.)"},
        ],
        # extremize 목적함수 — metric(최적화 대상)·direction·과최적화 가드. summarize_returns 산출 지표만.
        "objective_metric": [
            {"value": "sharpe", "does": "샤프 비율(위험조정수익, 기본)"},
            {"value": "sortino", "does": "소르티노(하방위험조정)"},
            {"value": "cagr", "does": "연복리수익률(%)"},
            {"value": "cum_return", "does": "누적수익률(%)"},
            {"value": "mdd", "does": "최대낙폭(음수%) — ⚠ '낙폭 최소화'는 direction=max(0에 가까울수록 좋음)"},
        ],
        "objective_direction": [
            {"value": "max", "does": "최대화"},
            {"value": "min", "does": "최소화"},
        ],
        "objective": {
            "field": "study.objective",
            "does": "extremize 목적함수 — metric·direction·oos_guard(in-sample 최적을 시간폴드 OOS 일관성으로 교차검증).",
            "use_for": "최적화 기준 지정. 기본=sharpe/max/guard. 예: '낙폭 최소'=metric:mdd+direction:max.",
        },
        "study_relation_kind": [
            {"value": "ic", "does": "횡단 정보계수(Information Coefficient) — 팩터값과 forward수익의 순위상관"},
            {"value": "regression", "does": "다중 설명변수(factors)의 forward수익 횡단 회귀 — Fama-MacBeth 계수·t값·95% 신뢰구간",
             "use_for": "'여러 팩터 중 무엇이 수익을 설명하나(상호 통제 후)'. study.factors=[score 블록들]·windows·universe 2+종목. 단일=ic."},
            {"value": "correlation", "does": "종목 간 일별수익 상관행렬(피어슨) — 동시점 동조성·분산효과",
             "use_for": "'A·B 상관계수'·'같이 움직이나'·'분산·헤지 후보'. universe 2+종목, target_node·factors 불필요. windows=[N]로 최근 N일."},
        ],
        "regression_factors": {
            "field": "study.factors",
            "does": "relation_kind=regression의 설명변수 목록(각 score/condition 블록). 날짜별 횡단 OLS로 동시 통제.",
            "use_for": "다중팩터 회귀. 예: [pb_ratio, momentum, ...]. IC(target_node 단일)와 구분.",
        },
        "study_event_basis": [
            {"value": "close", "does": "종가→종가 수익(이벤트 스터디 기본)"},
            {"value": "intraday", "does": "시가→종가 수익(당일 반등 포착)"},
            {"value": "excess", "does": "시장 대비 초과수익(universe.kind!=single 필요)"},
        ],
    }
