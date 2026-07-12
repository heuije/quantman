"""StrategyIR → 사람이 읽는 백테스트 구성 설명 (MECE 버킷별).

전략을 입력/편집하면 "정확히 어떤 가정·설정으로 백테스트가 구성되는지"를
8(+1) 버킷으로 분해해 항목별로 설명한다. 각 항목 = 해석된 값 + 출처 + 한 줄 의미.

  버킷: ① 유니버스 ② 진입 신호 ③ 진입 실행 ④ 사이징 ⑤ 청산 ⑥ 리스크 오버레이
        ⑦ 체결·비용 ⑧ 자본·기간·데이터  (+⑨ 분석 차원 — sweep 요청 시에만)

출처(provenance):
  - "지정": 유저가 정함(스키마 기본과 다른 값, 또는 신호 같은 필수 논리).
  - "기본": 유저가 안 정해 엔진 기본값 적용 — 특히 비용·체결·자본 등 *보이지 않던* 가정.
  (LLM이 유추한 가정 "[추론]"은 컴파일러의 assumptions 리스트로 별도 표시 — 여기 포함 안 함.)

의미 텍스트는 capability_spec()을 단일 출처로 재사용한다(중복 작성·드리프트 방지).
단위는 엔진(engine.py) 실제 적용과 일치: 비용(수수료·슬리피지·매도세)=분수(bps→소수),
take_profit/stop_loss/trail_pct/amount_pct/vol_target/낙폭/그룹캡=퍼센트
(엔진 cur_ret=(close-entry)/entry*100 과 직접 비교 — spec M-exit가 "(%)"로 못박음).
"""
from __future__ import annotations

from ..blocks.node import referenced_columns, referenced_symbols
from ..exec_defaults import DEFAULT_EXECUTION, is_futures
from .capabilities import capability_spec
from .spec import (Entry, Overlays, PositionSpec, SimSpec, Sizing, StrategyIR,
                   Study, Universe, signal_out_type)

_SET = "지정"
_DEF = "기본"

_CAP = capability_spec()

# 출처 판정 기준 — 스키마 기본 인스턴스(신호 불필요한 하위 모델만).
_D_UNI = Universe()
_D_POS = PositionSpec()
_D_SIM = SimSpec()
_D_STUDY = Study()


def _split_mode(ir: StrategyIR) -> str:
    """study(time_fold/folds)에서 옛 period_split 표시값을 복원 — 검증 방식 라벨·_does 키용.

    엔진은 split_dates 유무·folds(2=oos·4=walk_forward) 만 구분하므로 kfold는 walk_forward와
    동치로 묶인다(_SPLIT/capabilities도 동일 명시). time_fold 아니면 단일 구간(single).
    """
    st = ir.study
    if st.axis != "time_fold":
        return "single"
    return "oos" if st.folds == 2 else "walk_forward"


def _does(spec_key: str, value) -> str:
    """capability_spec의 enum 목록에서 value의 'does' 텍스트(없으면 '')."""
    for e in _CAP.get(spec_key, []):
        if isinstance(e, dict) and e.get("value") == value:
            return e.get("does", "")
    return ""


def _frac_pct(x: float) -> str:
    """분수 → 퍼센트 문자열. 0.001 → '0.1%', 0.0003 → '0.03%' (수수료·슬리피지 등 분수 비용)."""
    return f"{x * 100:g}%"


def _pct(x: float) -> str:
    """이미 퍼센트 단위인 값 → 문자열. 10 → '10%' (take_profit·stop_loss·trail_pct·vol_target·낙폭 등)."""
    return f"{x:g}%"


def _won(x: float) -> str:
    return f"{x:,.0f}원"


def _euro(word: str) -> str:
    """단어 뒤 조사 '(으)로' 선택 — 받침 없거나 ㄹ받침이면 '로', 그 외 '으로'."""
    if not word or not ("가" <= word[-1] <= "힣"):
        return "로"
    jong = (ord(word[-1]) - 0xAC00) % 28
    return "로" if jong in (0, 8) else "으로"


def _it(label: str, value: str, provenance: str, meaning: str = "") -> dict:
    return {"label": label, "value": value, "provenance": provenance, "meaning": meaning}


def _bucket(key: str, title: str, question: str, items: list[dict]) -> dict:
    return {"key": key, "title": title, "question": question, "items": items}


