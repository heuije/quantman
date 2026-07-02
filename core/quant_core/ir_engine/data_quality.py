"""실행 전 데이터 품질 불변식 (data_engine_design.md §7 기둥Ⅰ·Phase 0.5).

백테스트가 쓰는 데이터가 완전·최신한지 *실행 시점*에 검사해 위반을 **명시 경고**로 표면화한다 —
stale/gappy 입력이 침묵의 0%/flat으로 나오던 부류(#4 KOSPI 내부공백·2026 신호 staleness)를 한
진입점에서 전수 차단. 어느 심볼·미래 심볼이든.

휴리스틱은 **결정적**(now() 미사용 → 골든 안전). 절대 신선도(vs 오늘)·거래달력 기반 완전성은
레지스트리 Coverage(후속 Phase)가 담당 — 여기선 교차 심볼 *상대* 비교로:
  · missing   : 데이터 없음/유효가격 0
  · stale_data: 가장 최신 심볼보다 크게 일찍 끝남(신선도 발산 — 크로스에셋 신호 staleness)
  · data_gap  : 공통 구간 데이터 밀도가 최밀 심볼보다 크게 낮음(내부 공백)
단일 심볼(피어 없음)의 절대 staleness는 Coverage 도입 후. 보고된 증상은 모두 크로스에셋이라 충분.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

_STALE_GRACE_DAYS = 30      # 최신 심볼 대비 이만큼↑ 일찍 끝나면 staleness(시장 캘린더 미세차 흡수)
_GAP_TOL = 0.05             # 공통 구간 밀도가 최밀 심볼의 (1-tol)배 미만이면 내부 공백
_CARRYFWD_FLOOR = 5         # 기준 달력 대비 이 일수↑ 결손이면 교차달력 carry-forward로 표면화(미세차 노이즈 억제)


def assess_data_quality(dataset: dict[str, Any], *, start=None, end=None,
                        relevant=None, traded=None) -> list[dict]:
    """서빙된 데이터셋의 품질 위반 경고 목록. 위반 없으면 빈 리스트(결정적).

    relevant(심볼 집합)이 주어지면 *그 심볼만* 평가한다 — 'all' 유니버스는 dataset에 매크로/
    지수/크립토 참조 시계열까지 싣지만, 후보가 아닌 그것들의 staleness를 경고하면 결과를 오해
    시킨다(R4). 호출자(service)가 전략 관련 심볼(거래가능 유니버스 ∪ 참조)만 넘겨 misleading
    경고를 원천 차단한다. None이면 전체(하위호환·단위테스트).

    traded(백테스트가 실제 체결하는 유니버스 심볼)가 주어지면 교차달력 carry-forward의 *기준
    거래달력*으로 쓴다 — 신호/참조 심볼(예: S&P500)이 그 달력의 개장일에 값이 없어 전일값
    유지되는 것만 표면화하고, 거래 심볼 자신은 제외(자기 달력에선 결손 아님). None이면 다수결 폴백."""
    items = (dataset.items() if relevant is None
             else [(s, dataset[s]) for s in dataset if s in relevant])
    warns: list[dict] = []
    valids: dict[str, pd.Series] = {}
    for sym, df in items:
        if not isinstance(df, pd.DataFrame) or df.empty or "Close" not in df.columns:
            warns.append({"code": "missing_data", "message": f"{sym}: 데이터 없음"})
            continue
        v = df["Close"].dropna()
        if v.empty:
            warns.append({"code": "missing_data", "message": f"{sym}: 유효 가격 없음"})
            continue
        valids[sym] = v

    if len(valids) < 2:        # 피어 없으면 상대 비교 불가(절대 검사는 Coverage 후속)
        return warns

    lasts = {s: v.index[-1] for s, v in valids.items()}
    firsts = {s: v.index[0] for s, v in valids.items()}
    ref_last = max(lasts.values())
    for s, last in lasts.items():
        if (ref_last - last).days > _STALE_GRACE_DAYS:
            warns.append({"code": "stale_data",
                          "message": (f"{s}: 데이터 {last.date()}까지 — 최신 결손"
                                      f"(다른 심볼은 ~{ref_last.date()}; 그 이후 구간은 신호 부재로 무체결)")})

    lo, hi = max(firsts.values()), min(lasts.values())
    if start is not None:
        lo = max(lo, pd.Timestamp(start))
    if end is not None:
        hi = min(hi, pd.Timestamp(end))
    if lo < hi:
        idx_in_range = {s: v.loc[lo:hi].index for s, v in valids.items()}
        counts = {s: len(ix) for s, ix in idx_in_range.items()}
        peak = max(counts.values()) if counts else 0
        gap_flagged: set[str] = set()
        for s, c in counts.items():
            if peak and c < peak * (1 - _GAP_TOL):
                gap_flagged.add(s)
                warns.append({"code": "data_gap",
                              "message": (f"{s}: {lo.date()}~{hi.date()} 데이터 밀도 부족"
                                          f"({c}/{peak}일 — 결손 구간으로 무체결 가능)")})
        # 교차 거래달력 carry-forward(#1) — 기준 거래달력에서 값이 없어 전일값으로 유지되는 심볼
        # (예: 미국 S&P500을 한국 코스피 거래일에 참조)을 정보성으로 표면화한다(정상·데이터 손실
        # 아님 — 사용자가 '결손 多'로 오인하던 부류). market_calendar 미의존: 실제 날짜만으로 결정.
        # 기준 = traded(체결 유니버스)의 거래일이면 신호/참조 심볼만 표면화(거래 심볼은 자기 달력에서
        # 결손 아님 → 제외); traded 없으면 다수결 달력 폴백. 진짜 내부공백(data_gap)은 제외(중복 방지).
        traded_syms = {s for s in (traded or ()) if s in idx_in_range}
        if traded_syms:
            reference = pd.DatetimeIndex(np.unique(
                np.concatenate([idx_in_range[s].values for s in traded_syms])))
            candidates = [s for s in idx_in_range if s not in traded_syms]
        else:
            n = len(valids)
            maj = (n + 1) // 2       # ceil(n/2): n=2면 합집합(둘 다), n≥3이면 다수 심볼이 공유한 거래일
            per_date = pd.Series(np.concatenate(
                [ix.values for ix in idx_in_range.values()])).value_counts()
            reference = pd.DatetimeIndex(per_date.index[per_date >= maj])
            candidates = list(idx_in_range)
        nref = len(reference)
        cross = []
        for s in candidates:
            if s in gap_flagged or nref == 0:
                continue
            missing = nref - int(idx_in_range[s].isin(reference).sum())
            if missing > _CARRYFWD_FLOOR:
                cross.append((s, missing))
        if cross:
            detail = ", ".join(f"{s} {m}일" for s, m in sorted(cross, key=lambda x: -x[1]))
            warns.append({"code": "calendar_carryforward",
                          "message": ("거래달력 차이 — 다른 거래일 기준 심볼은 상대 휴장일에 전일값을 "
                                      f"유지합니다(정상·데이터 손실 아님): {detail}")})
    return warns
