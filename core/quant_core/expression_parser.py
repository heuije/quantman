"""
FastExpression AST 수식 파서 및 연산 엔진.
모든 수식을 (dates x symbols) 형태의 2차원 DataFrame(가중치 행렬)으로 안전하고 고속으로 연산한다.
"""

from __future__ import annotations

import ast
import numpy as np
import pandas as pd
from typing import Any, Callable

# ── 그룹 융합 헬퍼 ────────────────────────────────────────────────────────────

def get_symbol_group(sym: str, group_type: str = "Industry") -> str:
    """티커 심볼의 그룹(업종/섹터)명. static.classification 사이드카(FDR KRX-DESC) 조회.

    미수급 종목(US 또는 사이드카 미적재)은 폴백 — KR은 '기타', 그 외 'Other'.
    폴백은 빈 그룹을 안 만들도록 일관된 값을 준다(그룹 연산 안전).
    """
    from .data.feeds.classification import symbol_group
    g = symbol_group(sym, group_type)
    if g:
        return g
    return "기타" if sym.split(".")[0].isdigit() else "Other"


_NAME_CACHE: dict | None = None


def symbol_name(sym: str) -> str:
    """티커 코드 → 종목명. ticker_db.json(코어 metadata)의 한글명 k(없으면 영문 e), 미수급은 코드 폴백.

    결과 표시용(자기서술 결과 계약) — 코드만 보이던 결과에 이름을 붙인다. ticker_db 형식
    [{"t":"005930.KS","k":"삼성전자","e":...}] → {코드: 이름} 맵 1회 캐시.
    """
    global _NAME_CACHE
    if _NAME_CACHE is None:
        import json
        import os
        from pathlib import Path
        p = (Path(os.getenv("QP_CORE_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
             / "ticker_db.json")
        m: dict[str, str] = {}
        if p.exists():
            try:
                for r in json.loads(p.read_text(encoding="utf-8")):
                    code = str(r.get("t", "")).split(".")[0]
                    nm = (r.get("k") or r.get("e") or "").strip()
                    if code and nm:
                        m.setdefault(code, nm)
            except (ValueError, OSError):
                pass
        _NAME_CACHE = m
    return _NAME_CACHE.get(sym.split(".")[0], sym)


# ── 시계열 및 횡단면 연산 함수군 ───────────────────────────────────────────────

def ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).mean()

def ts_sum(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).sum()

def ts_std_dev(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).std()

def ts_delta(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.diff(d)

def ts_delay(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.shift(d)

def ts_rank(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """d일 동안의 시계열 내 현재 값의 롤링 백분위 순위 (0~1)."""
    try:
        return x.rolling(d).rank(pct=True)
    except Exception:
        # 구버전 pandas 호환용 백엔드 롤백
        return x.rolling(d).apply(
            lambda w: (w <= w[-1]).sum() / len(w) if len(w) > 0 else np.nan,
            raw=True
        )

def ts_zscore(x: pd.DataFrame, d: int) -> pd.DataFrame:
    mu = x.rolling(d).mean()
    std = x.rolling(d).std().replace(0, np.nan)
    return (x - mu) / std

def ts_decay_linear(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """선형 가중치(최근일 가중치 대폭 부여)를 적용한 이동평균."""
    weights = np.arange(1, d + 1)
    weights = weights / weights.sum()
    return x.rolling(d).apply(lambda w: np.dot(w, weights), raw=True)

def ts_max(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).max()

def ts_min(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d).min()

def ts_arg_max(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """d일 중 최고치 발생 시점부터 현재까지의 일수."""
    return x.rolling(d).apply(lambda w: d - 1 - np.argmax(w) if len(w) > 0 else np.nan, raw=True)

def ts_arg_min(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """d일 중 최저치 발생 시점부터 현재까지의 일수."""
    return x.rolling(d).apply(lambda w: d - 1 - np.argmin(w) if len(w) > 0 else np.nan, raw=True)

def cs_rank(x: pd.DataFrame) -> pd.DataFrame:
    """횡단면(Cross-Sectional) 순위화 (0~1로 정규화)."""
    return x.rank(axis=1, pct=True)

def cs_zscore(x: pd.DataFrame) -> pd.DataFrame:
    """횡단면 표준화."""
    mean = x.mean(axis=1)
    std = x.std(axis=1).replace(0, np.nan)
    return x.sub(mean, axis=0).div(std, axis=0)

def cs_normalize(x: pd.DataFrame) -> pd.DataFrame:
    """횡단면 평균 차감(평균 0화)."""
    return x.sub(x.mean(axis=1), axis=0)

def cs_scale(x: pd.DataFrame) -> pd.DataFrame:
    """절대 비중합이 1.0이 되도록 롱숏 스케일링."""
    return x.div(x.abs().sum(axis=1).replace(0, np.nan), axis=0)

def hump(x: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """가중치 일일 변동폭이 threshold 이하인 경우 어제 비중을 유지하여 Turnover 억제."""
    out = x.copy()
    for col in x.columns:
        s = x[col].to_numpy()
        h = np.empty_like(s)
        if len(s) > 0:
            h[0] = s[0]
            for t in range(1, len(s)):
                val = s[t]
                prev = h[t - 1]
                if pd.isna(val):
                    h[t] = np.nan
                elif pd.isna(prev):
                    h[t] = val
                elif abs(val - prev) > threshold:
                    h[t] = val
                else:
                    h[t] = prev
        out[col] = h
    return out

def group_neutralize(x: pd.DataFrame, group_type: str = "Industry") -> pd.DataFrame:
    """동일 산업군/섹터 평균 차감을 통한 산업 위험 노출 제거."""
    group_map = {sym: get_symbol_group(sym, group_type) for sym in x.columns}
    out = x.copy()
    unique_groups = set(group_map.values())
    for g in unique_groups:
        cols_in_g = [sym for sym, grp in group_map.items() if grp == g]
        if len(cols_in_g) > 0:
            g_df = x[cols_in_g]
            mean_g = g_df.mean(axis=1)
            out[cols_in_g] = g_df.sub(mean_g, axis=0)
    return out

def if_else(cond: pd.DataFrame, x: pd.DataFrame | float, y: pd.DataFrame | float) -> pd.DataFrame:
    """삼항 연산자 (조건 만족 시 x, 아니면 y)."""
    if isinstance(x, (int, float)):
        x = pd.DataFrame(x, index=cond.index, columns=cond.columns)
    if isinstance(y, (int, float)):
        y = pd.DataFrame(y, index=cond.index, columns=cond.columns)
    return x.where(cond, y)


# ── ExpressionParser 클래스 ───────────────────────────────────────────────────

class ExpressionParser:
    """WorldQuant BRAIN 호환 AST 기반 퀀트 수식 해석 및 고속 연산 파서."""
    
    def __init__(self, data: dict[str, pd.DataFrame]):
        self.data = data
        all_dates = set()
        for df in data.values():
            if not df.empty:
                all_dates.update(df.index)
        self.master_idx = pd.DatetimeIndex(sorted(all_dates))
        self.symbols = list(data.keys())
        
        # 허용된 안전 퀀트 연산자 등록
        self.funcs: dict[str, Callable] = {
            # 시계열
            "ts_mean": ts_mean,
            "ts_sum": ts_sum,
            "ts_std_dev": ts_std_dev,
            "ts_delta": ts_delta,
            "ts_delay": ts_delay,
            "ts_rank": ts_rank,
            "ts_zscore": ts_zscore,
            "ts_decay_linear": ts_decay_linear,
            "ts_max": ts_max,
            "ts_min": ts_min,
            "ts_arg_max": ts_arg_max,
            "ts_arg_min": ts_arg_min,
            # 횡단면
            "rank": cs_rank,
            "zscore": cs_zscore,
            "normalize": cs_normalize,
            "scale": cs_scale,
            # 변환 / 그룹
            "hump": hump,
            "group_neutralize": group_neutralize,
            "if_else": if_else,
            # 수학 함수
            "log": lambda x: np.log(x.clip(lower=1e-9)),
            "exp": lambda x: np.exp(x),
            "abs": lambda x: x.abs(),
            "sign": lambda x: np.sign(x),
        }

    def get_matrix(self, indicator: str) -> pd.DataFrame:
        """지표 컬럼을 추출하여 (dates x symbols) DataFrame 매트릭스로 병합한다. (대소문자 미구분)"""
        series_dict = {}
        target_col = None
        
        # 컬럼 대소문자 미구분 검색
        for sym, df in self.data.items():
            if df.empty:
                continue
            if indicator in df.columns:
                target_col = indicator
                break
            for col in df.columns:
                if col.lower() == indicator.lower():
                    target_col = col
                    break
            if target_col:
                break
                
        if not target_col:
            raise ValueError(f"지표 '{indicator}'를 데이터셋 컬럼에서 찾을 수 없습니다.")
            
        for sym, df in self.data.items():
            if not df.empty and target_col in df.columns:
                series_dict[sym] = df[target_col]
            else:
                series_dict[sym] = pd.Series(np.nan, index=self.master_idx)
                
        matrix = pd.DataFrame(series_dict)
        matrix = matrix.reindex(self.master_idx)
        return matrix

    def resolve_variable(self, name: str) -> pd.DataFrame:
        """변수명을 단일 지표 매트릭스 또는 매크로 지표(브로드캐스트)로 해석한다."""
        # 1) 전체 기재형: "VIX.Close", "삼성전자.Close"
        if "." in name:
            sym, indic = name.split(".", 1)
            if sym in self.data:
                df = self.data[sym]
                target_col = None
                if indic in df.columns:
                    target_col = indic
                else:
                    for col in df.columns:
                        if col.lower() == indic.lower():
                            target_col = col
                            break
                if target_col:
                    series = df[target_col].reindex(self.master_idx)
                    return pd.DataFrame({s: series for s in self.symbols}, index=self.master_idx)

        # 2) 심볼 단독: "VIX" (해당 심볼의 마감 가격을 모든 종목에 매트릭스로 브로드캐스트)
        if name in self.data:
            df = self.data[name]
            col = "Close" if "Close" in df.columns else df.columns[0]
            series = df[col].reindex(self.master_idx)
            return pd.DataFrame({s: series for s in self.symbols}, index=self.master_idx)
            
        # 3) 일반 지표명: "Close", "rsi_14" (각 종목의 해당 컬럼 횡단면 결합)
        return self.get_matrix(name)

    def evaluate(self, expression: str) -> pd.DataFrame:
        """수식 문자열을 AST 안전 파싱하여 (dates x symbols) 매트릭스로 최종 평가한다."""
        expr_ast = ast.parse(expression.strip(), mode="eval")
        
        def _eval(node: Any) -> Any:
            # 1) 상수
            if isinstance(node, ast.Constant):
                return node.value
            elif isinstance(node, ast.Num):  # legacy Python 하위 호환
                return node.n
                
            # 2) 변수 (지표)
            elif isinstance(node, ast.Name):
                return self.resolve_variable(node.id)
                
            # 3) 단항 연산
            elif isinstance(node, ast.UnaryOp):
                operand = _eval(node.operand)
                if isinstance(node.op, ast.USub):
                    return -operand
                elif isinstance(node.op, ast.UAdd):
                    return +operand
                elif isinstance(node.op, ast.Not):
                    return ~operand
                raise TypeError(f"지원하지 않는 단항 연산자: {type(node.op)}")
                
            # 4) 이항 연산
            elif isinstance(node, ast.BinOp):
                left = _eval(node.left)
                right = _eval(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                elif isinstance(node.op, ast.Sub):
                    return left - right
                elif isinstance(node.op, ast.Mult):
                    return left * right
                elif isinstance(node.op, ast.Div):
                    return left / right
                elif isinstance(node.op, ast.Pow):
                    return left ** right
                elif isinstance(node.op, ast.Mod):
                    return left % right
                raise TypeError(f"지원하지 않는 이항 연산자: {type(node.op)}")
                
            # 5) 비교 연산
            elif isinstance(node, ast.Compare):
                left = _eval(node.left)
                right = _eval(node.comparators[0])
                op = node.ops[0]
                if isinstance(op, ast.Gt):
                    return left > right
                elif isinstance(op, ast.GtE):
                    return left >= right
                elif isinstance(op, ast.Lt):
                    return left < right
                elif isinstance(op, ast.LtE):
                    return left <= right
                elif isinstance(op, ast.Eq):
                    return left == right
                elif isinstance(op, ast.NotEq):
                    return left != right
                raise TypeError(f"지원하지 않는 비교 연산자: {type(op)}")
                
            # 6) 함수 호출
            elif isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise TypeError("함수 이름은 반드시 단순 식별자 형태여야 합니다.")
                func_name = node.func.id
                if func_name not in self.funcs:
                    raise NameError(f"지원하지 않는 퀀트 연산자: {func_name}")
                args = [_eval(arg) for arg in node.args]
                return self.funcs[func_name](*args)
                
            raise TypeError(f"해석할 수 없는 수식 노드 유형: {type(node)}")
            
        result = _eval(expr_ast.body)
        
        # 연산 결과가 스칼라이면 매트릭스로 팽창
        if isinstance(result, (int, float)):
            result = pd.DataFrame(result, index=self.master_idx, columns=self.symbols)
            
        return result