# ── ① 유니버스 ──────────────────────────────────────────────────────────────
def _universe(u: Universe) -> dict:
    items = []
    kind_label = {"single": "단일 종목", "list": "지정 바스켓",
                  "all": "전체 유니버스"}.get(u.kind, u.kind)
    items.append(_it("거래 대상", kind_label,
                     _SET if u.kind != _D_UNI.kind else _DEF, _does("universe_kind", u.kind)))
    if u.kind in ("single", "list") and u.symbols:
        items.append(_it("종목", ", ".join(u.symbols), _SET))
    if u.kind == "all":
        items.append(_it("매크로 지수 제외", "예" if u.exclude_macro else "아니오",
                         _SET if u.exclude_macro != _D_UNI.exclude_macro else _DEF))
    if u.screener:
        refresh = u.screener.get("refresh", "")
        val = f"적용 (갱신: {refresh})" if refresh else "적용"
        items.append(_it("2차 스크리너", val, _SET, _CAP.get("screener", {}).get("does", "")))
    else:
        items.append(_it("2차 스크리너", "없음", _DEF))
    return _bucket("universe", "① 유니버스 (거래 후보)", "어떤 종목이 거래 후보인가?", items)


# ── ② 진입 신호 ──────────────────────────────────────────────────────────────
def _signal(ir: StrategyIR) -> dict:
    sig = ir.signal
    st = signal_out_type(sig)
    type_label = {"condition": "룰 (조건 충족 시 진입)",
                  "score": "팩터 (점수로 종목 선택·랭킹)"}.get(st, st or "알 수 없음")
    items = [_it("신호 종류", type_label, _SET, "유저가 입력한 전략 논리의 핵심.")]
    cols = sorted(referenced_columns(sig))
    items.append(_it("사용 지표·가격", ", ".join(cols) if cols else "없음", _SET))
    syms = sorted(referenced_symbols(sig))
    if syms:
        items.append(_it("신호 내 참조 종목", ", ".join(syms), _SET))
    return _bucket("signal", "② 진입 신호 (트리거 논리)", "무엇을 보고 사기로 정하나?", items)


# ── ③ 진입 실행 ──────────────────────────────────────────────────────────────
_REBAL = {"daily": "매일", "weekly": "매주", "monthly": "매월",
          "quarterly": "분기마다", "annual": "매년"}


def _rebal_label(e: Entry) -> str:
    if e.rebalance == "every_n_days":
        return f"{e.every_n_days or '?'}거래일마다"
    return _REBAL.get(e.rebalance, e.rebalance)


def _selection_label(e: Entry) -> str:
    if e.top_n:
        return f"점수 상위 {e.top_n}종목"
    if e.top_pct:
        return f"점수 상위 {_pct(e.top_pct)}"
    if e.threshold is not None:
        return f"점수 {e.threshold:g} 이상 전부"
    return "조건 충족 종목 전부"


def _entry(pos: PositionSpec) -> dict:
    e = pos.entry
    dir_label = {"long": "매수(롱)만", "short": "공매도(숏)만",
                 "long_short": "롱·숏 양방향"}.get(pos.direction, pos.direction)
    items = [_it("방향", dir_label,
                 _SET if pos.direction != _D_POS.direction else _DEF, _does("direction", pos.direction))]
    mode_label = {"on_signal": "신호 발생 시 진입", "scheduled": "정기 리밸런싱",
                  "always": "매일 목표비중 유지"}.get(e.mode, e.mode)
    items.append(_it("진입 방식", mode_label,
                     _SET if e.mode != _D_POS.entry.mode else _DEF, _does("entry_mode", e.mode)))
    if e.mode == "scheduled":
        items.append(_it("리밸런싱 주기", _rebal_label(e), _SET, _does("rebalance", e.rebalance)))
        items.append(_it("종목 선택", _selection_label(e),
                         _SET if (e.top_n or e.top_pct or e.threshold is not None) else _DEF))
        items.append(_it("빈 슬롯 처리", {"cash": "현금 보유", "replace": "차순위 충원"}.get(e.refill, e.refill),
                         _SET if e.refill != _D_POS.entry.refill else _DEF, _does("refill", e.refill)))
    return _bucket("entry", "③ 진입 실행 (신호→포지션)",
                   "신호가 켜지면 어떻게·얼마나 자주 들어가나?", items)


