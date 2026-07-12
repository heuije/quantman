"""백테스트 결과(pandas·numpy)를 JSON 안전 형태로 변환."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _num(v: Any):
    """NaN/inf는 None으로, numpy 스칼라는 파이썬 기본형으로."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    return v


def clean_json(obj: Any):
    """중첩 dict/list의 NaN/inf→None, numpy 스칼라→파이썬형으로 정리.

    펼침·이벤트 resultset(버킷/윈도별 지표 dict)을 JSONResponse(allow_nan=False)에
    안전하게 통과시킨다 — n=0 버킷의 nan 지표가 500을 내지 않도록.
    """
    if isinstance(obj, dict):
        return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    return _num(obj)


def _series_points(s: pd.Series) -> list[dict]:
    return [
        {"date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
         "value": _num(val)}
        for idx, val in s.items()
    ]


def serialize_backtest(result: dict) -> dict:
    """IR 엔진 backtest_from_spec 결과를 JSON 직렬화 가능한 dict로."""
    if not result.get("success"):
        return {"success": False, "error": result.get("error")}

    trades_df: pd.DataFrame = result["trades"]
    trades = []
    for _, row in trades_df.iterrows():
        rec = {}
        for k, v in row.items():
            if hasattr(v, "strftime"):
                rec[k] = v.strftime("%Y-%m-%d")
            else:
                rec[k] = _num(v)
        trades.append(rec)

    bench = result.get("benchmark")
    return {
        "success": True,
        "metrics": {k: _num(v) for k, v in result["metrics"].items()},
        "equity": _series_points(result["equity"]),
        # 합성(WS3)은 벤치마크 없음 — None 허용(웹 EquityChart는 미제공 시 생략).
        "benchmark": _series_points(bench) if isinstance(bench, pd.Series) else None,
        "trades": trades,
    }


def serialize_ir_result(res: dict) -> tuple[dict, str]:
    """IR 엔진 결과 → (JSON-안전 dict, kind). 직렬화 정책 단일 출처 — /ir/strategy 라우터와
    챗 도구(run_simulate·run_adjust)가 공유한다(DRY). kind ∈ {error, analysis, axis, backtest}로
    호출측이 부가처리(라우터: analysis→뉴스 facet, backtest→DB persist)를 분기한다.

    · analysis(select·describe·report·extremize): pandas 키 제거 후 clean_json(분석 dict엔 pandas 없음).
    · axis(펼침 resultset): equity Series 등은 비호환 → 선별 키만 clean_json.
    · backtest(1회): serialize_backtest로 equity/benchmark→points·trades→records.
    챗 렌더·웹 모두 shape로 분기하므로 전 경로에서 shape를 보존한다.
    """
    if not res.get("success"):
        return {"success": False, "error": res.get("error"), "issues": res.get("issues", [])}, "error"
    # (1) 1회 백테스트만 pandas 직렬화 — trades DataFrame로 확정 감지(query명·mock 비의존).
    if isinstance(res.get("trades"), pd.DataFrame) and not res.get("axis"):
        payload = serialize_backtest(res)
        payload["warnings"] = res.get("warnings", [])
        # run_query가 스탬프한 결과 품질 계약(status/diagnostics/verdict)과 진단 신호를 보존한다.
        # 없으면 serialize에서 소실돼 agent가 재분류 → capital_starved 등 컨텍스트를 잃고 잘못된
        # verdict('신호 미충족')를 냈다(#2 자본부족 verdict가 웹·모델에 미도달하던 부류).
        for k in ("shape", "status", "diagnostics", "verdict", "capital_starved",
                  "components",    # components(WS3) — 합성 구성 분해. 유실 시 웹 표 미표시(whitelist 부류)
                  "contributions"):   # 적립식(WS4) — 납입 요약(원금대비). 유실 시 웹·모델 미도달
            if res.get(k) is not None:
                payload[k] = clean_json(res[k]) if k in ("components", "contributions") else res[k]
        return payload, "backtest"
    # (2) 펼침(axis) resultset — top-level equity Series 등 JSON 비호환 → 화이트리스트 키만.
    #     extremize는 best/oos 등 키 보존이 필요해 (3) 통과 분기로(구 라우터 동작 보존).
    if res.get("axis") and res.get("reduction") != "extremize":
        out = {"success": True, "axis": res["axis"], "warnings": res.get("warnings", [])}
        for k in ("buckets", "overall", "axes", "metrics", "compare", "consistency", "windows",
                  "by_regime", "n_events", "basis", "by_window", "relation", "factor_names",
                  "composition", "shape", "n_symbols", "query",
                  "row_axis"):   # WS2 — 코호트 행축(종목/파라미터/조건) 라벨. 누락 시 웹이 '종목' 오표기
            if k in res and res[k] is not None:
                out[k] = res[k]
        return clean_json(out), "axis"
    # (3) 그 외 전 분석형상(select·describe·relate·prescribe·breadth·extremize·report) —
    #     분석 dict엔 pandas가 없어 pandas 키만 제거하고 통과(전 키 보존·clean_json).
    out = {k: v for k, v in res.items() if k not in ("equity", "benchmark", "trades", "weight")}
    return clean_json(out), "analysis"
