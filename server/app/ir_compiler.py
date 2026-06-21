"""자연어 전략 설명 → StrategyIR 컴파일러 (Anthropic Claude).

설계(프롬프트 엔지니어링 베스트프랙티스):
  - 역할·정적 지식을 XML 섹션으로 구조화한 시스템 프롬프트(프롬프트 캐싱).
  - **단계별 추론 강제**: emit_strategy 도구의 필드 순서가 곧 추론 순서다
    (intent_summary → strategy_archetype → mapping_rationale → strategy → ...).
    "번역 전에 이해"하게 만들어, 메커니즘/수식 설명도 그 이면의 매매 의도로 환원한다.
  - **능력(capability) 의미 명세**(capability_spec): 각 프리미티브가 '무엇을 달성하고 어떤
    전략에 쓰는가'를 줘서, '목표 베타·상수 레버리지·종가 리밸런싱'을 always+leverage로
    일반적으로 매핑한다(단건 few-shot 땜질 아님).
  - 내부 validate→repair 루프(유저 명료화 없음). 검증기가 실행불가 IR을 구조적으로 차단.

키(ANTHROPIC_API_KEY) 미설정 시 success=False 사유 반환 — 다른 기능엔 영향 없음.
"""
from __future__ import annotations

import json
from typing import Callable

from quant_core.indicators import get_indicator_compare_group

from .config import settings