# ── ④ 사이징 ────────────────────────────────────────────────────────────────
_SIZING = {"equal_weight": "동일가중", "signal_proportional": "신호 점수 비례",
           "vol_inverse": "변동성 역가중", "target_vol": "목표 변동성",
           "fixed_weight": "지정 비중", "fixed_amount": "종목당 고정금액",
           "pct_cash": "자본 대비 %"}


def _sizing(pos: PositionSpec, sim: SimSpec) -> dict:
    s = pos.sizing
    items = [_it("비중 방식", _SIZING.get(s.mode, s.mode),
                 _SET if s.mode != _D_POS.sizing.mode else _DEF, _does("sizing_mode", s.mode))]
    if s.mode == "pct_cash":
        items.append(_it("종목당 비중", _pct(s.amount_pct),
                         _SET if s.amount_pct != _D_POS.sizing.amount_pct else _DEF))
    if s.mode == "fixed_amount" and s.amount_krw:
        items.append(_it("종목당 금액", _won(s.amount_krw), _SET))
    if s.mode == "fixed_weight" and s.weights:
        items.append(_it("지정 비중", ", ".join(f"{k} {_pct(v)}" for k, v in s.weights.items()), _SET))
    if s.mode == "target_vol" and s.target_vol_pct:
        items.append(_it("목표 연변동성", _pct(s.target_vol_pct), _SET))
    if s.mode in ("vol_inverse", "target_vol"):
        items.append(_it("변동성 측정 창", f"{s.vol_window}일",
                         _SET if s.vol_window != _D_POS.sizing.vol_window else _DEF))
    lev = sim.leverage
    items.append(_it("레버리지", f"{lev:g}배" if lev != 1.0 else "없음 (1배)",
                     _SET if lev != _D_SIM.leverage else _DEF, _CAP.get("leverage", {}).get("does", "")))
    if s.max_position_pct != _D_POS.sizing.max_position_pct:
        items.append(_it("종목당 상한", _pct(s.max_position_pct), _SET))
    if s.futures_margin_pct != _D_POS.sizing.futures_margin_pct:
        items.append(_it("선물 증거금 사용률", _pct(s.futures_margin_pct), _SET,
                         "선물 진입 시 가용현금의 이 %를 증거금으로 사용 — 레버리지 안전상한(100%=full-margin)"))
    return _bucket("sizing", "④ 포지션 사이징 (얼마나)", "각 종목에 자본을 어떻게 배분하나?", items)


# ── ⑤ 청산 규칙 ──────────────────────────────────────────────────────────────
def _exit(pos: PositionSpec) -> dict:
    x = pos.exit
    if pos.entry.mode == "always":
        return _bucket("exit", "⑤ 청산 규칙 (나가는 조건)", "언제·왜 파나?",
                       [_it("청산", "always 모드 — 청산 규칙 미적용(매일 목표비중으로 되감음)", _DEF,
                            _CAP.get("exit", {}).get("does", ""))])
    items = []
    if x.take_profit is not None:
        items.append(_it("익절", _pct(x.take_profit), _SET))
    if x.stop_loss is not None:
        items.append(_it("손절", _pct(x.stop_loss), _SET))
    if x.hold_days is not None:
        _hd = "당일 종가 청산" if x.hold_days == 0 else f"{x.hold_days}거래일 후 청산"
        items.append(_it("보유 기간", _hd, _SET))
    if x.trail_pct is not None:
        items.append(_it("트레일링 스탑", _pct(x.trail_pct), _SET))
    if x.trail_atr_mult is not None:
        items.append(_it("ATR 트레일링", f"{x.trail_atr_mult:g}배", _SET))
    if x.condition is not None:
        items.append(_it("매도 신호", "별도 조건 블록 충족 시", _SET))
    if not items:
        items.append(_it("청산 규칙", "명시적 청산 규칙 없음 — 신호 이탈/리밸런싱으로만 청산", _DEF,
                         "⚠ on_signal 단발 진입에서 청산 규칙이 없으면 포지션이 오래 유지될 수 있음."))
    return _bucket("exit", "⑤ 청산 규칙 (나가는 조건)", "언제·왜 파나?", items)


