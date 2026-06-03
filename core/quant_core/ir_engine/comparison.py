"""일반화 비교 프리미티브 — 기여/비중 패널을 임의 라벨로 그룹화(atomic·MECE).

재설계 핵심. 백테스트 1회가 만드는 두 패널을 입력으로 받는다:
  · contribution[일,종목] — 일별 포트폴리오수익 기여(Σ종목 = 포트 일별수익).
  · weight[일,종목]       — 그날 비중(기여/비중 = 그 종목의 일별수익).

라벨 패널 λ[일,종목]→그룹키로 셀을 묶으면, 라벨이 종목 함수면 섹터/종목별, 일 함수면
레짐/요일별, 둘 다면 섹터×레짐 — *하나의 메커니즘*에서 모든 비교가 나온다. 셀이 정확히 한
그룹에 속하므로 그룹별 기여 합 = 전체(가산성=MECE).

옛 sweep condition축은 라벨 패널을 columns[0] 단일 컬럼으로 붕괴시켜 종목축(섹터)을 못 했다.
이 프리미티브는 패널 전체를 셀 단위로 그룹화해 그 결함을 구조적으로 제거한다.

요약 지표(summarize_returns)는 호출자가 daily 시리즈에 적용한다 — 프리미티브는 순수 분해만.
"""
from __future__ import annotations

import pandas as pd


def compare_by_partition(
    contribution: pd.DataFrame,
    weight: pd.DataFrame,
    label_panel: pd.DataFrame,
) -> dict:
    """기여/비중 패널을 라벨로 그룹화.

    반환: {
      "buckets": {그룹키: {"daily_contribution": Series, "daily_standalone": Series}},
      "overall_daily": Series,   # 전체 포트 일별수익(Σ종목 기여)
    }
    - daily_contribution: 그 그룹이 포트 일별수익에 기여한 몫(가산 — Σ그룹 = overall).
    - daily_standalone:   기여/그룹비중 = 그 그룹만 거래했을 때의 일별수익(비중 0인 날 NaN).
    """
    groups = [g for g in pd.unique(label_panel.values.ravel()) if pd.notna(g)]
    buckets: dict = {}
    for g in groups:
        mask = label_panel == g
        daily_contribution = (contribution * mask).sum(axis=1)
        group_weight = (weight * mask).sum(axis=1)
        daily_standalone = daily_contribution.divide(group_weight).where(group_weight != 0)
        buckets[g] = {
            "daily_contribution": daily_contribution,
            "daily_standalone": daily_standalone,
        }
    return {"buckets": buckets, "overall_daily": contribution.sum(axis=1)}