# ── few-shot: 단계별 추론 형식을 보여주는 검증된 예시 (특정 답 암기가 아니라 '과정'을 가르침) ──
_FEWSHOT = [
    {
        "nl": "삼성전자를 종가가 60일 이동평균 위로 올라오면 매수하고 15% 익절 7% 손절",
        "out": {
            "intent_summary": "삼성전자 단일 종목. 종가가 60일 이동평균을 상향 돌파한 날 매수, 15% 익절 또는 7% 손절 시 청산.",
            "strategy_archetype": "이벤트룰",
            "mapping_rationale": "단일 종목→universe.single. '돌파한 날 매수'=이벤트→entry.on_signal. 신호=compare(종가 > ts_mean(종가,60)). 익절/손절→exit.take_profit=15/stop_loss=-7.",
            "strategy": {
                "name": "삼성전자 60일선 돌파",
                "universe": {"kind": "single", "symbols": ["005930"]},
                "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
                    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "ts_mean", "params": {"window": 60}, "inputs": {
                        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}}},
                "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "on_signal"},
                             "exit": {"take_profit": 15, "stop_loss": -7}},
                "simulation": {"initial_capital": 100000000, "fill": "next_open"},
            },
            "assumptions": ["'60일 이동평균'을 종가 60일 단순이동평균으로 해석"],
            "expressible": True,
        },
    },
    {
        "nl": "KODEX 200을 노출이 순자산의 2배가 되도록 매일 맞추며 계속 보유",
        "out": {
            "intent_summary": "KODEX200(069500)을 항상 보유하되, 노출을 순자산의 2배로 매일 유지(상승하면 더 사고 하락하면 줄임).",
            "strategy_archetype": "상수레버리지·상시보유",
            "mapping_rationale": "'노출을 순자산의 N배로 매일 유지'=entry.always(매일 리밸런싱)+simulation.leverage=2(노출=leverage×순자산). 매일 종가 기준 조정→fill=close. 계속 보유→신호는 항상 참(compare(종가>0)). exit는 always에서 무시되므로 비움.",
            "strategy": {
                "name": "KODEX200 2배 상수 레버리지",
                "universe": {"kind": "single", "symbols": ["069500"]},
                "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
                    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "const", "params": {"value": 0}}}},
                "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "always"}, "exit": {}},
                "simulation": {"initial_capital": 100000000, "fill": "close", "leverage": 2},
            },
            "assumptions": ["'2배'를 시뮬레이션 레버리지 2로 해석", "신호는 항상 참(종가>0)으로 매일 보유 유지"],
            "expressible": True,
        },
    },
    {
        "nl": "KODEX200을 종가가 N일 이동평균 위로 올라오면 매수, 이동평균 기간(10·20·60)과 보유일(20·40)을 바꿔가며 성과를 한눈에 비교",
        "out": {
            "intent_summary": "KODEX200(069500) 단일. 종가가 N일 이동평균 상향 돌파 시 매수. 이동평균 기간과 보유일을 격자로 펼쳐 성과 비교.",
            "strategy_archetype": "파라미터 펼침(민감도)",
            "mapping_rationale": "단일→universe.single. 'N일선 위로 올라오면 매수'=신호 compare(종가>ts_mean(종가,N))+entry.on_signal. '바꿔가며 한눈에 비교'=study.axis=parameter(query는 기본 simulate). 두 변수→study.param_grid 두 축. **각 축은 {path:점경로, values:[값들]}** — 항목을 이름으로 감싸지 않는다. 신호의 기간은 ts_mean 블록의 window 경로, 보유일은 position.exit.hold_days 경로. 2축이면 데카르트곱.",
            "strategy": {
                "name": "KODEX200 이동평균 돌파 민감도",
                "universe": {"kind": "single", "symbols": ["069500"]},
                "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
                    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "ts_mean", "params": {"window": 20}, "inputs": {
                        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}}},
                "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "on_signal"}, "exit": {"hold_days": 20}},
                "simulation": {"initial_capital": 100000000, "fill": "next_open"},
                "study": {"axis": "parameter", "param_grid": [
                    {"path": "signal.inputs.right.params.window", "values": [10, 20, 60]},
                    {"path": "position.exit.hold_days", "values": [20, 40]}]},
            },
            "assumptions": ["이동평균 기간 N을 [10,20,60], 보유일을 [20,40]으로 격자 해석"],
            "expressible": True,
        },
    },
    {
        # cross-asset 신호 참조 + %단위 임계 + 비용(분수) + 당일 롱숏 — 라이브 결함 부류 앵커
        # (±0.1%를 −0.001로 100×축소·commission_pct 환각·S&P500 접두 소실을 한 예시로 교정).
        "nl": "S&P500이 전일대비 -0.1% 이하면 코스피200선물을 시가매수해 당일 종가청산, +0.1% 이상이면 시가매도해 당일 종가청산. 수수료 편도 0.01%로 연도별 수익률.",
        "out": {
            "intent_summary": "코스피200선물 일중매매. 전일 S&P500 등락률이 -0.1% 이하면 시가 매수→당일 종가 청산(롱), +0.1% 이상이면 시가 매도→당일 종가 청산(숏). 연도별 성과.",
            "strategy_archetype": "조건별 롱숏 일중매매(이벤트룰)",
            "mapping_rationale": "타 자산(S&P500) 신호로 코스피200선물 매매 → 신호 ref에 'S&P500.pct_change_1d'(cross-asset, 매매는 universe 종목으로). 조건별 롱/숏=부호점수 select(레시피1): ≤-0.1%→+1(롱)·≥+0.1%→-1(숏)·그 외 0. pct_change_1d는 %단위라 임계는 -0.1·0.1(−0.001로 분수화 금지). 당일매매=on_signal+exit.hold_days=0+fill=next_open(시가진입·종가청산). 수수료 편도 0.01%=simulation.commission=0.0001(분수, commission_pct 아님). 연도별=study.axis=time_fold+reduction=enumerate.",
            "strategy": {
                "name": "S&P500 신호 코스피200선물 일중 롱숏",
                "universe": {"kind": "single", "symbols": ["코스피200선물"]},
                "signal": {"op": "select", "inputs": {
                    "cond": {"op": "compare", "params": {"op": "<="}, "inputs": {
                        "left": {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}},
                        "right": {"op": "const", "params": {"value": -0.1}}}},
                    "a": {"op": "const", "params": {"value": 1}},
                    "b": {"op": "select", "inputs": {
                        "cond": {"op": "compare", "params": {"op": ">="}, "inputs": {
                            "left": {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}},
                            "right": {"op": "const", "params": {"value": 0.1}}}},
                        "a": {"op": "const", "params": {"value": -1}},
                        "b": {"op": "const", "params": {"value": 0}}}}}},
                "position": {"direction": "long_short", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "on_signal", "threshold": 0}, "exit": {"hold_days": 0}},
                "simulation": {"initial_capital": 100000000, "fill": "next_open", "commission": 0.0001},
                "study": {"axis": "time_fold", "reduction": "enumerate", "split_period": "year"},
            },
            "assumptions": ["S&P500.pct_change_1d=전일대비 등락률(%)을 cross-asset 신호로 참조",
                            "전일 S&P500 기준 당일 코스피200선물 시가진입·종가청산(hold_days=0)",
                            "수수료 편도 0.01%=commission 0.0001(분수), 슬리피지 미지정→엔진 기본",
                            "연도별=split_period='year'(달력 연 단위, 키=2015·2016… — folds 추측 아님)"],
            "expressible": True,
        },
    },
]


def _param_text(p: dict) -> str:
    name = p.get("name", "?")
    opts = p.get("options")
    if opts:
        return f'{name}={"|".join(str(o) for o in opts)}'
    if p.get("default") is not None:
        return f'{name}(기본 {p["default"]})'
    return name