# ── ⑥ 리스크 오버레이 ────────────────────────────────────────────────────────
def _overlays(ov: Overlays) -> dict:
    items = []
    if ov.vol_target is not None:
        items.append(_it("변동성 타게팅", f"연 {_pct(ov.vol_target)} 목표로 노출 조절", _SET))
    if ov.max_drawdown_stop is not None:
        soft = f" ({_pct(ov.max_drawdown_soft)}부터 디리스킹)" if ov.max_drawdown_soft is not None else ""
        items.append(_it("최대낙폭 청산", f"{_pct(ov.max_drawdown_stop)} 도달 시 전량{soft}", _SET))
    if ov.max_group_pct is not None:
        items.append(_it("그룹 노출 상한", _pct(ov.max_group_pct), _SET))
    if ov.turnover_damp is not None:
        items.append(_it("턴오버 억제", f"비중변동 {ov.turnover_damp:g} 미만 무시", _SET))
    if not items:
        items.append(_it("오버레이", "추가 포트폴리오 리스크 제어 없음", _DEF))
    return _bucket("overlays", "⑥ 리스크 오버레이 (포트폴리오 제어)",
                   "포트폴리오 전체에 거는 안전장치는?", items)


# ── ⑦ 체결·비용 가정 (가장 보이지 않던 silent 기본값) ────────────────────────
def _execution(sim: SimSpec, u: Universe) -> dict:
    items = []
    items.append(_it("체결 시점",
                     {"next_open": "신호 다음 거래일 시가", "close": "당일 종가",
                      "typical": "당일 (고+저+종)/3"}.get(sim.fill, sim.fill),
                     _SET if sim.fill != _D_SIM.fill else _DEF, _does("fill", sim.fill)))
    items.append(_it("체결 지연", f"신호 후 {sim.delay}거래일",
                     _SET if sim.delay != _D_SIM.delay else _DEF, "look-ahead(미래정보) 방지."))
    comm = sim.commission if sim.commission is not None else DEFAULT_EXECUTION["bt_commission_bps"] / 10_000.0
    items.append(_it("수수료", f"편도 {_frac_pct(comm)}",
                     _SET if sim.commission is not None else _DEF, "매수·매도 양방향 부과(KIS 위탁 기준)."))
    slip = sim.slippage if sim.slippage is not None else DEFAULT_EXECUTION["bt_slippage_bps"] / 10_000.0
    items.append(_it("슬리피지", f"편도 {_frac_pct(slip)}",
                     _SET if sim.slippage is not None else _DEF, "체결가 불리 가정."))
    # 매도세(증권거래세)는 주식 매도에만 — 선물은 면제(엔진 stx=0). 유니버스 자산클래스에 맞춰 표기.
    tax = sim.sell_tax if sim.sell_tax is not None else DEFAULT_EXECUTION["bt_sell_tax_bps"] / 10_000.0
    _fut_n = sum(1 for x in u.symbols if is_futures(x))
    if u.symbols and _fut_n == len(u.symbols):
        items.append(_it("매도세", "면제 (선물)", _DEF, "선물은 증권거래세 없음 — 백테스트도 미부과."))
    elif _fut_n:                                   # 주식+선물 혼합
        items.append(_it("매도세", f"{_frac_pct(tax)} (주식 다리만)",
                         _SET if sim.sell_tax is not None else _DEF,
                         "거래세+농특세, 주식 매도 시에만 — 선물 다리는 면제."))
    else:
        items.append(_it("매도세", _frac_pct(tax),
                         _SET if sim.sell_tax is not None else _DEF, "거래세+농특세, 매도 시에만."))
    if sim.short_borrow_pct is not None:
        items.append(_it("공매도 차입비용", f"연 {_pct(sim.short_borrow_pct)}", _SET))
    if sim.funding_cost_pct is not None:
        items.append(_it("차입(레버리지) 비용", f"연 {_pct(sim.funding_cost_pct)}", _SET))
    return _bucket("execution", "⑦ 체결·비용 가정", "어떤 가격·비용으로 체결된다고 가정하나?", items)


# ── ⑧ 자본·기간·데이터 ──────────────────────────────────────────────────────
_SPLIT = {"single": "단일 구간 1회", "walk_forward": "시간순 4분할 일관성 점검",
          "oos": "인/아웃 2분할", "kfold": "시간순 4분할(walk_forward와 동일)"}


