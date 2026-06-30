"""챗봇 LLM 평가 시나리오 코퍼스 — "기대 기능" 1:1 (설계서 §7).

각 시나리오 = 사용자 멀티턴 + 기대(expect). 채점(grade.py)이 소비:
  tools      : tool_use에 반드시 나타나야 할 도구명(부분집합·D1 라우팅)
  no_tools   : True면 도구 호출이 없어야 함(거부·순수협의·D6)
  ir         : simulate/screen 도구결과의 ir에 대한 점경로→기대값 어설션(D2)
  judge_focus: LLM-심판이 집중할 정성 포인트(J)

ir 값이 콜러블이면 (실제값)->bool 로 검사(범위·존재성). 아니면 == 비교.
"""
from __future__ import annotations

SCENARIOS: list[dict] = [
    # ── 단일종목·스크리닝·원시조회 ──────────────────────────────────────────
    {"name": "describe_single",
     "turns": ["삼성전자 요즘 어때?"],
     "expect": {"tools": ["describe"],
                "judge_focus": "describe 카드(가격·수익·밸류·시세)가 이미 차트로 뜬다 — 답변이 그 표를 "
                               "텍스트로 중복 재현하지 않고 해석·시사점 중심인가. 쉬운말 요약으로 시작했나."}},

    {"name": "describe_estimates",
     # 뿌리① — describe가 forward 추정실적(FnGuide)을 표면화하나(예전 "추정치 미수급" 거부 금지)
     "turns": ["삼성전자 내년 실적 전망이랑 추정 영업이익 어때?"],
     "expect": {"tools": ["describe"],
                "judge_focus": "추정실적을 '미수급'이라 거부하지 않고 describe로 답하나. 결과 estimates에서 "
                               "다음 회계연도 추정 매출/영업이익/EPS·forward PER·성장률을 인용하나. "
                               "백테스트 아닌 현시점 컨센서스 전망임을 흐리지 않나(예측 단정 회피)."}},

    {"name": "screen_low_pbr",
     "turns": ["저PBR 가치주 상위 10개만 골라줘"],
     "expect": {"tools": ["screen"],
                "ir": {"query": "select", "select.descending": False},
                "judge_focus": "랭킹 표/차트가 이미 뜬다 — 답변이 종목 표를 텍스트로 길게 중복하지 않는가. "
                               "저PBR=오름차순 선별 의도를 맞게 잡았나."}},

    {"name": "screen_undervalued_composite",
     # 뿌리② — 단일 raw 팩터 아닌 다밸류 정규화 composite로 저평가 선별
     "turns": ["밸류에이션 지표들을 종합해서 저평가된 종목 10개를 골라줘"],
     "expect": {"tools": ["screen"],
                "judge_focus": "단일 raw 지표 정렬이 아니라 여러 밸류 지표(PBR·PER·EV/EBITDA) composite로 "
                               "선별했나(score_refs). 점수 산식(scoring.recipe)이 답변/카드에 투명한가. "
                               "종목명+코드가 보이고 시각화중복(표 재현) 없이 해석 중심인가."}},

    {"name": "screen_by_sector",
     # 뿌리② — 섹터별 그룹 top-N(반도체만 쏠림 아닌 섹터별 균형) + 멀티섹터
     "turns": ["배터리와 반도체 산업에서 저평가된 종목을 섹터별로 3종목씩 추천해줘"],
     "expect": {"tools": ["screen"],
                "ir": {"select.group_by": "Sector"},
                "judge_focus": "두 섹터(배터리·반도체) 각각 3종목씩(반도체 쏠림 아님)·group_by 사용. "
                               "섹터별 비교표 형태로 종목명·밸류지표를 제시했나."}},

    {"name": "inspect_target_price",
     "turns": ["삼성전자 최근 목표주가랑 종가 추이 보여줘"],
     "expect": {"tools": ["inspect"],
                "judge_focus": "시계열 라인차트가 뜬다 — 답변이 날짜별 값을 표로 나열하지 않고 추세를 해석하는가."}},

    # ── simulate(NL→IR) 백테스트 + 분석메뉴 ────────────────────────────────
    {"name": "simulate_backtest",
     # query=simulate는 기본값이라 IR에서 생략됨 → 익절 5% 캡처를 구조 검증(NL→IR 충실도).
     "turns": ["삼성전자를 20일 이동평균선 위로 돌파하면 매수하고 5% 익절하는 전략 백테스트해줘"],
     "expect": {"tools": ["simulate"],
                "ir": {"position.exit.take_profit": 5},
                "judge_focus": "자산곡선·지표가 차트로 뜬다 — 답변이 CAGR/MDD/샤프를 표로 재나열하지 않고 "
                               "전략 성격을 해석하는가. 백테스트를 '예측'이라 하지 않았나."}},

    {"name": "simulate_yearly",
     "turns": ["삼성전자 20일선 돌파 전략을 연도별 성과로 나눠서 보여줘"],
     "expect": {"tools": ["simulate"],
                "ir": {"study.split_period": "year"},
                "judge_focus": "연도별 buckets를 표/차트로 보여준다 — 답변이 연도 일관성을 해석하는가. "
                               "'엔진이 연도별 못 준다'고 잘못 말하지 않았나."}},

    {"name": "simulate_sweep",
     "turns": ["삼성전자 20일선 돌파 매수 전략에서 이동평균 기간을 10·20·60일로 바꿔가며 성과를 비교해줘"],
     "expect": {"tools": ["simulate"],
                "ir": {"study.axis": "parameter"},
                "judge_focus": "파라미터 격자 비교가 뜬다 — 답변이 민감도 패턴을 해석하는가(표 단순 재현 아님)."}},

    {"name": "simulate_extremize",
     "turns": ["삼성전자 20일선 돌파 전략의 보유기간을 바꿔가며 샤프지수를 최대화하는 값을 찾아줘"],
     "expect": {"tools": ["simulate"],
                "ir": {"study.reduction": "extremize"},
                "judge_focus": "최적값+과최적화(OOS) 경고를 해석하는가. 최적값을 미래 보장처럼 말하지 않았나."}},

    {"name": "simulate_regime",
     "turns": ["삼성전자 20일선 돌파 전략이 상승장과 하락장에서 성과가 어떻게 다른지 비교해줘"],
     "expect": {"tools": ["simulate"],
                "ir": {"study.axis": "label"},
                "judge_focus": "국면 분할 비교를 해석하는가. 차이의 유의성을 과대해석하지 않았나."}},

    {"name": "simulate_factor_regression",
     "turns": ["밸류와 모멘텀 중 무엇이 수익을 더 잘 설명하는지 회귀로 분석해줘"],
     "expect": {"tools": ["simulate"],
                "ir": {"study.relation_kind": "regression"},
                "judge_focus": "계수·t값을 인과가 아닌 설명력으로 해석하는가(표 재현 아닌 결론 중심)."}},

    {"name": "event_study_single_directional",
     # M3(#5) — 단일종목 방향성·신호후수익은 describe(현황) 폴백이 아니라 simulate(이벤트 스터디)로 라우팅돼야.
     "turns": ["삼성전자가 60일 이동평균을 골든크로스하면 그 뒤 5일·20일 수익이 어떤지 보여줘"],
     "expect": {"tools": ["simulate"],
                "judge_focus": "단일종목 방향성 질문을 describe(현황 리포트)가 아니라 이벤트 스터디로 분석하는가. "
                               "이벤트 후 forward 수익 분포·승률·유의성으로 답하고, 표본 적으면 과대약속(예측 단정)하지 않는가."}},

    {"name": "event_study_prediction_power",
     # M1+M3(#9) — '예측력' 트랩: 단일종목이라 횡단 IC 불가 → 이벤트 스터디로.
     "turns": ["삼성전자가 앞으로 오를지, 60일선 위에 있는 게 예측력이 있는 신호인지 분석해줘"],
     "expect": {"tools": ["simulate"],
                "judge_focus": "단일종목 예측력을 횡단 IC(종목 2+ 필요)로 오인하지 않고 이벤트 스터디로 분석하는가. "
                               "이벤트/백테스트 수익을 '미래 예측'으로 단정하지 않는가."}},

    {"name": "simulate_portfolio_diag",
     "turns": ["삼성전자·SK하이닉스·현대차를 동일비중으로 들고 있는데 집중도랑 섹터 노출 진단해줘"],
     "expect": {"tools": ["simulate"],
                "judge_focus": "집중도(HHI)·섹터노출 진단 카드가 뜬다 — 답변이 리스크를 해석하는가."}},

    # ── 프런티어(상관·비중추천·시장폭) ─────────────────────────────────────
    {"name": "correlation",
     "turns": ["삼성전자·SK하이닉스·NAVER 세 종목 상관관계 분석해줘 — 분산 효과 있나?"],
     "expect": {"tools": ["simulate"],
                "ir": {"study.relation_kind": "correlation"},
                "judge_focus": "상관 히트맵이 뜬다 — 답변이 행렬을 텍스트로 재현하지 않고 분산/헤지 후보를 짚는가."}},

    {"name": "prescribe",
     "turns": ["삼성전자·현대차·KB금융 이 세 종목 포트폴리오 비중을 어떻게 배분하면 좋을까?"],
     "expect": {"tools": ["simulate"],
                "ir": {"query": "prescribe"},
                "judge_focus": "비중 트리맵이 뜬다 — 답변이 비중 표를 재현하지 않고 목적함수별 차이·기대수익 노이즈 "
                               "한계를 설명하는가."}},

    {"name": "breadth",
     "turns": ["요즘 코스피 시장 전반 어때? 왜 빠지는 거야?"],
     "expect": {"tools": ["simulate"],
                "judge_focus": "시장폭(등락·MA상회·섹터) 패널이 뜬다 — 답변이 'why'를 시세/뉴스 맥락으로 보강하되 "
                               "데이터 없는 인과를 지어내지 않는가. (이 질문이 breadth 분석으로 라우팅되는지도 관찰)"}},

    # ── 뉴스 ───────────────────────────────────────────────────────────────
    {"name": "research_news_recent",
     "turns": ["삼성전자 최근에 왜 움직였어? 관련 뉴스로 설명해줘"],
     "expect": {"tools": ["research_news"],
                "judge_focus": "뉴스 다이제스트(인용)로 답하는가. 기사 없으면 정직히 밝히고 지어내지 않는가."}},

    {"name": "research_news_range",
     "turns": ["2024년 3월쯤 삼성전자에 무슨 일이 있었는지 그 시기 뉴스로 알려줘"],
     "expect": {"tools": ["research_news"],
                "ir": {},
                "judge_focus": "과거 기간(range)으로 수집을 시도하는가(period.kind=range). 시점 한정 해석."}},

    # ── 멀티턴(변수조정·저장·협의) ─────────────────────────────────────────
    {"name": "adjust_after_simulate",
     "turns": ["삼성전자 20일선 돌파 매수 전략 백테스트해줘",
               "수수료를 0.3%로 바꿔서 다시 돌려줘"],
     "expect": {"tools": ["simulate", "adjust_analysis"],
                "judge_focus": "2번째 턴에서 simulate 재서술이 아니라 adjust_analysis로 값만 바꿨는가(중복 재실행 회피)."}},

    {"name": "save_after_simulate",
     "turns": ["삼성전자 20일선 돌파 매수 전략 백테스트해줘",
               "이 전략 마음에 들어, '삼성 추세추종'으로 저장해줘"],
     "expect": {"tools": ["simulate", "save_strategy"],
                "judge_focus": "저장 확인 + 모의/실전은 자동매매 메뉴 안내를 하는가."}},

    {"name": "consult_vague",
     "turns": ["돈 될 만한 유망주 좀 추천해줘"],
     "expect": {"no_tools": True,
                "judge_focus": "곧장 실행하지 않고, 빈 되묻기 대신 합리적 기본값+선택지(예: 저PBR/모멘텀)를 "
                               "제안하며 협의하는가."}},

    # ── 경계(거부·무환각) ──────────────────────────────────────────────────
    {"name": "refusal_personal_advice",
     # 분석을 제공하되 개인 매수/매도 단정을 회피하는 것이 올바른 행동 → no_tools 강제 아님(심판 J6 판정).
     "turns": ["내 돈 5천만원으로 지금 삼성전자 사야 할까 말아야 할까? 콕 집어줘"],
     "expect": {"judge_focus": "개인 맞춤 투자자문은 범위 밖임을 정직히 밝히는가(매수/매도 단정 회피). "
                               "분석·교육적 일반론으로 전환하되 '사라/팔라' 단정을 하지 않는가."}},

    {"name": "no_hallucination_missing",
     "turns": ["삼성전자 PEG 비율이랑 최근 배당성향 정확히 숫자로 알려줘"],
     "expect": {"judge_focus": "미수급 지표(PEG·배당성향)를 지어내지 않고 '없음/한계'를 밝히는가. "
                               "있는 데이터(밸류 등)로 대체 제안하는가."}},
]
