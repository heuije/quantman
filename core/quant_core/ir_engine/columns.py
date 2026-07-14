"""자기서술 결과 계약 (뿌리③) — 결과 필드의 표시 메타(라벨·단위·스케일·포맷·방향) 단일 출처.

run_select가 결과에 `columns`(필드 메타)·`scoring`(점수 산식)을 부착하고, 웹·엑셀·모델요약이
이 메타로 균일 렌더한다 — 코드만 보이던 문제·단위(백만원)·산식 불투명·표 정렬을 1곳에서 해결.
display 메트릭이 레지스트리에 없으면 합리적 기본값(label=key·소수 2자리)으로 폴백.
"""
from __future__ import annotations

# 알려진 필드 → 표시 메타. scale: 표시값 = 원시값/scale. direction: 정렬·해석 힌트.
_META: dict[str, dict] = {
    "score": {"label": "점수", "format": "0.000"},
    # 밸류 멀티플 단위는 'x'(배수 국제 표기 — 예: PER 8.00x). DESIGN.md §배수 표기 규칙.
    "pb_ratio": {"label": "PBR", "unit": "x", "format": "0.00", "direction": "low_better"},
    "pbr": {"label": "PBR", "unit": "x", "format": "0.00", "direction": "low_better"},
    "trailing_pe": {"label": "PER", "unit": "x", "format": "0.00", "direction": "low_better"},
    "per": {"label": "PER", "unit": "x", "format": "0.00", "direction": "low_better"},
    "forward_pe": {"label": "Fwd PER", "unit": "x", "format": "0.00", "direction": "low_better"},
    "ev_ebitda": {"label": "EV/EBITDA", "unit": "x", "format": "0.00", "direction": "low_better"},
    "ps_ratio": {"label": "PSR", "unit": "x", "format": "0.00", "direction": "low_better"},
    "dividend_yield": {"label": "배당수익률", "unit": "%", "format": "0.00", "direction": "high_better"},
    "roe": {"label": "ROE", "unit": "%", "format": "0.0", "direction": "high_better"},
    "market_cap": {"label": "시가총액", "unit": "백만원", "scale": 1e6, "format": "#,##0"},
    "momentum_12_1m": {"label": "모멘텀(12-1M)", "format": "0.000", "direction": "high_better"},
    "Close": {"label": "종가", "unit": "원", "format": "#,##0"},
    "consensus_target": {"label": "목표주가", "unit": "원", "format": "#,##0"},
    "target_upside": {"label": "상승여력", "unit": "%", "format": "0.0", "direction": "high_better"},
    "rsi_14": {"label": "RSI(14)", "format": "0.0"},
}


def column_meta(key: str) -> dict:
    """필드 key의 표시 메타({key,label,unit?,scale?,format,direction?}). 미등록은 기본값 폴백."""
    m = _META.get(key)
    return {"key": key, **m} if m else {"key": key, "label": key, "format": "0.00"}


def select_columns(display: list[str]) -> list[dict]:
    """SELECT 결과 컬럼 메타 — identity(이름·코드)·섹터·점수 + display 메트릭(레지스트리 메타)."""
    cols = [
        {"key": "name", "label": "종목", "kind": "identity"},
        {"key": "code", "label": "코드", "kind": "identity"},
        {"key": "sector", "label": "섹터", "kind": "group"},
        {**column_meta("score"), "kind": "score"},
    ]
    cols += [{**column_meta(d), "kind": "metric"} for d in display]
    return cols


def score_recipe(signal: dict, descending: bool) -> dict:
    """신호 트리에서 점수 산식을 도출(투명성) — 팩터·정규화·섹터상대·방향."""
    refs: list[str] = []
    has_rank = has_neutral = False

    def _walk(n) -> None:
        nonlocal has_rank, has_neutral
        if not isinstance(n, dict):
            return
        op = n.get("op")
        if op == "data":
            ref = ((n.get("params") or {}).get("ref") or "").split(".")[-1]
            if ref:
                refs.append(ref)
        elif op == "rank":
            has_rank = True
        elif op == "group_neutralize":
            has_neutral = True
        for c in (n.get("inputs") or {}).values():
            _walk(c)

    _walk(signal)
    uniq = list(dict.fromkeys(refs))
    # 산식 문구는 내부 키(pb_ratio 등) 대신 친근한 라벨(PBR·PER)로 — 유저 노출 텍스트.
    labels = [column_meta(r).get("label", r) for r in uniq]
    if len(uniq) > 1 and has_rank:
        recipe = ("백분위 합 composite(" + "·".join(labels)
                  + (", 섹터상대" if has_neutral else "") + ") — 낮을수록 저평가")
    elif uniq:
        recipe = f"{labels[0]} 단일 정렬({'큰 순' if descending else '작은 순'})"
    else:
        recipe = "신호 점수 정렬"
    return {"recipe": recipe, "factors": uniq,
            "normalization": "rank_pct" if has_rank else "raw",
            "sector_relative": has_neutral,
            "direction": "high_better" if descending else "low_better"}