def _catalog_text(catalog: list[dict]) -> str:
    lines = []
    for b in catalog:
        parts = [f'{b["op"]}', f'[{b.get("label","")}]', f'→{b.get("out_type","")}']
        slots = b.get("slots") or {}
        if slots:
            parts.append("slots=" + ",".join(f"{k}:{v}" for k, v in slots.items()))
        params = b.get("params") or []
        if params:
            parts.append("params={" + ", ".join(
                _param_text(p) for p in params if isinstance(p, dict)) + "}")
        if b.get("variadic"):
            parts.append("(가변입력)")
        doc = (b.get("doc") or "").strip().splitlines()
        if doc:
            parts.append("— " + doc[0])
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


def _cap_item(it: dict) -> str:
    v = it.get("value") or it.get("field") or ""
    s = f'    - {v}: {it.get("does", "")}'
    if it.get("use_for"):
        s += f'  〔쓰임: {it["use_for"]}〕'
    return s


def _capabilities_text(caps: dict) -> str:
    out = []
    for cat, val in caps.items():
        out.append(f'  [{cat}]')
        if isinstance(val, list):
            out += [_cap_item(it) for it in val]
        elif isinstance(val, dict):
            out.append(_cap_item(val))
            for k, d in (val.get("knobs") or {}).items():
                out.append(f'        · {k}: {d}')
        else:
            out.append(f'    {val}')
    return "\n".join(out)