def _environment(sim: SimSpec, u: Universe, split_mode: str) -> dict:
    items = [_it("초기 자본", _won(sim.initial_capital),
                 _SET if sim.initial_capital != _D_SIM.initial_capital else _DEF)]
    if sim.start or sim.end:
        items.append(_it("기간", f"{sim.start or '데이터 시작'} ~ {sim.end or '데이터 끝'}", _SET))
    else:
        items.append(_it("기간", "데이터 전체 기간", _DEF))
    # 주의: split_mode(single/oos/walk_forward)는 capabilities enum이 아니다 — 과거 존재하지
    # 않는 'period_split' 키를 _does로 조회해 무성 빈 문자열이었다(조용한 손동기화 부패의
    # 실측 표본). 설명은 _SPLIT(표시 계층 소유)만 쓴다.
    items.append(_it("검증 방식", _SPLIT.get(split_mode, split_mode),
                     _SET if split_mode != "single" else _DEF))
    items.append(_it("데이터 빈도", "일봉 (일별 OHLC)", _DEF, "분/틱 단위 아님."))
    items.append(_it("데이터 처리", "종목별 시장 소스 1개 사용 · 배당/생존편향 등 세부 처리는 데이터 레이어에 의존", _DEF))
    # 선물 연속물·롤 — 심볼별 정직성. 만기물 패널 보유(KOSPI200)는 롤/조정을 백테스트에 반영,
    # 미보유(원유 등 yfinance)는 단일 연속 시계열 가정(미적용). LLM이 "만기 자동 롤"이라 해도
    # 실제 반영 여부를 패널 존재로 판정해 거짓 약속을 차단한다. 통화 환산(FX)은 여전히 미적용.
    if u.kind in ("single", "list") and any(is_futures(x) for x in u.symbols):
        from ..data_fetcher import has_futures_panel
        fut = [x for x in u.symbols if is_futures(x)]
        if any(has_futures_panel(x) for x in fut):
            rm = sim.roll_method or "at_expiry(기본)"
            sa = sim.series_adjust or "none(기본)"
            items.append(_it("선물 연속물·롤", f"만기물 패널로 연속물 구성 — 롤={rm}, 조정={sa} (백테스트 반영)",
                             _SET if (sim.roll_method or sim.series_adjust) else _DEF,
                             "만기=패널 마지막 존재일(데이터 주도). roll_cost_pct는 실제 베이시스 우선이라 무시."))
        if any(not has_futures_panel(x) for x in fut):
            reserved = [k for k, v in (("만기 롤", sim.roll_method), ("연속물 조정", sim.series_adjust),
                                       ("롤비용", sim.roll_cost_pct)) if v is not None]
            set_note = f" (설정한 {', '.join(reserved)}은 현재 미반영)" if reserved else ""
            items.append(_it("선물 연속물·롤", "단일 연속 시계열 가정 — 만기물 패널 미수집 선물은 롤·연속물 조정 미적용" + set_note,
                             _DEF, "롤·만기물 전환은 만기물 패널이 있어야 반영됨(현재 KOSPI200)."))
        if sim.account_currency == "USD" or any(instrument_currency_usd(x) for x in u.symbols):
            items.append(_it("선물 통화 환산", "현재 미적용 — 단일통화 백테스트 가정(손익률은 통화 스케일에 불변)",
                             _DEF, "해외선물 USD↔KRW 환율 환산은 아직 미구현."))
    return _bucket("environment", "⑧ 자본·기간·데이터", "어떤 자본·기간·데이터 위에서 도나?", items)


def instrument_currency_usd(symbol: str) -> bool:
    """해외(USD) 선물 여부 — 통화 환산 미적용 안내 트리거용."""
    from ..exec_defaults import instrument_spec
    s = instrument_spec(symbol)
    return s.asset_class == "futures" and s.currency == "USD"


