"""평가 컨텍스트 + 데이터 참조 해석 — (dates × symbols) 패널 모델.

명세 §2.2·§4. 의존: types만(순환 방지). op 모듈들이 여기서 resolve_data를 끌어 쓴다.

데이터 참조 의미론 (panel 모델, 비전 §1.1 데이터 격자):
  - "X"            (점 없음)  → 각 종목 자신의 X 패널 (예: "Close", "rsi_14")
  - "__SELF__.X"              → "X"와 동일 (각 종목 자신). UI [이 종목] 라벨용.
  - "SYM.X"        (SYM∈데이터) → SYM의 X를 전 종목에 브로드캐스트 (예: "S&P500.pct_change_1d")

한 종목 마스크가 필요하면 패널을 평가한 뒤 그 종목 컬럼을 select_symbol로 뽑는다
(current_symbol 치환 의미론).

패널 격자 = (유니버스 달력 × 유니버스 심볼). 참조전용 심볼("SYM.X" 브로드캐스트)은
달력·컬럼에 끼지 않고 ffill 조인으로만 합류한다 — from_dataset(universe=...) 참조.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SELF = "__SELF__"


@dataclass
class EvalContext:
    """평가 1회의 환경. dataset + 마스터 타임라인 + 무결성 파라미터."""

    data: dict[str, pd.DataFrame]
    master_idx: pd.DatetimeIndex
    symbols: list[str]
    # 무결성(P0-6에서 채움) — delay: 신호 평가 후 며칠 뒤 체결 가정.
    delay: int = 0

    @classmethod
    def from_dataset(cls, data: dict[str, pd.DataFrame], delay: int = 0,
                     universe: list[str] | None = None) -> "EvalContext":
        """universe = 평가 주체 심볼(마스터 달력·패널 컬럼의 주인). None이면 전 키.

        마스터 달력은 유니버스 심볼 달력의 합집합으로 한정한다 — 참조전용 심볼
        (VIX 등 타 달력 매크로)이 달력에 끼면 __SELF__ 시리즈(ffill 없음)에 참조
        전용 일자마다 NaN 구멍이 생겨 ts_*(rolling, min_periods=window)가 전멸하고
        신호가 조용히 영구 False(항상 현금)가 된다. 참조 심볼은 data에 남아
        resolve_data 브로드캐스트(ffill)로만 합류한다.
        """
        if universe is None:
            subjects = list(data.keys())
        else:
            subjects = [s for s in dict.fromkeys(universe)
                        if s in data and data[s] is not None and not data[s].empty]
            if not subjects:
                raise ValueError("universe 심볼이 데이터셋에 없어 평가 달력을 만들 수 없습니다.")
        dates: set = set()
        for s in subjects:
            df = data[s]
            if df is not None and not df.empty:
                dates.update(df.index)
        idx = pd.DatetimeIndex(sorted(dates))
        return cls(data=data, master_idx=idx, symbols=subjects, delay=delay)


def _col(df: pd.DataFrame, name: str) -> str | None:
    """대소문자 미구분 컬럼 조회."""
    if name in df.columns:
        return name
    low = {c.lower(): c for c in df.columns}
    return low.get(name.lower())


def _matrix(indic: str, ctx: EvalContext) -> pd.DataFrame:
    """지표 indic을 (dates × symbols) 패널로 — 각 종목 자신의 값."""
    out: dict = {}
    for s in ctx.symbols:
        df = ctx.data[s]
        col = _col(df, indic) if (df is not None and not df.empty) else None
        out[s] = (df[col].reindex(ctx.master_idx) if col is not None
                  else pd.Series(np.nan, index=ctx.master_idx))
    return pd.DataFrame(out, index=ctx.master_idx)


def resolve_data(ref: str, ctx: EvalContext) -> pd.DataFrame:
    """데이터 참조 문자열을 패널로 해석."""
    if "." in ref:
        sym, indic = ref.split(".", 1)
        if sym == SELF:
            return _matrix(indic, ctx)
        if sym in ctx.data:
            df = ctx.data[sym]
            col = _col(df, indic) if (df is not None and not df.empty) else None
            # 브로드캐스트(외부 시장/매크로)는 ffill — 종목 달력과 휴장일이 달라도
            # 마지막 관측값을 이어 붙인다(PIT 일관). 자기 데이터(_matrix)는 ffill 안 함.
            series = (df[col].reindex(ctx.master_idx).ffill() if col is not None
                      else pd.Series(np.nan, index=ctx.master_idx))
            return pd.DataFrame({s: series for s in ctx.symbols}, index=ctx.master_idx)
    return _matrix(ref, ctx)


def select_symbol(panel, sym: str):
    """패널에서 한 종목 컬럼(Series)을 뽑는다. 스칼라/시리즈면 그대로."""
    if isinstance(panel, pd.DataFrame):
        return panel[sym]
    return panel


def as_bool_panel(x):
    """조건 출력 정규화 — NaN→False, bool dtype."""
    if isinstance(x, pd.DataFrame):
        return x.fillna(False).astype(bool)
    if isinstance(x, pd.Series):
        return x.fillna(False).astype(bool)
    return x