def _system_prompt(catalog: list[dict], capabilities: dict, indicator_cols: list[str]) -> str:
    examples = "\n\n".join(
        f"<example>\n입력: {ex['nl']}\nemit_strategy 인자:\n{json.dumps(ex['out'], ensure_ascii=False, indent=1)}\n</example>"
        for ex in _FEWSHOT
    )
    # %계열 지표(COMPARE_GROUP SSOT) — 임계 const 스케일 가이드용(값이 이미 퍼센트).
    pct_cols = [c for c in indicator_cols if get_indicator_compare_group(c) == "pct"]
    return f"""<role>
너는 한국어 투자전략 설명을 백테스트 IR(StrategyIR JSON)로 변환하는 결정론적 컴파일러다.
오직 emit_strategy 도구로만 결과를 제출한다. 아래 <capabilities>·<block_catalog>·<reference_data>에
없는 진입모드·op·지표·필드를 지어내지 않는다. 출력 언어(설명·가정)는 한국어.
</role>

<ir_structure>
StrategyIR = {{
  "name": str,
  "universe": {{"kind": "single|list|all", "symbols": [..], "screener": {{"condition": <블록>, "refresh": "each_rebalance|once_at_start"}}}},
  "signal": <블록트리>,          // 신호. {{op, params, inputs:{{slot: 자식블록}}}} 재귀. 잎: data{{ref}}, const{{value}}
  "position": {{"direction":.., "sizing":{{"mode":..}}, "entry":{{"mode":..}}, "exit":{{..}}, "overlays":{{..}}}},
  "simulation": {{"initial_capital":.., "fill":.., "leverage":.., "start":"YYYY-MM-DD", "end":"YYYY-MM-DD", ...}},
  "query": "simulate|select|describe|relate|prescribe|breadth",   // 무엇을 묻는가(기본 simulate=손익 백테스트). select=현시점 스크리닝, describe=살펴보기(단일종목 리포트·포트폴리오 진단·신호 분포), relate=관계/이벤트/상관, prescribe=포트폴리오 비중 추천(최적화), breadth=시장 폭(장세).
  "prescribe": {{"max_weight":0~1|null, "window":N|null}},   // query="prescribe" 전용 — 종목당 비중 상한·추정 거래일수(생략 가능)
  "study": {{"axis":"none|parameter|entity|label|time_fold", "reduction":"enumerate|contrast|consistency|extremize", "param_grid":[{{"path":점경로,"values":[..]}}], "assets":[..], "label":<블록>, "split_period":"year|quarter|month", "folds":N, "split_dates":["YYYY-MM-DD",..], "target_node":<블록>, "relation_kind":"ic|regression|correlation", "factors":[<블록>,..], "windows":[..], "event":<블록>, "objective":{{"metric":..,"direction":"max|min","oos_guard":bool}}}}  // objective는 extremize 전용
}}
// ⚠ time_fold에서 **"연도별/연간/매년"은 study.split_period="year"**(엔진이 달력 연으로 분할, 키=2015·2016…). folds=252 같은 "1년 거래일수"로 추측하지 말 것(라이브 결함). 분기별=quarter·월별=month. folds는 단순 시간순 등분 수일 뿐.
펼침/분석이 없으면 query·study를 생략(기본 simulate·axis=none). 종목 자신의 컬럼은 ref에 "__SELF__." 접두(예 "__SELF__.Close"). 신호 out_type: condition(룰)·score(팩터)·value·label.
</ir_structure>

<capabilities>
각 구성요소가 '무엇을 하는가(does)'와 '어떤 전략 의도에 쓰는가(쓰임)'. **의도→프리미티브 매핑의 1차 근거.**
{_capabilities_text(capabilities)}
</capabilities>

<block_catalog>
신호 블록(op) 목록 — 이것만 사용:
{_catalog_text(catalog)}
</block_catalog>

<reference_data>
지표 ref(이 외 임의 지표 금지; 종목 자신은 __SELF__. 접두): {", ".join(indicator_cols)}
기본 OHLCV: Open, High, Low, Close, Volume
종목 표기: 국내주식=6자리 코드(삼성전자 005930), 미국주식=티커(AAPL), 내장 자산명=정확한 키
(S&P500, 코스피200선물, 원유선물, 금선물, 은선물(COMEX), 천연가스선물, 나스닥선물, 비트코인선물 등). 모르면 사용자가 쓴 명칭 그대로.
</reference_data>

<units_and_costs>
const(상수)의 **스케일**을 틀리면 전혀 다른 전략이 된다(라이브 실측 결함 — ±0.1%를 ±0.001로 100× 축소해 매일 진입). 반드시:
- **%계열 지표는 값이 이미 퍼센트**다. "전일대비 -0.1%"는 const **-0.1** 이다. -0.001(분수)로 ÷100 하지 말 것. 익절/손절(take_profit·stop_loss)도 퍼센트(15% → 15). SYM. 접두가 붙어도 동일(S&P500.pct_change_1d 도 %).
  %계열 지표: {", ".join(pct_cols)}
- **타 종목 신호 참조**는 "SYM.지표" 형태(예: S&P500.pct_change_1d) — 신호가 다른 자산을 봐도 매매는 universe 종목으로 한다. 재작성(repair) 시 SYM. 접두를 떨어뜨려 자기참조로 바꾸지 말 것.
- **비용**을 사용자가 지정하면 simulation.commission·simulation.slippage 에 **분수**로 넣는다: 편도 0.01% → 0.0001, 0.005% → 0.00005, 0.1% → 0.001. (commission_pct·transaction_cost_pct 같은 필드는 **없다** — 쓰면 조용히 무시된다.) 미지정이면 비우고 엔진 기본값(수수료 0.03%·슬리피지 0.1%)에 맡긴다.
</units_and_costs>

<idioms>
원자(블록)만으로는 안 보이는 **검증된 합성 레시피**. 의도가 아래 패턴에 해당하면 그대로 따르고,
이 목록을 먼저 대조한 뒤에야 '표현 불가'나 '단순화'를 판단한다(원자는 있어도 합성을 못 찾으면 안 됨).

1. [조건 기반 롱/숏/중립] "A 규칙이면 롱, (다른) B 규칙이면 숏, 아니면 미보유"처럼 롱·숏에 *서로 다른 조건*.
   → long_short는 단일 score의 부호로 방향을 가르므로, select로 **부호 점수**를 만든다:
     signal = select(cond=A, a=const(1), b=select(cond=B, a=const(-1), b=const(0)))
     position.direction="long_short", entry.threshold=0. 부호>0=롱·<0=숏·=0=미보유(중립밴드 자동).
   **entry.mode는 의도로 가른다(M5d):**
   · 이벤트/당일·룰 트리거("시가 매수 종가 매도"·"~면 진입") → entry.mode="on_signal". 부호방향이
     바별로 롱/숏을 가르며 라이브 양방향 체결 가능(선물 sell-to-open). 당일매매면 exit.hold_days=0
     (+ simulation.fill="next_open" → 시가진입·종가청산).
   · 정기 리밸런스/추세 보유(주기적으로 부호 재평가해 계속 보유) → entry.mode="scheduled"(rebalance="daily").
   A·B 안의 임계 const를 param_grid 두 축으로 독립 스윕 가능.
   (단방향만이면 on_signal+condition+direction="long"/"short". 양방향은 위 부호점수+long_short.)
2. [시계열 모멘텀(TSMOM) 롱숏] "추세가 양이면 롱, 음이면 숏" → signal=score(예: ts_delta(Close,N)),
   direction="long_short", entry.threshold=0. 부호가 곧 방향(중립=정확히 0).
3. [정기 리밸런스 팩터(횡단)] "매월/매주 ___ 상위 N(또는 X%) 보유" → universe.kind=all(또는 list+세부조건),
   signal=score(팩터), entry.mode="scheduled"+rebalance, top_n 또는 top_pct. 롱숏이면 부호/순위로 양다리.
   "거래대금·시총·밸류 등으로 선별한 종목에서"처럼 자격 필터가 붙으면 universe.screener={{condition, refresh}}로 2차 선별.
4. [국면별 비교] "상승장/하락장 등 국면에 따라 신호·성과가 어떻게 다른가" → 신호 대수는 그대로 두고
   분할 라벨로 본다. *신호 자체의 분포*가 국면별로 어떤지면 query="describe"+study.target_node(그 신호)
   +study.label(국면 라벨); *전략 성과*가 국면별로 다른지면 query는 기본 simulate, study.axis="label"
   +study.reduction="contrast"+study.label(국면 라벨). 어느 쪽이든 신호 대수(signal)는 바꾸지 않는다.
5. [조건 지속/최근 발생] "N일 연속 충족"·"최근 M일 내 발생" → condition을 modifier 블록으로 감싼다.
6. [선물 디렉셔널·추세추종] 선물 심볼(코스피200선물·원유선물·금선물·나스닥선물·은선물(COMEX)·
   천연가스선물·비트코인선물)은 카탈로그가 승수·증거금·통화를 알아 엔진이 자동 인식 — IR엔
   자산클래스 표시 불필요, 종목처럼 universe.symbols에 이름만 넣는다(주식과 같은 신호·청산 어휘).
   · 단발 룰("선물이 ___면 롱, N% 손절"): single + on_signal(condition) — 증거금 레버리지 *보유*
     포지션으로 진입, take_profit/stop_loss(%)·hold_days·매도조건으로 청산(보유형이라 vol drag 없음).
   · 추세추종 보유/팩터: single+always(보유 마스크, 신호 참인 동안 보유) 또는 scheduled(daily).
   · 숏(단방향): on_signal+condition+direction="short"(선물 sell-to-open). 양방향(조건별 롱/숏)은
     부호점수+long_short(레시피 1) — 이벤트/당일이면 entry.mode="on_signal"(라이브 양방향 가능·당일은
     hold_days=0), 정기 리밸런스/추세 보유면 scheduled(daily). (선물 숏은 차입 불필요·대칭.)
     레버리지는 증거금으로 내재 → leverage=1 기본(명목 더 키울 때만 >1).
   · ⚠ roll_method·series_adjust·roll_cost_pct·account_currency는 **현재 엔진 미적용(예약)** — 채우지
     말 것(단일 연속 시계열·단일통화 가정). 사용자가 강하게 명시하면 채우되 "현재 미적용"을 assumptions에 명시.
7. [스크리닝(현 시점 종목 선별)] "저평가 X 상위 N개"·"조건 맞는 종목 골라줘"처럼 *백테스트 손익이
   아니라 지금 시점 종목 리스트*가 답이면 → query="select" + signal=랭킹 score(예: 낮은 PBR이면
   data(__SELF__.pb_ratio)) + universe.kind=all + universe.screener.condition=
   is_in(attribute("Sector"), ["반도체"], match="contains") 같은 섹터/자격 필터 + select={{top_n:N,
   descending:false(저평가=낮은값 우선)·true(높은값 우선), display:[pb_ratio, ...](근거 지표)}}.
   ⚠ 섹터/업종 필터는 match="contains" 필수 — 분류 데이터가 KSIC 자유서술("반도체 제조업")이라
   정확매칭(기본 exact)이면 "반도체"는 0건이 된다. 버킷·국면 등 정확 라벨 필터에는 match 생략(exact).
   (※ 레시피 3은 *정기 리밸런싱 백테스트*(simulate), 본 레시피는 *현 시점 스냅샷 선별*(select) — 둘 구분.)
8. [단일종목 360 리포트] "삼성전자 어때"·"이 종목 분석/요약"처럼 *한 종목의 현황*이 답이면 →
   query="describe" + universe.kind="single" + symbols=[그 종목] + signal=data("__SELF__.Close")
   (분석 동사라 신호는 명목 — 엔진이 가격·수익·리스크·밸류·섹터를 자동 조립). study 불필요.
   (※ '왜 올랐나/성장전망/실적후확률'은 뉴스·추정치·이벤트 데이터 필요 — 아직 미지원이면 assumptions에 명시.)
9. [포트폴리오 진단] "내 포트폴리오 진단"·"보유종목 집중·리스크 봐줘"처럼 *보유 집합의 진단*이면 →
   query="describe" + universe.kind="portfolio" + symbols=[보유들] + (보유 비중 알면 universe.weights={{종목:비중}}, 없으면 동일가중)
   + signal=data("__SELF__.Close")(명목). 엔진이 집중도(HHI)·섹터노출·가중밸류·포트 변동성 산출. study 불필요.
10. [최적 파라미터/종목 찾기(extremize)] "샤프(또는 수익률·CAGR)를 *최대화*하는 [기간/임계값/top_n] 찾아줘"·
    "어떤 종목이 제일 나은가"처럼 *그리드 중 최적 1개*가 답이면 → study.axis="parameter"(+param_grid)
    또는 "entity"(+assets) + study.reduction="extremize" + study.objective={{metric, direction, oos_guard:true}}.
    metric은 sharpe(기본)·sortino·cagr·cum_return·mdd만. ⚠ mdd는 음수라 "낙폭 최소"=direction:"max".
    oos_guard=true(기본)면 최적값을 시간폴드로 재검(과최적화 경고). (※ enumerate=모든 셀 나열, extremize=최적 1개.)
11. [다중팩터 횡단 회귀] "밸류·모멘텀·퀄리티 중 무엇이 forward 수익을 설명하나(상호 통제)"·"여러 지표로
    수익 횡단 회귀"처럼 *여러 설명변수의 동시 예측력*이면 → query="relate" + study.relation_kind="regression"
    + study.factors=[팩터1, 팩터2, ...](각 score 블록) + study.windows. universe.kind=all/list(종목 2+).
    Fama-MacBeth(날짜별 횡단 OLS→계수 시계열 평균+t값/신뢰구간). (※ 단일팩터 예측력=relation_kind="ic"+target_node.)
12. [상관행렬] "A와 B(와 C)의 상관계수"·"이 종목들 상관관계/같이 움직이나"·"분산투자 되나·헤지 후보"처럼
    *종목 간 수익 동조성*이면 → query="relate" + study.relation_kind="correlation" + universe.kind="list"(종목 2+).
    target_node·factors 불필요(가격수익 공행렬). windows=[N]으로 최근 N거래일 한정 가능(없으면 전체). 결과=상관 히트맵.
13. [포트폴리오 비중 추천] "이 종목들 어떻게 배분/비중 얼마"·"포트폴리오 추천(종목+비중)"·"분산 최적 비중"처럼
    *비중 처방*이면 → query="prescribe" + universe.kind="list"(종목 2+). 위험기반(최소분산·리스크패리티·동일가중)
    +최대샤프를 동시 산출(결과=비중 트리맵). 종목당 상한=prescribe.max_weight, 추정기간=prescribe.window. signal은 명목(data Close).
14. [시장 breadth] "코스피 왜 빠져/올라"·"시장 분위기·장세 어때"·"상승하락 종목 수/시장 폭"처럼 *시장 전반 상태*면
    → query="breadth" + universe.kind="all"(전체) 또는 list(대표 종목군). 상승하락 비율·평균수익·MA 상회·섹터별 약강세.
    *왜*(거시·뉴스 인과)는 엔진이 아니라 사이드카·해석이 보강 — breadth는 시장 폭의 what을 결정적으로 제공. signal은 명목.
</idioms>

<process>
emit_strategy를 호출하되, 인자 필드를 반드시 아래 순서로 채워 추론한다(순서가 곧 사고 과정):
1. intent_summary: 입력을 '무엇을·언제·어떻게 사고팔까'의 평문 전략으로 한두 문장 재진술.
   ⚠ 입력이 상품의 작동원리·수식·메커니즘 설명이어도, 그것을 '그 상품을 복제하는 매매 주문'으로
      환원한다(예: 레버리지 ETF의 일일 리밸런싱 수식 → "노출을 순자산의 2배로 매일 유지").
2. strategy_archetype: 이벤트룰 / 정기리밸런싱팩터 / 상수레버리지·상시보유 / 스크리너선별 / 롱숏 등.
3. mapping_rationale: intent의 각 요소를 <capabilities>·<idioms>의 어느 것으로 매핑했는지 근거.
   ⚠ expressible=false 또는 *단순화*(한쪽 다리 드롭 등)를 결론짓기 전에 반드시 <idioms> 쿡북과
   <capabilities> '쓰임'을 대조한다. 특히 양방향(롱/숏)·중립밴드·국면조건부는 <idioms>에 합성 레시피가
   있으므로 단방향으로 축소하지 말 것.
4. strategy: 위 매핑을 StrategyIR JSON으로.
5. assumptions: 모호하게 해석한 부분 + **결과에 영향 주는 단순화·생략을 반드시 명시**(예: 한쪽 다리 드롭,
   임계 임의 선정, 이벤트→레벨 근사). 조용히 줄이지 말고 그 사실을 한 줄로 적는다.
6. expressible: <capabilities>·<idioms> 조합으로도 매핑 경로가 없을 때만 false(이때 strategy는 {{}}).
   매핑 경로가 하나라도 있으면 반드시 true. (안전망: 검증기가 잘못된 IR을 거르고 유저가 최종 확인하므로,
   "근사 가능하면" 표현을 시도하라 — 섣불리 표현불가로 포기하지 말 것.)
</process>

<rules>
- 검증 실패 tool_result를 받으면, 그 오류만 고쳐 emit_strategy를 다시 호출한다(유효 op·지표 ref·구조만).
- 단일 종목 상시보유의 신호는 항상 참 condition(compare(종가>0))으로 둔다.
- 청산 규칙(exit)은 on_signal·scheduled에만 의미; always(상시)에선 무시되므로 비운다.
- '종가 부근/종가에' 체결 = simulation.fill="close".
</rules>

<examples>
{examples}
</examples>"""