# ── ⑨ 분석 차원 (펼침 요청 시에만) ───────────────────────────────────────────
def _display_axis_target(ir: StrategyIR) -> tuple[str, str]:
    """신 query/study에서 옛 (sweep_axis, sweep_target) 표시쌍 복원(capabilities does-텍스트 키).

    describe→(none,signal)·relate+event→(time,return)·relate(IC)→(none,relation)·
    simulate는 study.axis로(parameter→parameter·entity→asset·label→condition·그 외 none).
    """
    st = ir.study
    if ir.query == "describe":
        return "none", "signal"
    if ir.query == "relate":
        return ("time", "return") if st.event is not None else ("none", "relation")
    return {"parameter": "parameter", "entity": "asset",
            "label": "condition"}.get(st.axis, "none"), "return"


# 표시 계층 소유 라벨 — _display_axis_target의 옛 표시쌍(asset·condition·time 등)은 capabilities
# enum 어휘가 아니라서 _does 조회가 항상 무성 빈 문자열 → raw enum이 그대로 노출됐다(조용한
# 손동기화 부패 실측 표본). capabilities 키를 조회하려면 값 어휘까지 일치해야 한다 — 여기 값은
# 표시 전용이므로 명시 맵을 표시 계층이 소유한다.
_AXIS_LABEL = {"parameter": "파라미터 격자 — 값 조합별 재실행해 비교",
               "asset": "종목별 — 자산마다 개별 실행해 비교",
               "condition": "라벨 분할 — 그룹별 기여를 나눠 비교",
               "time": "이벤트 시간축 — 발생 이후 경과일별 경로"}
_TARGET_LABEL = {"return": "전략(이벤트) 수익률", "signal": "신호값 분포",
                 "relation": "신호↔미래수익 관계(IC)"}


def _analysis(ir: StrategyIR) -> dict | None:
    axis, target = _display_axis_target(ir)
    if axis == "none":            # 옛 sweep.axis=="none"(단일·기간분할·IC·신호분포)은 분석 버킷 없음
        return None
    items = [_it("분석 축", _AXIS_LABEL.get(axis, axis), _SET),
             _it("측정 대상", _TARGET_LABEL.get(target, target), _SET)]
    if ir.study.label is not None:
        items.append(_it("분할 라벨", "그룹 라벨 블록(섹터·국면 등)으로 분할", _SET))
    return _bucket("analysis", "⑨ 분석 차원 (결과 펼침)", "단일 결과인가, 쪼개 보나?", items)


