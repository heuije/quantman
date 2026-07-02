"""엔진 실결과 → 서버 직렬화 JSON 픽스처 덤프 (Part B1 — API $0 시각 검증).

고정 IR 픽스처를 **프로덕션 동일 데이터**(Part A로 받은 QP_CORE_DATA_DIR) 위에서 실행하고,
서버가 웹으로 보내는 것과 **동일한 직렬화**(`serialize_ir_result`)로 JSON을 뽑아
`web/public/dev-fixtures/`에 쓴다. 웹 `/dev/render` 라우트가 이 JSON을 실제 `ChatResultView`로
렌더 → LLM 0·API 0으로 차트 렌더를 Chrome에서 검증.

사용(먼저 Part A로 데이터 pull):
    QP_CORE_DATA_DIR=~/.quant-platform/dev-data python scripts/dump_results.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "core"))
sys.path.insert(0, str(_ROOT / "server"))

_OUT = _ROOT / "web" / "public" / "dev-fixtures"

# ── IR 빌딩블록 ───────────────────────────────────────────────────────────────
C = {"op": "data", "params": {"ref": "__SELF__.Close"}}


def _ma(w):
    return {"op": "ts_mean", "params": {"window": w}, "inputs": {"signal": C}}


def _cross(w):
    return {"op": "cross", "params": {"direction": "up"}, "inputs": {"left": C, "right": _ma(w)}}


def _hold(sym, cap=5e8, mp=50.0):
    return {"name": f"{sym} 추세추종", "universe": {"kind": "single", "symbols": [sym]},
            "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {"left": C, "right": _ma(20)}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight", "futures_margin_pct": mp},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 10}},
            "simulation": {"initial_capital": cap}, "query": "simulate"}


# ── 픽스처 (대표 차트 표면) ──────────────────────────────────────────────────
FIXTURES = [
    ("backtest_futures", "선물 백테스트 (자본곡선·%토글·방법론패널)", _hold("코스피200선물")),
    ("capital_starved", "자본부족 0거래 (StatusBanner·정직 verdict·#2)", _hold("코스피200선물", cap=1e7)),
    ("event_study", "이벤트 스터디 (구성분해·#7)", {
        "name": "코스피선물 골든크로스 후 수익", "universe": {"kind": "single", "symbols": ["코스피200선물"]},
        "signal": C, "query": "relate",
        "study": {"event": _cross(60), "windows": [5, 20, 60], "event_basis": "close"}}),
    ("cross_calendar", "교차달력 경고 (S&P500→코스피선물·#1)", {
        "name": "S&P 신호 코스피선물", "universe": {"kind": "single", "symbols": ["코스피200선물"]},
        "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
            "left": {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}},
            "right": {"op": "const", "params": {"value": 0}}}},
        "position": {"direction": "long", "sizing": {"mode": "equal_weight", "futures_margin_pct": 50.0},
                     "entry": {"mode": "on_signal"}, "exit": {"hold_days": 0}},
        "simulation": {"initial_capital": 5e8, "fill": "next_open"}, "query": "simulate"}),
    ("relate_ic", "팩터 IC (관계분석)", {
        "name": "모멘텀 IC", "universe": {"kind": "list", "symbols": ["005930", "000660", "005380", "035420"]},
        "signal": C, "query": "relate",
        "study": {"relation_kind": "ic", "target_node": {"op": "data", "params": {"ref": "__SELF__.pct_change_20d"}},
                  "windows": [5, 20]}}),
    ("sweep", "파라미터 스윕 (격자 비교)", {
        "name": "코스피선물 이평 민감도", "universe": {"kind": "single", "symbols": ["코스피200선물"]},
        "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {"left": C, "right": _ma(20)}},
        "position": {"direction": "long", "sizing": {"mode": "equal_weight", "futures_margin_pct": 50.0},
                     "entry": {"mode": "on_signal"}, "exit": {"hold_days": 20}},
        "simulation": {"initial_capital": 5e8}, "query": "simulate",
        "study": {"axis": "parameter", "param_grid": [
            {"path": "signal.inputs.right.params.window", "values": [10, 20, 60]}]}}),
]


def _collect_symbols(spec: dict) -> set[str]:
    """유니버스 심볼 + 신호/스터디의 크로스에셋 ref(SYM.field, __SELF__ 제외)."""
    syms = set(spec.get("universe", {}).get("symbols") or [])

    def walk(node):
        if isinstance(node, dict):
            if node.get("op") == "data":
                ref = (node.get("params") or {}).get("ref", "")
                if "." in ref and not ref.startswith("__SELF__"):
                    syms.add(ref.split(".")[0])
            for v in (node.get("inputs") or {}).values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    st = spec.get("study") or {}
    for node in (spec.get("signal"), st.get("event"), st.get("target_node"), st.get("label")):
        walk(node)
    for f in st.get("factors") or []:
        walk(f)
    return syms


def main() -> int:
    from quant_core.dataset import load_dataset_for
    from quant_core.ir_engine import classify_status
    from quant_core.ir_engine.service import strategy_from_spec
    from quant_core.ir_engine.spec import StrategyIR
    from app.serialize import clean_json, serialize_ir_result
    from app.chat.tools import attach_methodology, param_manifest
    from app.chat.context import attach_context

    def _finalize(res: dict, spec: dict) -> dict:
        """tools.run_simulate + agent 후처리 체인 재현 → 프로덕션과 동일한 렌더 shape."""
        res, _ = serialize_ir_result(res)
        if not res.get("success"):
            return res
        res["ir"] = spec
        res["adjustable"] = param_manifest(StrategyIR.model_validate(spec))
        res["assumptions"] = []
        res = attach_context(res)
        if res.get("success") and "status" not in res:
            res.update(classify_status(res))
        res = attach_methodology(res)
        return clean_json(res)

    _OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, label, spec in FIXTURES:
        try:
            StrategyIR.model_validate(spec)     # 조기 검증(픽스처 유효성)
            ds = load_dataset_for(_collect_symbols(spec), with_indicators=True)
            res = _finalize(strategy_from_spec(spec, ds), spec)
            (_OUT / f"{name}.json").write_text(
                json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
            manifest.append({"name": name, "label": label,
                             "shape": res.get("shape"), "status": res.get("status"),
                             "success": res.get("success")})
            print(f"✓ {name}: shape={res.get('shape')} status={res.get('status')} "
                  f"verdict={(res.get('verdict') or '')[:30]} warnings={len(res.get('warnings') or [])}")
        except Exception as e:  # noqa: BLE001 — 픽스처 하나 실패가 전체를 막지 않게
            print(f"✗ {name}: {type(e).__name__}: {e}")
    (_OUT / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ {len(manifest)}/{len(FIXTURES)} 픽스처 → {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