_EMIT_TOOL = {
    "name": "emit_strategy",
    "description": "단계별 추론(intent→archetype→mapping)을 거쳐 컴파일된 StrategyIR을 제출한다. "
                   "검증 실패 tool_result를 받으면 오류를 고쳐 다시 호출.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent_summary": {"type": "string",
                               "description": "1단계: 입력을 '무엇을·언제·어떻게 사고팔까'의 평문 전략으로 재진술. 메커니즘/수식 설명이면 그 이면의 매매 의도로 환원."},
            "strategy_archetype": {"type": "string",
                                   "description": "2단계: 전략 유형(이벤트룰/정기리밸런싱팩터/상수레버리지·상시보유/스크리너선별/롱숏 등)."},
            "mapping_rationale": {"type": "string",
                                  "description": "3단계: intent 각 요소를 capabilities의 어느 구성요소로 매핑했는지 근거. 표현불가 결론 전 capabilities '쓰임'과 대조."},
            "strategy": {"type": "object",
                         "description": "4단계: 매핑을 StrategyIR JSON으로. expressible=false면 빈 객체."},
            "assumptions": {"type": "array", "items": {"type": "string"},
                            "description": "5단계: 모호하게 해석한 부분(한국어)."},
            "expressible": {"type": "boolean",
                            "description": "6단계: capabilities 조합으로도 매핑 경로가 없을 때만 false. 경로가 있으면 반드시 true."},
        },
        "required": ["intent_summary", "strategy_archetype", "mapping_rationale",
                     "strategy", "assumptions", "expressible"],
    },
}