def _research_narrative(ir: StrategyIR) -> str | None:
    """리서치 질의(select/describe/relate/extremize)는 백테스트가 아니라 분석 — 동사별 서술.

    simulate(extremize 제외)면 None → 기존 백테스트 서술로 폴백. explain_ir이 select·리포트·
    진단·회귀를 "…일봉 백테스트합니다"로 오서술하던 문제(프로덕션 확인)를 동사별로 교정한다.
    """
    u, st = ir.universe, ir.study

    def _uni() -> str:
        if u.kind == "single" and u.symbols:
            return u.symbols[0]
        if u.kind in ("list", "portfolio") and u.symbols:
            head = ", ".join(u.symbols[:4]) + ("…" if len(u.symbols) > 4 else "")
            return f"{len(u.symbols)}종목({head})"
        return "전체 종목"

    def _cols(node) -> str:
        c = sorted(referenced_columns(node)) if node is not None else []
        return ", ".join(c) if c else "지정 신호"

    if ir.query == "select":
        sel = ir.select
        n = (f"상위 {sel.top_n}종목" if sel and sel.top_n
             else f"상위 {_pct(sel.top_pct)}" if sel and sel.top_pct else "상위 종목")
        asof = sel.as_of if (sel and sel.as_of and sel.as_of != "latest") else "현 시점"
        order = "낮은" if (sel and not sel.descending) else "높은"
        return (f"{_uni()} 중 {_cols(ir.signal)} 점수가 {order} 순으로 {asof} 스냅샷에서 {n}을 "
                f"선별합니다 — 스크리닝(현 시점 종목 리스트)이며 백테스트 손익은 산출하지 않습니다.")

    if ir.query == "describe":
        if u.kind == "single":
            sym = u.symbols[0] if u.symbols else "이 종목"
            return (f"{sym} 한 종목의 가격·기간수익·리스크(변동성·낙폭)·밸류·섹터 현황을 요약합니다 "
                    f"— 손익 백테스트가 아니라 현황 리포트입니다.")
        if u.kind == "portfolio":
            return (f"보유 {len(u.symbols)}종목의 집중도(HHI)·섹터 노출·가중 밸류·포트 변동성을 "
                    f"진단합니다 — 손익 백테스트가 아니라 포트폴리오 진단입니다.")
        return (f"{_uni()}의 {_cols(st.target_node)} 신호값 분포를 (선택 시 국면별로) 살펴봅니다 "
                f"— 손익이 아니라 신호값 자체의 분포입니다.")

    if ir.query == "relate":
        if st.event is not None:
            # WS1: 음수 창=발생 *이전*(전조) 구간 1급 지원 — 계약(contracts.py relate.event_study
            # does)과 동일 사실을 서술한다(종전 '발생 이후만·전조 아님' 문구는 stale·오도).
            wl = [int(w) for w in (st.windows or [5, 10, 20])]
            wins = "·".join(f"{w:+d}" for w in wl)
            if all(w < 0 for w in wl):
                path_txt = "발생 *이전*(전조) 구간"
            elif any(w < 0 for w in wl):
                path_txt = "발생 전·후 구간"
            else:
                path_txt = "발생 *이후* 경로"
            return (f"{_uni()}에서 이벤트 {path_txt} 경과일별({wins}일) 수익"
                    f"(평균·최대낙폭/상승)을 봅니다 — 분석 전용(미래참조).")
        if st.relation_kind == "regression":
            fn = []
            for fac in (st.factors or []):
                c = sorted(referenced_columns(fac))
                fn.append(c[0] if c else "팩터")
            fl = ", ".join(fn) if fn else "설명변수"
            return (f"{_uni()}에서 {fl} 다중 팩터의 forward 수익 횡단 회귀(Fama-MacBeth 계수·t값·"
                    f"신뢰구간)로 예측력을 봅니다 — 분석 전용(미래참조), 손익 아님.")
        return (f"{_uni()}에서 {_cols(st.target_node)}의 forward 수익 횡단 예측력(IC)을 봅니다 "
                f"— 분석 전용(미래참조).")

    if st.reduction == "extremize":          # simulate + 최적화
        obj = ir.study.objective
        metric = obj.metric if obj else "sharpe"
        direction = "최대" if (not obj or obj.direction == "max") else "최소"
        axis_word = "파라미터 격자" if st.axis == "parameter" else "종목"
        return (f"{axis_word}를 바꿔가며 {metric}를 {direction}화하는 최적 설정을 찾고, "
                f"시간폴드 OOS 일관성으로 과최적화 위험을 점검합니다.")
    return None


