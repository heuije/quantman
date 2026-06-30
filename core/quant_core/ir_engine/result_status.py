"""결과 품질 계약 (chat-reliability-redesign §3) — 모든 엔진/도구 결과가 자기 품질을 스스로 서술.

`run_query`가 성공 결과에 status/diagnostics/verdict를 스탬프한다(shape 옆). 모델(compact_summary)·
UI(ChatResultView)·루프(agent)·메트릭(ChatTurnMetric)이 **단일 계약**으로 분기 — 빈/퇴화/불가 결과를
'성공'으로 흘려보내던 낙관 전파(진단 뿌리 R1)를 종식한다.

status 의미:
  ok               유효·해석 가능
  empty            정상 실행, 진짜 빈 결과(거래 0·적격 0·이벤트 0) — 정직 고지 + 완화안
  degenerate       실행됐으나 신뢰 불가(불가능한 손실·단일 극단거래) — 경고·진단(맹목 재서술 금지)
  data_insufficient  데이터 결손/부족이 결과를 무효화 — 보강/구간 조정
  infeasible       실행 자체 실패(success=False) — 구체 사유

순수 함수(부수효과 없음) — 픽스처로 단위 테스트. 숫자 임계는 모듈 상수.
"""
from __future__ import annotations

import re
from typing import Any

from .summarize import result_shape

# ── 판정 임계 (단일 출처) ──────────────────────────────────────────────────────
_COVERAGE_INSUFFICIENT = 0.80   # 공통구간 데이터 밀도 < 이 값이면 data_insufficient
_MIN_EVENTS = 5                 # 이벤트스터디 표본 하한(미만이면 저신뢰 — verdict 명시, status는 ok)
_LOSS_IMPOSSIBLE = -100.0       # 누적수익(%)·MDD(%)가 이 값 미만이면 퇴화(무레버리지 -100% 한계 위반)

_GAP_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*일")   # 'data_gap' 메시지의 '404/498일' 밀도


def _coverage(warnings: list) -> float | None:
    """data_gap 경고에서 공통구간 밀도(있으면 가장 낮은 값)를 추출. 없으면 None."""
    best: float | None = None
    for w in warnings:
        if not (isinstance(w, dict) and w.get("code") == "data_gap"):
            continue
        m = _GAP_RE.search(str(w.get("message", "")))
        if m and int(m.group(2)):
            cov = int(m.group(1)) / int(m.group(2))
            best = cov if best is None else min(best, cov)
    return best


def _stale(warnings: list) -> list[str]:
    return [str(w.get("message", "")).split(":")[0]
            for w in warnings if isinstance(w, dict) and w.get("code") == "stale_data"]