def _resolve_symbols(strat: dict, valid_keys: set[str], name_map: dict[str, str]) -> dict:
    """universe.symbols의 이름→데이터셋 키 해석. 유효 키면 그대로, 이름이면 name_map으로,
    둘 다 아니면 원문 유지(검증기가 R0로 잡음)."""
    u = strat.get("universe")
    if isinstance(u, dict) and isinstance(u.get("symbols"), list):
        u["symbols"] = [
            s if (s := str(x).strip()) in valid_keys else name_map.get(s.lower(), s)
            for x in u["symbols"]
        ]
    return strat


def _route_directional(strat: dict) -> dict:
    """결정적 라우팅 정규화(M5d) — 부호방향 long_short 당일매매를 on_signal로 보장.

    long_short 당일매매(hold_days=0)는 scheduled 리밸런스 경로가 종가청산을 못 해
    backtest≠live가 된다(엔진은 on_signal 경로에서만 hold_days==0 당일청산 — engine.py:409).
    랭킹(top_n/top_pct)이 아닌 부호방향 long_short + hold_days==0이면 entry.mode를
    on_signal로 강제한다 — LLM이 옛 쿡북 습관으로 scheduled를 내도 라이브 가능 경로로
    수렴시킨다(엔진 _direction_for가 score 부호로 방향 결정). 랭킹·비당일은 불변
    (기존 scheduled 팩터/TSMOM 보존). 결정적이라 LLM 변동과 무관하게 재현."""
    pos = strat.get("position")
    if not isinstance(pos, dict) or pos.get("direction") != "long_short":
        return strat
    entry = pos.get("entry")
    if not isinstance(entry, dict):
        return strat
    is_ranking = entry.get("top_n") is not None or entry.get("top_pct") is not None
    hold_days = (pos.get("exit") or {}).get("hold_days")
    if not is_ranking and hold_days == 0:
        entry["mode"] = "on_signal"
        if entry.get("threshold") is None:
            # 부호방향의 임계를 명시(0) — None은 소비층(_select 랭킹 추락 vs
            # _direction_for 0.0)마다 다르게 해석돼 양방향 동시 후보 사고를 냈다.
            entry["threshold"] = 0.0
    return strat


