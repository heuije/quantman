"""러너 능력 계약(RunnerContract) — 러너 단위 단일 선언에서 검증·광고·경계가드·자기서술 파생.

배경(docs/REDESIGN/capability-contract-redesign.md): 블록(BlockDef)·데이터(DataTypeSpec) 계층은
"선언 1곳 → 파생 N곳" 패턴인데 러너(query 동사) 계층만 이 선언이 없어, 능력이 capabilities
손글 산문·컴파일러 레시피에만 존재 → 광고↔검사↔구현↔자기서술이 손동기화로 어긋나며 조용한
오답(음수 창 wrap·코스닥 탈락·IC라벨)이 반복됐다. 이 모듈이 그 빈 층을 채운다.

소비자 4곳:
  · validate_strategy(spec.py)       — 도메인 규칙(컴파일 수리 루프의 교사)
  · run_query(run.py)                — 경계 가드(검증 우회 호출·저장 IR 방어) + runner 키 스탬프
  · capability_spec(capabilities.py) — 러너 광고·명시적 한계(not_supported) 텍스트
  · summarize/status                 — shape·계산 방향(describes) 서술

원칙:
  · 도메인 원시형은 실측 수요만(IntRange·OneOf·MinLen·MinSymbols) — 새 원시형은 실제 결함이
    요구할 때 추가(over-engineering 가드). 필드 교차 제약(basis별 floor 등)은 cross_checks
    콜러블 — 원시형 DSL을 불리지 않는다.
  · 데이터 의존 가정(컬럼 존재·행 수)은 계약 범위 밖 — 무결성 게이트(D-*)·manifest의 층.
  · resolve_runner는 run.py `_dispatch_query`(+engine entry.mode 분기)의 **결정 로직만** 추출한
    SSOT다 — 디스패치 자체는 그대로 두고 검증기·가드가 같은 결정을 공유한다. 드리프트는
    테스트(resolve 예측=실제 실행 러너)로 잠근다.
  · 상한 없는 도메인엔 상한을 만들지 않는다(근거 없는 강성 금지) — 정직한 성능 저하는
    자기서술(표본 커버리지)이 담당.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..blocks.validate import SEV_ERROR, Issue

# ── 도메인 원시형 (4종 고정) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class IntRange:
    """수치 범위(정수·실수 공용 비교). each=True면 리스트 원소별. None(미설정)은 통과 —
    존재 요구는 MinLen/교차 검사가 담당(선택 파라미터를 과잉 강성으로 막지 않기 위함)."""
    min: Optional[float] = None
    max: Optional[float] = None
    each: bool = False

    def check(self, value) -> Optional[str]:
        if value is None:
            return None
        vals = list(value) if self.each and isinstance(value, (list, tuple)) else [value]
        bad = []
        for v in vals:
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                bad.append(v)
                continue
            if (self.min is not None and fv < self.min) or (self.max is not None and fv > self.max):
                bad.append(v)
        if bad:
            rng = (f"{self.min if self.min is not None else ''}~"
                   f"{self.max if self.max is not None else ''}")
            return f"허용 범위({rng}) 밖: {sorted(set(bad), key=str)}"
        return None


@dataclass(frozen=True)
class OneOf:
    """enum 멤버십 — 러너별 *부분집합* 표현용(스키마 전체 어휘와 같으면 Literal에 위임하고 생략)."""
    values: tuple

    def check(self, value) -> Optional[str]:
        if value not in self.values:
            return f"허용 값({'·'.join(map(str, self.values))}) 밖: {value!r}"
        return None


@dataclass(frozen=True)
class MinLen:
    n: int

    def check(self, value) -> Optional[str]:
        if value is None or len(value) < self.n:
            return f"최소 {self.n}개 필요(현재 {0 if value is None else len(value)}개)"
        return None


@dataclass(frozen=True)
class MinSymbols:
    """명시 유니버스 최소 종목 수 — Check(path="universe")로 쓴다.
    kind=all은 런타임 크기라 정적 검사 생략(빈 결과는 러너의 정직 오류가 담당)."""
    n: int

    def check(self, universe) -> Optional[str]:
        if universe is None or universe.kind == "all":
            return None
        if len(dict.fromkeys(universe.symbols)) < self.n:
            return f"종목이 {self.n}개 이상 필요합니다(현재 {len(set(universe.symbols))}개)"
        return None


# ── 계약 선언 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Check:
    """IR 경로 하나에 대한 도메인 검사. rule/message 미지정 시 C-<러너키>/원시형 메시지."""
    path: str                      # 점 경로 — 예: "study.windows", "select.top_n"
    primitive: object              # IntRange | OneOf | MinLen
    rule: Optional[str] = None     # 흡수한 기존 규칙의 ID 호환용(예: "S-event")
    message: Optional[str] = None  # 원시형 메시지 대신 쓸 전체 문장(정직 한계 안내 포함)


@dataclass(frozen=True)
class RunnerContract:
    key: str                                  # resolve_runner가 반환하는 러너 키
    fn: str                                   # 구현 위치(문서·추적용)
    label: str                                # 광고·오류문에 쓰는 한글 이름
    does: str                                 # 무엇을 계산하는가(1문장)
    use_for: str                              # 언제 쓰는가
    not_supported: tuple = ()                 # 명시적 한계 + 대안(광고의 1급 시민)
    shape: str = ""                           # 결과 형상 태그(자기서술 파생용)
    checks: tuple = ()                        # Check 튜플
    cross_checks: tuple = ()                  # Callable[[StrategyIR], Optional[tuple[str, str]]]
                                              #   반환 (message, path) — 필드 교차 제약


def _get_path(obj, path: str):
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        cur = getattr(cur, part, None)
    return cur


def contract_issues(strategy) -> list:
    """IR을 담당 러너 계약과 대조 — 위반은 Issue(SEV_ERROR)로. 미등록 러너는 검사 없음(점진 이행)."""
    c = REGISTRY.get(resolve_runner(strategy))
    if c is None:
        return []
    issues: list = []
    for chk in c.checks:
        bad = chk.primitive.check(_get_path(strategy, chk.path))
        if bad:
            issues.append(Issue(chk.rule or f"C-{c.key}", SEV_ERROR,
                                chk.message or f"{c.label}: {chk.path} — {bad}", chk.path))
    for fn in c.cross_checks:
        r = fn(strategy)
        if r:
            # (rule, message, path) 또는 (message, path) — 흡수한 기존 규칙은 ID를 명시해 소비자 호환.
            if len(r) == 3:
                rule, msg, path = r
            else:
                msg, path = r
                rule = f"C-{c.key}"
            issues.append(Issue(rule, SEV_ERROR, msg, path))
    return issues


# ── 디스패치 결정 SSOT ────────────────────────────────────────────────────────


def resolve_runner(s) -> str:
    """IR → 러너 키. run.py `_dispatch_query`(1차) + engine `run_unified`의 entry.mode 분기(2차)
    + select mode(3차)의 결정 로직을 순수함수로 추출 — 분기 *순서까지* 실제 디스패치와 동일해야
    한다(드리프트는 test_runner_contracts의 예측=실행 일치 테스트가 잠근다)."""
    if s.components:                            # 전략 합성(WS3) — 동사 분기보다 먼저(dispatch 미러)
        return "simulate.composite"
    q = s.query
    if q == "select":
        return "select.compare" if (s.select is not None and s.select.mode == "compare") else "select.rank"
    if q == "prescribe":
        return "prescribe"
    if q == "breadth":
        return "breadth"
    if q == "rotation":
        return "rotation"
    if q == "describe":
        return {"single": "describe.single",
                "portfolio": "describe.portfolio"}.get(s.universe.kind, "describe.signal_dist")
    if q == "relate":
        st = s.study
        if st.axis == "entity":
            return "relate.entity_cohort"
        # WS2 — 축 조합(필드 존재로도 분기: '격자/대안이 있는데 조용히 무시' 부류 차단·dispatch 미러)
        if st.event is not None and (st.axis == "variant" or st.variants):
            return "relate.event_variants"
        if st.event is not None and (st.axis == "parameter" or st.param_grid):
            return "relate.event_sweep"
        if st.event is not None:
            return "relate.event_study"
        if st.relation_kind == "regression":
            return "relate.regression"
        if st.relation_kind == "correlation":
            return "relate.correlation"
        return "relate.ic"
    st = s.study
    if st.axis == "time_fold":
        return "simulate.period_split"
    if st.variants and st.axis in ("none", "variant"):
        return "simulate.sweep.variant"     # 필드 존재 분기(WS2) — run_sweep 승격과 미러
    if st.axis == "none":
        # 엔진 2차 분기(run_unified) — entry.mode는 IR에서 정적으로 읽힌다.
        return ("simulate.event" if s.position.entry.mode == "on_signal"
                else "simulate.scheduled")
    if st.reduction == "extremize":
        return "simulate.extremize"
    return f"simulate.sweep.{st.axis}"      # parameter | entity | label | variant


# ── 계약 레지스트리 (러너 21종 전수 — P2) ─────────────────────────────────────
# 흡수한 기존 검증 규칙은 ID(S-SEL·S-CORR 등)를 유지해 소비자(수리 루프·웹 표시) 호환.
# 새로 승격한 도메인(옛 조용한 실패)은 C-<러너키> ID. 검사 없는 러너도 등록한다 —
# 광고(does/use_for)·shape의 단일 원천이자 "전수 등록됐다"는 계약 테스트의 기준.


def _event_windows_domain(s):
    """윈도우 값 도메인 — 음수(전조·pre-event)는 close/excess 기준에서 1급 지원(WS1).

    잔여 제약 2가지만 거부: ① 0 창은 intraday(당일 시가→종가) 전용 — close/excess의 0은
    무의미(anchor=endpoint). ② intraday 기준의 음수 창 — 장중 전조는 분봉 데이터가 필요해
    데이터 범위 밖(가짜 근사 폴백 금지). 조기 이벤트(시리즈 시작 이전 창)는 검증이 아니라
    엔진 탈락 회계(evaluated_by_window)가 담당한다."""
    st = s.study
    if not st.windows:
        return None                          # 빈 목록은 MinLen 검사가 담당
    ws = {int(w) for w in st.windows}
    if st.event_basis == "intraday":
        bad = sorted(w for w in ws if w < 0)
        if bad:
            return ("S-event",
                    (f"intraday(당일 시가→종가) 기준은 이벤트 *이전*(전조) 창 {bad}을 지원하지 "
                     "않습니다 — 장중 전조는 분봉 데이터가 필요합니다(미보유). 전조 분석은 "
                     "event_basis=close로 음수 윈도우(예: -120=이벤트 전 120일 구간)를 쓰세요."),
                    "study.windows")
        return None
    if 0 in ws:
        return ("S-event",
                ("윈도우 0(당일)은 intraday 기준 전용입니다(close/excess의 0은 무의미) — "
                 "당일 시가→종가 반응은 event_basis=intraday로, 발생 후 경로는 양수, "
                 "이벤트 *이전*(전조) 구간은 음수 윈도우로 분석하세요."), "study.windows")
    return None


def _event_excess_needs_universe(s):
    if s.study.event_basis == "excess" and s.universe.kind == "single":
        return ("S-event", "초과수익(excess) 기준은 시장 지수 생성을 위해 종목이 2개 이상이어야 합니다.",
                "study.event_basis")
    return None


def _select_top_xor(s):
    """top_n/top_pct 정확히 하나 — 옛 S-SEL 값 정합(설정 자체의 존재는 spec의 S-SEL이 담당)."""
    sel = s.select
    if sel is not None and (sel.top_n is None) == (sel.top_pct is None):
        return ("S-SEL", "top_n과 top_pct 중 정확히 하나를 지정하세요.", "select")
    return None


def _cohort_needs_two(s):
    ents = list(dict.fromkeys(s.study.assets or s.universe.symbols))
    if len(ents) < 2:
        return ("종목 코호트 비교는 대상(assets 또는 universe.symbols)이 2개 이상이어야 합니다 — "
                "1개면 단일종목 분석(describe·이벤트 스터디)을 쓰세요.", "study.assets")
    return None


def _portfolio_weights_positive(s):
    w = s.universe.weights
    if w and any(float(v) <= 0 for v in w.values()):
        return ("S-PORT", "보유 비중(weights)은 양수여야 합니다.", "universe.weights")
    return None


def _scheduled_every_n_days(s):
    ent = s.position.entry
    if ent.rebalance == "every_n_days" and not (ent.every_n_days and ent.every_n_days >= 1):
        return ("rebalance=every_n_days는 every_n_days(1 이상)가 필요합니다 — 없으면 리밸런싱이 "
                "한 번도 발화하지 않아 조용한 무거래가 됩니다.", "position.entry.every_n_days")
    return None


def _sweep_param_grid(s):
    st = s.study
    if not st.param_grid or any(not ax.values for ax in st.param_grid):
        return ("S-sweep", "파라미터축 펼침은 param_grid(경로·값 목록)가 필요합니다.", "study")
    return None


def _variants_min2(s):
    """구조 대안 축은 이름 붙은 대안 2개 이상 — 1개면 비교가 아니라 단일 실행이다(WS2)."""
    if len(s.study.variants or []) < 2:
        return ("S-sweep", "variant 축은 이름 붙은 대안(variants)이 2개 이상 필요합니다.",
                "study.variants")
    return None


def _sweep_assets(s):
    if not s.study.assets:
        return ("S-sweep", "자산축 펼침은 assets(종목 목록)가 필요합니다.", "study")
    return None


def _rc(key, fn, label, does, use_for, **kw):
    return RunnerContract(key=key, fn=fn, label=label, does=does, use_for=use_for, **kw)


_MIN2_IC = ("IC(횡단 상관) 분석은 종목이 2개 이상이어야 합니다. "
            "단일종목의 예측력은 이벤트 스터디(query=relate + study.event 조건 + windows)로 분석하세요.")
_MIN2_REG = ("횡단 회귀는 종목이 2개 이상이어야 합니다. "
             "단일종목의 예측력은 이벤트 스터디(query=relate + study.event 조건 + windows)로 분석하세요.")

REGISTRY: dict[str, RunnerContract] = {c.key: c for c in [
    # ── 리서치 동사 ──
    _rc("select.rank", "run.py::run_select", "스크리닝(랭킹)",
        "as-of 시점 횡단 score 랭킹으로 상위 종목 선별(+스크리너 자격 필터)",
        "'지금 조건 맞는 종목 골라줘' — 현 시점 종목 리스트가 답일 때.",
        shape="select",
        checks=(Check("select.top_n", IntRange(min=1), rule="S-SEL",
                      message="top_n은 1 이상이어야 합니다."),
                Check("select.top_pct", IntRange(min=0.000001, max=100), rule="S-SEL",
                      message="top_pct는 0 초과 100 이하여야 합니다.")),
        cross_checks=(_select_top_xor,)),
    _rc("select.compare", "run.py::_run_compare", "종목 비교표",
        "지정 종목들을 지표 컬럼으로 나란히 비교(랭킹·score 불요)",
        "'A vs B 비교해줘' — 표형 피어 비교.", shape="select"),
    _rc("prescribe", "run.py::run_prescribe", "포트폴리오 비중 추천",
        "수익 공분산 기반 비중 최적화(min_variance·max_sharpe·risk_parity·동일가중)",
        "'어떻게 배분할까' — 비중 추천.", shape="prescribe",
        checks=(Check("universe", MinSymbols(2), rule="S-PRESCRIBE",
                      message="포트폴리오 추천은 종목이 2개 이상이어야 합니다(universe.kind=list)."),)),
    _rc("breadth", "run.py::run_breadth", "시장 폭(장세)",
        "상승/하락 종목수·MA 상회 비율 등 시장 내부 지표 집계",
        "'시장 어떤가·왜 빠지나' — 장세 진단.", shape="breadth",
        checks=(Check("universe", MinSymbols(2), rule="S-BREADTH",
                      message="시장 breadth는 종목군이 필요합니다(universe.kind=all/list)."),)),
    _rc("rotation", "run.py::run_rotation", "섹터 순환매 히트맵",
        "섹터(행)×최근 월(열) 평균수익 히트맵으로 리더십 이동 표시",
        "'자금이 어느 섹터로 도나' — 시간에 걸친 섹터 순환.", shape="heatmap",
        checks=(Check("universe", MinSymbols(2), rule="S-ROTATION",
                      message="섹터 순환매는 종목군이 필요합니다(universe.kind=all/list)."),)),
    _rc("describe.single", "run.py::run_describe_report", "단일종목 360 리포트",
        "가격·수익·리스크·밸류·수급·컨센서스를 한 리포트로(시뮬 없음)",
        "'삼성전자 어때' — 한 종목 현황.", shape="describe_single"),
    _rc("describe.portfolio", "run.py::run_portfolio_diagnosis", "포트폴리오 진단",
        "보유 집합의 집중도(HHI)·섹터 노출·가중 밸류·변동성 진단",
        "'내 포트 진단' — 보유 기반(백테스트 아님).", shape="describe_portfolio",
        checks=(Check("universe", MinSymbols(1), rule="S-PORT",
                      message="포트폴리오 진단은 보유 종목(symbols)이 1개 이상 필요합니다."),),
        cross_checks=(_portfolio_weights_positive,)),
    _rc("describe.signal_dist", "run.py::_run_signal_study", "신호값 분포",
        "target_node 신호값의 분포(평균·분위·국면별)",
        "'이 신호가 보통 어떤 값인가' — 손익 아닌 신호 자체.", shape="signal_dist"),
    # ── relate ──
    _rc("relate.event_study", "run.py::_run_event_study", "이벤트 스터디",
        "이벤트(condition 참) 시점 기준 경과일별 수익 경로(평균·MAE·MFE·유의성) — 양수 창=발생 "
        "*이후* forward, 음수 창=발생 *이전*(전조·pre-event) 구간의 누적수익(창 시작점 anchor)",
        "'~한 날 이후 어떻게 되나'(양수 windows) · '급등/이벤트 *전*에 조짐이 있었나'(음수 windows — "
        "예: [-240,-120]=이벤트 전 240일/120일 구간). windows=경과 영업일(부호 있는 정수).",
        not_supported=(
            "intraday(당일 시가→종가) 기준의 음수 창 — 장중 전조는 분봉 데이터가 필요(미보유). "
            "전조 분석은 event_basis=close + 음수 윈도우로 가능.",),
        shape="event_study",
        checks=(Check("study.windows", MinLen(1), rule="S-event",
                      message="이벤트 분석은 윈도우가 필요합니다(경과 영업일 — 음수=이벤트 이전·전조)."),),
        cross_checks=(_event_windows_domain, _event_excess_needs_universe)),
    _rc("relate.event_sweep", "run.py::_run_event_sweep", "이벤트 스터디 × 파라미터 격자",
        "이벤트 정의·윈도우의 파라미터 격자점마다 이벤트 스터디를 재실행해 나란히 비교(버킷)",
        "'임계값 10/20/30%별로 이벤트 후(또는 전) 경향이 어떻게 다른가' — 이벤트 민감도 탐색을 "
        "한 번에(param_grid 경로는 study.event 내부 값·windows 등 아무 점경로).",
        shape="cohort",
        checks=(Check("study.windows", MinLen(1), rule="S-event",
                      message="이벤트 분석은 윈도우가 필요합니다(경과 영업일 — 음수=이벤트 이전·전조)."),),
        cross_checks=(_sweep_param_grid, _event_windows_domain, _event_excess_needs_universe)),
    _rc("relate.event_variants", "run.py::_run_event_variants", "이벤트 조건 대안 비교",
        "이름 붙은 이벤트 조건 대안 N개를 각각 이벤트 스터디로 실행해 나란히 비교(버킷)",
        "'진입조건 A/B/C 중 뭐가 나은가' — 구조가 다른 조건 대안 비교를 1콜로"
        "(study.variants=[{name, node}]).",
        shape="cohort",
        checks=(Check("study.windows", MinLen(1), rule="S-event",
                      message="이벤트 분석은 윈도우가 필요합니다(경과 영업일 — 음수=이벤트 이전·전조)."),),
        cross_checks=(_variants_min2, _event_windows_domain, _event_excess_needs_universe)),
    _rc("relate.ic", "run.py::_run_ic_study", "IC(예측력) 분석",
        "신호(target_node)와 w일 forward 수익의 날짜별 횡단 상관(IC·IR)",
        "'이 지표가 수익을 예측하나' — 다종목 예측력.",
        not_supported=("단일종목 IC — 횡단 상관이라 종목 2+ 필요. 단일종목 예측력은 "
                       "이벤트 스터디(event 조건+windows)로 가능.",),
        shape="relate_ic",
        checks=(Check("study.windows", MinLen(1), rule="S-target",
                      message="IC 분석은 forward 윈도우(windows)가 필요합니다."),
                Check("study.windows", IntRange(min=1, each=True),
                      message="IC forward 윈도우는 1 이상 영업일이어야 합니다(음수·0 미지원)."),
                Check("universe", MinSymbols(2), rule="S-target", message=_MIN2_IC))),
    _rc("relate.regression", "run.py::_run_regression_study", "횡단 다중회귀(Fama-MacBeth)",
        "factors(설명변수들)로 w일 forward 수익을 날짜별 횡단 OLS — 팩터 프리미엄 추정",
        "'여러 팩터를 동시에 통제하면 뭐가 유효한가'.",
        shape="relate_regression",
        checks=(Check("study.factors", MinLen(1), rule="S-REG",
                      message="다중 회귀는 설명변수(factors)가 1개 이상 필요합니다."),
                Check("study.windows", MinLen(1), rule="S-REG",
                      message="회귀는 forward 윈도우(windows)가 필요합니다."),
                Check("study.windows", IntRange(min=1, each=True),
                      message="회귀 forward 윈도우는 1 이상 영업일이어야 합니다(음수·0 미지원)."),
                Check("universe", MinSymbols(2), rule="S-REG", message=_MIN2_REG))),
    _rc("relate.correlation", "run.py::_run_correlation_study", "상관행렬",
        "종목 간 일별수익 피어슨 상관행렬(동시점 동조성)",
        "'같이 움직이나·분산 되나'. windows=[N]이면 최근 N일만.",
        shape="correlation_matrix",
        checks=(Check("universe", MinSymbols(2), rule="S-CORR",
                      message="상관분석은 종목이 2개 이상이어야 합니다(universe.kind=list/all)."),
                Check("study.windows", IntRange(min=1, each=True),
                      message="상관 창(windows)은 1 이상 영업일이어야 합니다."))),
    _rc("relate.entity_cohort", "run.py::_run_entity_cohort", "종목 코호트 비교",
        "같은 분석을 종목별로 개별 실행해 나란히 비교(pool 아님)",
        "'여러 종목 각각 어떤가' — 다종목 1콜.", shape="cohort",
        cross_checks=(_cohort_needs_two,)),
    # ── simulate 계열 ──
    _rc("simulate.event", "engine.py::run_unified(on_signal)", "이벤트 백테스트",
        "신호(condition) 참인 날 진입·exit 규칙으로 청산하는 손익 시뮬",
        "돌파·과매도 반등 등 룰 기반 단발 매매.", shape="simulate"),
    _rc("simulate.scheduled", "engine.py::_run_scheduled", "리밸런스/팩터 백테스트",
        "주기(rebalance)마다 신호로 종목 선택·비중 배분하는 손익 시뮬",
        "정기 리밸런싱 팩터/포트폴리오·상시(always) 보유.", shape="simulate",
        checks=(Check("position.sizing.vol_window", IntRange(min=1),
                      message="변동성 창(vol_window)은 1 이상이어야 합니다(0/음수는 계산 불가)."),),
        cross_checks=(_scheduled_every_n_days,)),
    _rc("simulate.period_split", "run.py::run_period_split", "기간분할(OOS) 검증",
        "1회 실행 수익을 시간 폴드/달력 주기로 나눠 구간별 일관성 확인",
        "'연도별 성과'·강건성 점검.", shape="sweep",   # 결과 형상은 축+버킷(sweep 계열)
        checks=(Check("study.folds", IntRange(min=1),
                      message="folds는 1 이상이어야 합니다."),)),
    _rc("simulate.extremize", "run.py::run_extremize", "최적해 탐색",
        "축(parameter/entity)의 셀 중 목적함수 최적 셀 선택(+OOS 가드)",
        "'샤프 최대 파라미터 찾아줘'.", shape="extremize"),
    _rc("simulate.sweep.parameter", "run.py::run_sweep(parameter)", "파라미터 펼침",
        "param_grid 값 격자로 재실행해 셀별 성과 나열",
        "민감도 — '기간을 바꿔가며 비교'.", shape="sweep",
        cross_checks=(_sweep_param_grid,)),
    _rc("simulate.sweep.entity", "run.py::run_sweep(entity)", "종목 펼침",
        "assets 종목별 개별 백테스트 성과 나열",
        "'종목마다 따로 성과'.", shape="sweep",
        cross_checks=(_sweep_assets,)),
    _rc("simulate.sweep.label", "run.py::run_sweep(label)", "라벨 분할 성과",
        "1회 실행의 종목별 기여를 라벨(섹터·국면 등)로 그룹 분할 비교",
        "'섹터별/국면별 성과'.", shape="sweep"),
    _rc("simulate.sweep.variant", "run.py::run_sweep(variant)", "신호 대안 백테스트 비교",
        "이름 붙은 신호 대안(variants=[{name, node}]) N개를 각각 백테스트해 성과 버킷으로 비교",
        "'전략 A안 vs B안 vs C안 성과' — 값이 아니라 *구조*가 다른 신호 대안 비교를 1콜로.",
        shape="sweep",
        cross_checks=(_variants_min2,)),
    _rc("simulate.composite", "run.py::run_composite", "전략 합성(병행 포트폴리오)",
        "완결 전략 구성(components=[{weight, ir}]) 2개+를 각각 백테스트해 일일수익을 가중 "
        "합산(고정비중 매일 리밸런스 혼합) — 합성 자본곡선+구성별 분해",
        "'전략 A와 B를 병행하면'·'둘을 합친 포트폴리오'·기본 페어(같은 신호 A롱+B숏 2구성). "
        "top-level signal은 명목(data Close).",
        not_supported=(
            "구성 간 공동 증거금·두 다리 동시 체결(조인트 멀티레그) — 수익률 수준 합성만 "
            "(구성별 독립 실행·고정비중). 자동매매 연동도 미지원(백테스트 전용 — 합성은 "
            "구성별 개별 전략으로 등록 가능).",),
        shape="simulate"),
]}


def runner_limits_text() -> str:
    """컴파일러 프롬프트용 '지원 한계' 텍스트 — 계약 not_supported에서 생성(산문 손동기화 제거)."""
    lines = []
    for c in REGISTRY.values():
        for item in c.not_supported:
            lines.append(f"- {c.label}: {item}")
    return "\n".join(lines)
