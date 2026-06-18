"""NL→IR 컴파일 archetype 평가(수동 실행 — 실제 Anthropic 호출, CI 아님).

관용구 쿡북·표현가능성 규율의 효과를 데이터로 본다. 실패가 몰리는 archetype을 찾아
쿡북/few-shot에 환류(라이브러리 학습 flywheel). 키 필요(server/.env).

    cd platform/server && python -m evals.compile_archetypes
"""
import json
import sys
from pathlib import Path

from pydantic import ValidationError

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import quant_core as qc
from quant_core import data_fetcher as _df
from quant_core.blocks import catalog_spec
from quant_core.ir_engine import StrategyIR, capability_spec, validate_strategy

from app.config import settings
from app.ir_compiler import compile_nl
from app.compile_service import _schema_issues

# (label, nl, 통과 기대 속성 함수)  — 속성은 '둘 다 살았나' 같은 의미 체크
CASES = [
    ("양방향 밴드(WTI)",
     "WTI가 임계값 밑으로 붕괴(저가 기준)하면 long, 임계값 위로 돌파하면 short. "
     "long 임계값 0~60(5단위), short 임계값 80~140(5단위). 청산은 20~360영업일(20일 단위) 또는 "
     "-10% 손절·+50% 익절. 각 임계값/영업일별 성과를 한눈에.",
     lambda ir: ir.get("position", {}).get("direction") == "long_short"),
    ("단순 돌파 롱",
     "삼성전자를 종가가 20일 이동평균 위로 올라오면 매수, 10% 익절 5% 손절.",
     lambda ir: ir.get("position", {}).get("direction") == "long"),
    ("정기 리밸런스 팩터",
     "코스피 종목 중 최근 60일 모멘텀 상위 20종목을 매월 리밸런싱해 동일가중 보유.",
     lambda ir: (ir.get("entry", {}) or ir.get("position", {}).get("entry", {})).get("mode") == "scheduled"
                or ir.get("position", {}).get("entry", {}).get("mode") == "scheduled"),
    ("TSMOM 롱숏",
     "코스피200선물을 20일 추세가 양이면 롱, 음이면 숏.",
     lambda ir: ir.get("position", {}).get("direction") == "long_short"),
    ("조건 양방향 선물 당일매매(M5d)",
     "S&P500 전일대비가 -0.1% 미만이면 코스피200 선물을 시가에 사서 종가에 팔고, "
     "0.1% 초과면 시가에 팔아(공매도) 종가에 청산.",
     lambda ir: (ir.get("position", {}).get("direction") == "long_short"
                 and (ir.get("position", {}).get("entry", {}) or {}).get("mode") == "on_signal"
                 and (ir.get("position", {}).get("exit", {}) or {}).get("hold_days") == 0)),
    ("저평가 반도체주 3개",
     "저평가된 반도체 종목 3개만 골라줘 — PBR 낮은 순으로, PBR과 시가총액도 같이 보여줘.",
     lambda ir: (ir.get("query") == "select"
                 and (ir.get("select") or {}).get("top_n") == 3
                 and (ir.get("select") or {}).get("descending") is False)),
    ("단일종목 360 리포트",
     "삼성전자 어때? 분석해줘.",
     lambda ir: ir.get("query") == "describe" and ir.get("universe", {}).get("kind") == "single"
                and bool(ir.get("universe", {}).get("symbols"))),
    ("포트폴리오 진단",
     "내 포트폴리오 진단해줘. 보유: 삼성전자, SK하이닉스, NAVER.",
     lambda ir: ir.get("query") == "describe" and ir.get("universe", {}).get("kind") == "portfolio"
                and len(ir.get("universe", {}).get("symbols", [])) >= 2),
    ("최적 파라미터(extremize)",
     "RSI 진입 임계값을 10부터 40까지 5단위로 바꿔가며 샤프를 최대화하는 값을 찾아줘.",
     lambda ir: ir.get("study", {}).get("reduction") == "extremize"
                and ir.get("study", {}).get("axis") == "parameter"),
    ("다중팩터 회귀(relate)",
     "PBR과 12개월 모멘텀이 forward 수익을 설명하는지 다중 횡단 회귀로 보여줘. 코스피 종목.",
     lambda ir: ir.get("query") == "relate"
                and ir.get("study", {}).get("relation_kind") == "regression"
                and len(ir.get("study", {}).get("factors", [])) >= 1),
]


def _build_validate():
    sym_keys = (set(_df.ALL_SYMBOLS)
                | {s["name"] for s in _df.load_user_stocks()}
                | set(_df.load_managed_kr_codes())
                | {s["code"] for s in _df.load_managed_overseas()})
    sym_keys |= {"원유선물", "코스피200선물"}  # 내장 매크로(로컬 데이터 차이 보정)
    valid_refs = (sym_keys | {"Open", "High", "Low", "Close", "Volume"}
                  | set(qc.get_all_indicator_columns()))

    def _validate(strat):
        try:
            s = StrategyIR.model_validate(strat)
        except ValidationError as e:
            return (_schema_issues(e), False)
        out = [{"rule": i.rule, "severity": i.severity, "is_error": i.is_error,
                "message": i.message, "path": i.path}
               for i in validate_strategy(s, valid_refs=valid_refs)]
        return (out, not any(i["is_error"] for i in out))

    return _validate, sym_keys


def main():
    print("MODEL:", settings.NL_COMPILE_MODEL, "| KEY:", bool(settings.ANTHROPIC_API_KEY))
    validate_fn, sym_keys = _build_validate()
    cat, caps = catalog_spec(), capability_spec()
    icols = sorted(qc.get_all_indicator_columns())
    n_ok = n_prop = 0
    for label, nl, prop in CASES:
        res = compile_nl(nl, catalog=cat, capabilities=caps, indicator_cols=icols,
                         valid_keys=sym_keys, name_map={}, validate_fn=validate_fn)
        ir = res.get("ir") or {}
        ok = bool(res.get("success"))
        prop_ok = ok and prop(ir)
        n_ok += ok
        n_prop += prop_ok
        print("=" * 64)
        print(f"[{label}] success={ok} prop={prop_ok} repair={res.get('repair_count')}")
        _pos = ir.get("position", {})
        print("  direction:", _pos.get("direction"),
              "| entry.mode:", (_pos.get("entry") or {}).get("mode"),
              "| threshold:", (_pos.get("entry") or {}).get("threshold"),
              "| hold_days:", (_pos.get("exit") or {}).get("hold_days"))
        st = ir.get("study", {}) or {}
        print("  study.axis:", st.get("axis"), "| param_grid 축:", len(st.get("param_grid", []) or []))
        print("  assumptions:", json.dumps(res.get("assumptions") or [], ensure_ascii=False)[:300])
        if not ok:
            print("  error:", res.get("error"))
    print("=" * 64)
    print(f"SCORE: success {n_ok}/{len(CASES)} · 속성충족 {n_prop}/{len(CASES)}")


if __name__ == "__main__":
    main()