def classify_status(result: Any) -> dict:
    """결과 dict → {status, diagnostics, verdict}. success=False면 infeasible."""
    if not isinstance(result, dict):
        return {"status": "infeasible", "diagnostics": {}, "verdict": "결과 형식 오류"}
    if not result.get("success", True):
        return {"status": "infeasible", "diagnostics": {},
                "verdict": str(result.get("error") or "실행 실패")}

    shape = result_shape(result)
    warnings = result.get("warnings") or []
    diag: dict[str, Any] = {}
    cov = _coverage(warnings)
    if cov is not None:
        diag["coverage"] = round(cov, 3)
    stale = _stale(warnings)
    if stale:
        diag["stale_symbols"] = len(stale)

    def done(status: str, verdict: str = "") -> dict:
        return {"status": status, "diagnostics": diag, "verdict": verdict}

    # ── 백테스트(단일) ──
    if shape == "simulate":
        m = result.get("metrics") or {}
        nt, tr, mdd = m.get("n_trades"), m.get("total_return"), m.get("mdd")
        if nt is not None:
            diag["n_trades"] = nt
        if cov is not None and cov < _COVERAGE_INSUFFICIENT:
            return done("data_insufficient",
                        f"데이터 밀도 {cov * 100:.0f}%로 부족 — 결손 구간이 결과를 왜곡합니다"
                        "(데이터 보강 또는 기간 조정 필요).")
        if nt == 0:
            starved = result.get("capital_starved") or 0
            if starved:
                # 신호는 발생했으나 자본이 1계약 증거금에 못 미쳐 진입 못 함(#2) — '신호 미충족'으로
                # 오인시키지 않는다. 결과 수치는 불변, 사유만 정확히.
                diag["capital_starved"] = starved
                return done("empty",
                            "거래 0건 — 진입 신호는 발생했으나 자본이 1계약 증거금에 못 미쳐 진입하지 "
                            "못했습니다. 자본을 늘리거나, 증거금 사용률을 높이거나, 미니 선물(승수 작음)을 "
                            "검토하세요.")
            return done("empty",
                        "거래 0건 — 신호가 한 번도 충족되지 않았습니다(전략·유니버스·사이징 점검).")
        impossible = (_num(tr) is not None and _num(tr) < _LOSS_IMPOSSIBLE) or \
                     (_num(mdd) is not None and _num(mdd) < _LOSS_IMPOSSIBLE)
        if impossible:
            return done("degenerate",
                        f"신뢰 불가 — 누적 {_fmt(tr)}%·MDD {_fmt(mdd)}%(거래 {nt}회)는 무레버리지 한계"
                        "(-100%)를 넘습니다. 레버리지/사이징/데이터 충돌 의심(엔진 마진 모델 점검).")
        if nt is not None and nt <= 1:
            return done("ok", f"거래 {nt}회로 표본이 극히 적어 통계적 신뢰도가 낮습니다.")
        return done("ok")

    # ── 스크리닝 ──
    if shape == "select":
        el = result.get("eligible_size")
        n = len(result.get("results") or [])
        diag["eligible_size"] = el
        diag["universe_size"] = result.get("universe_size")
        if el == 0 or n == 0:
            v = "조건을 만족하는 종목이 0건입니다"
            if stale:
                v += f"(일부 데이터 결손 {len(stale)}개 심볼 영향 가능)"
            return done("empty", v + " — 기준 완화 또는 유니버스 점검이 필요합니다.")
        return done("ok")

    # ── 이벤트 스터디 ──
    if shape == "event_study":
        ne = result.get("n_events")
        diag["n_events"] = ne
        if ne == 0:
            return done("empty", "이벤트 0건 — 조건이 너무 가혹합니다(임계값 완화 필요).")
        if ne is not None and ne < _MIN_EVENTS:
            return done("ok", f"이벤트 {ne}건으로 표본이 적어 통계 신뢰도가 제한적입니다.")
        return done("ok")

    # ── 분할(sweep: 파라미터·종목·국면·기간) ──
    if shape == "sweep":
        buckets = result.get("buckets") or {}
        active = [k for k, b in buckets.items()
                  if isinstance(b, dict) and not b.get("error") and b.get("n")]
        diag["n_buckets"] = len(buckets)
        inactive = [k for k in buckets if k not in active]
        if inactive:
            diag["inactive_buckets"] = inactive
        if buckets and not active:
            return done("empty", "전 구간이 무거래입니다 — 분할 기준·데이터를 점검하세요.")
        if cov is not None and cov < _COVERAGE_INSUFFICIENT:
            return done("data_insufficient", f"데이터 밀도 {cov * 100:.0f}%로 부족합니다.")
        return done("ok")

    # ── 관계 분석(IC·회귀·상관) — 횡단 표본 0이면 무의미한 NaN을 ok로 흘려보내지 않는다 ──
    # 좋은 가설(예: 외국인 수급)이 종목 수·공통구간 부족으로 표본 0이 돼 '빈 차트'로 죽던 부류(#9b)를
    # 닫는다. 단일종목은 엔진이 success=False(_empty)로 이미 infeasible 처리.
    if shape == "relate_ic":
        n = _max_window_count(result, "n")
        diag["ic_samples"] = n
        if n == 0:
            return done("data_insufficient",
                        "IC 표본 0 — 횡단 상관을 낼 종목 수·공통구간이 부족합니다(단일종목·짧은 기간). "
                        "단일 종목 예측은 시계열 이벤트스터디가 더 적합합니다.")
        if n < _MIN_EVENTS:
            return done("ok", f"IC 표본 {n}일로 적어 통계 신뢰도가 제한적입니다.")
        return done("ok")

    if shape == "relate_regression":
        if _max_window_count(result, "n_periods") == 0:
            return done("data_insufficient",
                        "회귀 표본 0 — 횡단 회귀를 낼 종목·공통구간이 부족합니다(종목 수·기간 점검).")
        return done("ok")

    if shape == "correlation_matrix":
        if not result.get("n_obs"):
            return done("data_insufficient",
                        "공통 관측치 0 — 상관을 낼 공통 거래일이 없습니다(기간·종목 점검).")
        return done("ok")

    # ── 그 외(describe·prescribe·breadth·inspect·news 등) — 성공이면 ok ──
    return done("ok")


def _max_window_count(result: Any, key: str) -> int:
    """관계분석 by_window의 표본 수 최댓값 — IC는 overall.n, 회귀는 n_periods. 윈도마다 표본이
    달라(공통구간 차이) 가장 많은 쪽을 본다(하나라도 유효하면 분석 가능). 결손/비수치는 0."""
    bw = result.get("by_window") if isinstance(result, dict) else None
    best = 0
    for block in (bw or {}).values():
        if not isinstance(block, dict):
            continue
        src = block.get("overall") if key == "n" else block
        n = (src or {}).get(key) if isinstance(src, dict) else None
        if isinstance(n, int):
            best = max(best, n)
    return best


def _num(v: Any) -> float | None:
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _fmt(v: Any) -> str:
    x = _num(v)
    return "—" if x is None else f"{x:.1f}"