def _force_attribute_filter_contains(node):
    """is_in(attribute(...)) 섹터/업종 필터를 부분일치(match="contains")로 결정적 강제.

    분류 데이터(FDR KRX-DESC)는 섹터/업종을 KSIC 자유서술("반도체 제조업")로 저장하는데,
    LLM은 사용자어("반도체")로 emit한다 → 정확매칭이면 0건(eligible_size=0, "저평가 반도체주"
    빈 결과의 근본원인). attribute 라벨은 항상 자유서술이라 contains가 옳다(exact는 사실상 무용).
    프롬프트 안내와 별개로 결정적이라 LLM 변동과 무관하게 재현(IR 트리 전체를 in-place 정규화)."""
    if isinstance(node, dict):
        if node.get("op") == "is_in":
            sig = (node.get("inputs") or {}).get("signal")
            if isinstance(sig, dict) and sig.get("op") == "attribute":
                node.setdefault("params", {})["match"] = "contains"
        for v in node.values():
            _force_attribute_filter_contains(v)
    elif isinstance(node, list):
        for v in node:
            _force_attribute_filter_contains(v)
    return node


def compile_nl(
    nl: str,
    *,
    catalog: list[dict],
    capabilities: dict,
    indicator_cols: list[str],
    valid_keys: set[str],
    name_map: dict[str, str],
    validate_fn: Callable[[dict], tuple[list[dict], bool]],
    max_repairs: int = 2,
) -> dict:
    """자연어 → StrategyIR. validate_fn(strategy_dict)->(issues, ok). 내부 수리 루프.

    반환: {success, ir, assumptions, issues, repair_count, error?}.
    """
    if not settings.ANTHROPIC_API_KEY:
        return {"success": False, "error": "ANTHROPIC_API_KEY가 설정되지 않았습니다 — 서버 환경변수에 키를 설정하세요.",
                "ir": {}, "assumptions": [], "issues": [], "repair_count": 0}
    try:
        import anthropic
    except ImportError:
        return {"success": False, "error": "anthropic 패키지가 설치되지 않았습니다 (pip install anthropic).",
                "ir": {}, "assumptions": [], "issues": [], "repair_count": 0}

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    # 정적·대형 시스템 프롬프트는 프롬프트 캐싱(ephemeral)으로 호출당 비용·지연 절감.
    system = [{"type": "text",
               "text": _system_prompt(catalog, capabilities, indicator_cols),
               "cache_control": {"type": "ephemeral"}}]
    messages: list[dict] = [{"role": "user", "content": f"다음 전략을 컴파일해줘:\n\n{nl}"}]
    last: dict = {"ir": {}, "assumptions": [], "issues": [], "repair_count": 0}

    for attempt in range(max_repairs + 1):
        try:
            resp = client.messages.create(
                model=settings.NL_COMPILE_MODEL, max_tokens=4096, system=system,
                tools=[_EMIT_TOOL], tool_choice={"type": "tool", "name": "emit_strategy"},
                messages=messages)
        except Exception as e:  # noqa: BLE001 — 외부 API 실패는 사유 반환
            return {"success": False, "error": f"LLM 호출 실패: {type(e).__name__}: {e}", **last}

        tu = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if tu is None:
            return {"success": False, "error": "LLM이 emit_strategy를 호출하지 않았습니다.", **last}

        inp = dict(tu.input or {})
        assumptions = inp.get("assumptions") or []
        if inp.get("expressible") is False:
            return {"success": False, "error": "이 전략은 현재 IR 구조로 표현할 수 없습니다.",
                    "ir": {}, "assumptions": assumptions, "issues": [], "repair_count": attempt}

        strat = _resolve_symbols(dict(inp.get("strategy") or {}), valid_keys, name_map)
        strat = _route_directional(strat)          # M5d 결정적 라우팅(부호방향 당일매매→on_signal)
        _force_attribute_filter_contains(strat)    # 섹터/업종 필터 부분일치 강제(반도체→반도체 제조업)
        issues, ok = validate_fn(strat)
        last = {"ir": strat, "assumptions": assumptions, "issues": issues, "repair_count": attempt}
        if ok:
            return {"success": True, **last}
        if attempt == max_repairs:
            break

        errs = [i for i in issues if i.get("is_error")]
        messages.append({"role": "assistant",
                         "content": [{"type": "tool_use", "id": tu.id, "name": tu.name, "input": inp}]})
        messages.append({"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tu.id,
            "content": ("검증 실패. 아래 오류를 모두 고쳐 emit_strategy를 다시 호출해줘"
                        "(유효한 op·지표 ref·구조만 사용):\n"
                        + json.dumps(errs, ensure_ascii=False))}]})

    return {"success": False, "error": "검증을 통과하는 IR을 생성하지 못했습니다.", **last}