def _narrative(ir: StrategyIR) -> str:
    """전략 전체를 흐르는 한국어 한 문단으로 묘사 — 직관적 요약(MECE 항목의 산문 버전)."""
    research = _research_narrative(ir)
    if research is not None:
        return research
    u, pos, sim = ir.universe, ir.position, ir.simulation
    e, x, s = pos.entry, pos.exit, pos.sizing
    st = signal_out_type(ir.signal)
    cols = sorted(referenced_columns(ir.signal))
    ind = ", ".join(cols) if cols else "지정한 신호"
    dir_word = {"long": "매수(롱)", "short": "공매도(숏)",
                "long_short": "롱·숏 양방향"}.get(pos.direction, pos.direction)

    if u.kind == "single" and u.symbols:
        uni = f"{u.symbols[0]}"
    elif u.kind == "list" and u.symbols:
        head = ", ".join(u.symbols[:4]) + ("…" if len(u.symbols) > 4 else "")
        uni = f"지정한 {len(u.symbols)}종목({head})"
    else:
        uni = "전체 종목"

    sents = []
    if e.mode == "scheduled":
        if e.top_n:
            sel = f"상위 {e.top_n}종목"
        elif e.top_pct:
            sel = f"상위 {_pct(e.top_pct)}"
        elif e.threshold is not None:
            sel = f"점수 {e.threshold:g} 이상 종목"
        else:
            sel = "조건 충족 종목 전부"
        kind_word = "점수" if st == "score" else "신호"
        sents.append(f"이 전략은 {uni} 중 {ind} {kind_word} 기준 {sel}을 "
                     f"{_rebal_label(e)} 리밸런싱해 {dir_word} 보유합니다.")
    elif e.mode == "always":
        sents.append(f"이 전략은 {uni}을 {ind} 기준 비중으로 매일 되감아 "
                     f"{dir_word} 노출을 상시 유지합니다.")
    else:  # on_signal
        sents.append(f"이 전략은 {uni}에서 {ind} 조건이 충족되면 {dir_word}로 진입합니다.")

    size_word = _SIZING.get(s.mode, s.mode)
    size_sent = f"비중은 {size_word}"
    tail = size_word
    if sim.leverage != 1.0:
        size_sent += f", 레버리지 {sim.leverage:g}배"
        tail = f"{sim.leverage:g}배"
    sents.append(size_sent + _euro(tail) + " 배분합니다.")

    if e.mode != "always":
        bits = []
        if x.take_profit is not None:
            bits.append(f"{_pct(x.take_profit)} 익절")
        if x.stop_loss is not None:
            bits.append(f"{_pct(x.stop_loss)} 손절")
        if x.hold_days is not None:
            bits.append("당일 종가 청산" if x.hold_days == 0
                        else f"{x.hold_days}거래일 보유 후 청산")
        if x.trail_pct is not None:
            bits.append(f"{_pct(x.trail_pct)} 트레일링 스탑")
        if x.condition is not None:
            bits.append("별도 매도 신호")
        if bits:
            sents.append("청산은 " + "·".join(bits) + " 규칙으로 합니다.")
        elif e.mode == "scheduled":
            sents.append("개별 청산 규칙 없이, 리밸런싱에서 신호가 빠지면 교체합니다.")
        else:
            sents.append("⚠ 명시적 청산 규칙이 없어 포지션이 오래 유지될 수 있습니다.")

    fill_word = {"next_open": "다음 거래일 시가", "close": "당일 종가",
                 "typical": "당일 평균가"}.get(sim.fill, sim.fill)
    comm = sim.commission if sim.commission is not None else DEFAULT_EXECUTION["bt_commission_bps"] / 10_000.0
    slip = sim.slippage if sim.slippage is not None else DEFAULT_EXECUTION["bt_slippage_bps"] / 10_000.0
    tax = sim.sell_tax if sim.sell_tax is not None else DEFAULT_EXECUTION["bt_sell_tax_bps"] / 10_000.0
    period = f"{sim.start or '데이터 시작'}~{sim.end or '데이터 끝'}" if (sim.start or sim.end) else "데이터 전체 기간"
    split_mode = _split_mode(ir)
    sents.append(f"체결은 {fill_word} 기준이고, 비용은 수수료 편도 {_frac_pct(comm)}·"
                 f"슬리피지 {_frac_pct(slip)}·매도세 {_frac_pct(tax)}를 가정하며, "
                 f"초기 자본 {_won(sim.initial_capital)}으로 {period}을 "
                 f"{_SPLIT.get(split_mode, split_mode)} 일봉 백테스트합니다.")

    disp_axis, _ = _display_axis_target(ir)
    if disp_axis != "none":
        axis_word = {"condition": "그룹(섹터·국면 등)별", "parameter": "파라미터 값별",
                     "asset": "종목별", "time": "이벤트 시점별"}.get(disp_axis, disp_axis)
        sents.append(f"결과는 {axis_word}로 나눠 비교합니다.")

    return " ".join(sents)


def explain_ir(ir: StrategyIR, assumptions: list[str] | None = None) -> dict:
    """StrategyIR → 설명 문서: 직관적 prose 요약 + MECE 버킷별 분해.

    반환: {"summary": str(한 문단 묘사), "buckets": [...], "assumptions": [...]}.
    buckets는 항상 ①~⑧ + (펼침 축이 있으면) ⑨. assumptions는 컴파일러가 준
    [추론] 가정 리스트(있으면 그대로 통과 — UI가 별도 밴드로 표시)."""
    pos, sim = ir.position, ir.simulation
    buckets = [
        _universe(ir.universe),
        _signal(ir),
        _entry(pos),
        _sizing(pos, sim),
        _exit(pos),
        _overlays(pos.overlays),
        _execution(sim, ir.universe),
        _environment(sim, ir.universe, _split_mode(ir)),
    ]
    analysis = _analysis(ir)
    if analysis is not None:
        buckets.append(analysis)
    return {"summary": _narrative(ir), "buckets": buckets,
            "assumptions": list(assumptions or [])}
