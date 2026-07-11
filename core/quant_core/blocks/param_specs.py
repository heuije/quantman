"""블록별 파라미터(잎 빈칸) UI 스키마 — 자기서술적 카탈로그.

명세 §12·P1-7. 프론트 빌더(와 향후 NL 컴파일러)가 각 블록이 어떤 잎 빈칸을
요구하는지 하드코딩 없이 알 수 있도록, op → 파라미터 스펙 목록을 한곳에 정의한다.
catalog_spec()이 이를 각 블록에 병합한다.

spec kind:
  - "ref"        : 데이터 참조 (종목.지표 / [이 종목].지표 / 매크로) — data 잎
  - "number"     : 숫자 입력
  - "number_list": 숫자 리스트 (bucket 경계, between 범위)
  - "select"     : 열거값 드롭다운 (options 제공)
"""

from __future__ import annotations

_WINDOW = {"name": "window", "kind": "number", "label": "기간(일)", "default": 20, "min": 1}
# 시계열 주기 — D(일봉)·W(주봉)·M(월봉). 거친 봉이면 window는 그 단위(주/월) 수.
_TIMEFRAME = {"name": "timeframe", "kind": "select", "label": "주기",
              "options": ["D", "W", "M"], "default": "D"}

PARAM_SPECS: dict[str, list[dict]] = {
    # ── 잎 ──
    "data": [{"name": "ref", "kind": "ref", "label": "데이터", "required": True}],
    "const": [{"name": "value", "kind": "number", "label": "값", "required": True}],
    # ── 축1 ──
    "compare": [{"name": "op", "kind": "select", "label": "비교",
                 "options": [">", ">=", "<", "<=", "between"], "required": True}],
    "logic": [{"name": "logic", "kind": "select", "label": "결합",
               "options": ["AND", "OR"], "default": "AND"}],
    "binary": [{"name": "op", "kind": "select", "label": "연산",
                "options": ["+", "-", "*", "/"], "required": True}],
    "unary": [{"name": "func", "kind": "select", "label": "함수",
               "options": ["log", "exp", "abs", "sign", "sqrt"], "required": True}],
    "select": [],
    "cross": [{"name": "direction", "kind": "select", "label": "방향",
               "options": ["up", "down"], "required": True}],
    "bucket": [{"name": "edges", "kind": "number_list", "label": "경계값", "required": True}],
    "modifier": [{"name": "kind", "kind": "select", "label": "수식어",
                  "options": ["streak", "within"], "required": True},
                 {"name": "days", "kind": "number", "label": "일수", "default": 2, "min": 1}],
    # ── 축2a 시계열 ──
    "ts_mean": [_WINDOW, _TIMEFRAME], "ts_sum": [_WINDOW, _TIMEFRAME],
    "ts_std": [_WINDOW, _TIMEFRAME], "ts_delta": [_WINDOW, _TIMEFRAME],
    "ts_delay": [_WINDOW, _TIMEFRAME], "ts_rank": [_WINDOW, _TIMEFRAME],
    "ts_zscore": [_WINDOW, _TIMEFRAME], "ts_decay": [_WINDOW, _TIMEFRAME],
    "ts_max": [_WINDOW, _TIMEFRAME], "ts_min": [_WINDOW, _TIMEFRAME],
    "ts_argmax": [_WINDOW, _TIMEFRAME], "ts_argmin": [_WINDOW, _TIMEFRAME],
    "ts_percentile": [_WINDOW, {"name": "percentile", "kind": "number",
                                "label": "백분위(%)", "default": 50, "min": 0, "max": 100}],
    "ts_autocorr": [_WINDOW, {"name": "lag", "kind": "number", "label": "지연(일)", "default": 1, "min": 1}],
    "ts_halflife": [{"name": "window", "kind": "number", "label": "기간(일)", "default": 60, "min": 3}],
    # ── 축2b 관계 ──
    "ts_corr": [_WINDOW],
    "ts_regression": [_WINDOW, {"name": "output", "kind": "select", "label": "출력",
                                "options": ["beta", "residual", "r2"], "default": "beta"}],
    "bar_index": [],
    "orthogonalize": [],
    # ── 축3 횡단 ──
    "rank": [{"name": "descending", "kind": "bool", "label": "큰 값이 상위", "default": False},
             {"name": "unit", "kind": "select", "label": "단위",
              "options": ["pct", "count"], "default": "pct"}],
    "zscore": [], "normalize": [], "scale": [], "cs_dispersion": [],
    # ── 축4 그룹 ──
    "group_rank": [{"name": "group_type", "kind": "select", "label": "그룹",
                    "options": ["Industry", "Sector"], "default": "Industry"}],
    "group_aggregate": [{"name": "stat", "kind": "select", "label": "집계",
                         "options": ["mean", "sum"], "default": "mean"},
                        {"name": "group_type", "kind": "select", "label": "그룹",
                         "options": ["Industry", "Sector"], "default": "Industry"}],
    "group_neutralize": [{"name": "group_type", "kind": "select", "label": "그룹",
                          "options": ["Industry", "Sector"], "default": "Industry"}],
    "hump": [{"name": "threshold", "kind": "number", "label": "임계", "default": 0.0, "min": 0}],
    "winsorize": [{"name": "lower", "kind": "number", "label": "하단(%)", "default": 5, "min": 0, "max": 100},
                  {"name": "upper", "kind": "number", "label": "상단(%)", "default": 95, "min": 0, "max": 100}],
    # ── 달력 라벨 ──
    "calendar": [{"name": "unit", "kind": "select", "label": "단위",
                  "options": ["weekday", "month", "monthweek", "dom", "bday", "turn_of_month"],
                  "default": "weekday"},
                 {"name": "tom_start", "kind": "number", "label": "월초 영업일수(turn_of_month)",
                  "default": 3, "min": 0},
                 {"name": "tom_end", "kind": "number", "label": "월말 영업일수(turn_of_month)",
                  "default": 1, "min": 0}],
    "attribute": [{"name": "attr", "kind": "select", "label": "분류",
                   "options": ["Industry", "Sector", "Market"], "default": "Industry"}],
    "is_in": [{"name": "values", "kind": "value_list", "label": "값 목록(섹터·버킷 등)", "required": True},
              {"name": "match", "kind": "select", "label": "매칭",
               "options": ["exact", "contains"], "default": "exact"},
              {"name": "negate", "kind": "bool", "label": "제외(체크 시 집합 밖)", "default": False}],
}


# ── 문장형 UI — 옵션 한글 라벨(값→문장 조각) + 블록 문장 템플릿 ──────────────────
# 자기서술 카탈로그 확장: 프론트 SentenceTree가 IR을 문장으로 렌더할 때 쓰는 메타.
# OPTION_LABELS는 select/bool param의 값을 문장에서 읽히는 한글로. 키 = "op.param".

OPTION_LABELS: dict[str, dict] = {
    "compare.op": {">": "보다 큼", ">=": "이상", "<": "보다 작음", "<=": "이하",
                   "==": "와 같음", "between": "범위 안"},
    "binary.op": {"+": "＋", "-": "−", "*": "×", "/": "÷"},
    "unary.func": {"log": "로그", "exp": "지수", "abs": "절댓값", "sign": "부호", "sqrt": "제곱근"},
    "cross.direction": {"up": "상향 돌파", "down": "하향 돌파"},
    "modifier.kind": {"streak": "연속 참", "within": "내 1회 이상 참"},
    "ts_regression.output": {"beta": "베타", "residual": "잔차", "r2": "R²(직선성)"},
    "group_rank.group_type": {"Industry": "업종", "Sector": "섹터"},
    "group_aggregate.group_type": {"Industry": "업종", "Sector": "섹터"},
    "group_aggregate.stat": {"mean": "평균", "sum": "합"},
    "group_neutralize.group_type": {"Industry": "업종", "Sector": "섹터"},
    "rank.unit": {"pct": "분위(0~1)", "count": "개수"},
    "rank.descending": {"false": "작은 값이 상위", "true": "큰 값이 상위"},
    "attribute.attr": {"Industry": "업종", "Sector": "섹터", "Market": "시장(거래소)"},
    "is_in.match": {"exact": "정확 일치", "contains": "부분 일치"},
    "calendar.unit": {"weekday": "요일", "month": "월", "monthweek": "월중주차",
                      "dom": "월중일", "bday": "영업일서수", "turn_of_month": "월말월초"},
    "is_in.negate": {"false": "중 하나", "true": "외(제외)"},
}

# 블록 문장 템플릿 — {slot}=하위문장, {param}=인라인 빈칸. variadic(logic)·잎(data·const)은
# 렌더러가 특수 처리하므로 제외. 없는 블록은 프론트가 generic(라벨+슬롯) 폴백.
PHRASE: dict[str, str] = {
    "compare": "{left} 이(가) {right} {op}",
    "binary": "{a} {op} {b}",
    "unary": "{signal}의 {func}",
    "select": "{cond}이면 {a}, 아니면 {b}",
    "cross": "{left}가 {right}를 {direction}",
    "modifier": "{signal}이 최근 {days}일 {kind}",
    "ts_mean": "{signal}의 {window}일 평균", "ts_sum": "{signal}의 {window}일 합",
    "ts_std": "{signal}의 {window}일 변동성", "ts_delta": "{signal}의 {window}일 변화량",
    "ts_delay": "{signal}의 {window}일 전 값", "ts_rank": "{signal}의 {window}일 내 순위",
    "ts_zscore": "{signal}의 {window}일 z-score", "ts_decay": "{signal}의 {window}일 시간가중평활",
    "ts_max": "{signal}의 {window}일 최댓값", "ts_min": "{signal}의 {window}일 최솟값",
    "ts_argmax": "{signal}의 {window}일 최고 후 경과일", "ts_argmin": "{signal}의 {window}일 최저 후 경과일",
    "ts_percentile": "{signal}의 {window}일 {percentile}백분위", "ts_autocorr": "{signal}의 {window}일 자기상관",
    "ts_halflife": "{signal}의 {window}일 평균회귀반감기",
    "ts_corr": "{a}와 {b}의 {window}일 상관", "ts_regression": "{y}를 {x}로 {window}일 적합한 {output}",
    "bar_index": "경과 영업일", "orthogonalize": "{a}에서 {b} 성분 제거",
    "rank": "{signal}의 횡단 순위({descending}·{unit})", "zscore": "{signal}의 횡단 표준화",
    "normalize": "{signal}의 횡단 평균차감", "scale": "{signal}의 비중 스케일",
    "cs_dispersion": "{signal}의 횡단 산포",
    "group_rank": "{signal}의 {group_type} 그룹 내 순위",
    "group_aggregate": "{signal}의 {group_type} 그룹 {stat}",
    "group_neutralize": "{signal}의 {group_type} 그룹 중립화",
    "hump": "{signal}의 턴오버 억제", "winsorize": "{signal}의 이상치 클립",
    "bucket": "{signal}을 {edges} 구간 분류", "calendar": "{unit} 달력",
    "attribute": "종목 {attr}", "is_in": "{signal}이 {values} {negate}",
}


def phrase_for(op: str):
    return PHRASE.get(op)


def params_for(op: str) -> list[dict]:
    """블록 파라미터 스펙 + 문장형 옵션 라벨 병합(자기서술)."""
    out = []
    for p in PARAM_SPECS.get(op, []):
        labels = OPTION_LABELS.get(f"{op}.{p['name']}")
        out.append({**p, "labels": labels} if labels else p)
    return out


# 빌더 팔레트 그룹핑·표시용 (op → 한글 라벨·카테고리). 잎(data/const)은 팔레트에
# 직접 안 띄우고 슬롯 채우기에서 선택되므로 카테고리 "잎".
BLOCK_META: dict[str, dict] = {
    "data": {"label": "데이터", "category": "잎"},
    "const": {"label": "상수", "category": "잎"},
    "compare": {"label": "비교", "category": "비교·논리"},
    "logic": {"label": "그리고/또는", "category": "비교·논리"},
    "cross": {"label": "돌파", "category": "비교·논리"},
    "select": {"label": "조건선택", "category": "비교·논리"},
    "binary": {"label": "사칙연산", "category": "산술"},
    "unary": {"label": "단항변환", "category": "산술"},
    "ts_mean": {"label": "N일 평균", "category": "시계열"},
    "ts_sum": {"label": "N일 합", "category": "시계열"},
    "ts_std": {"label": "N일 변동성", "category": "시계열"},
    "ts_delta": {"label": "N일 변화량", "category": "시계열"},
    "ts_delay": {"label": "N일 전 값", "category": "시계열"},
    "ts_rank": {"label": "윈도우 내 위치", "category": "시계열"},
    "ts_zscore": {"label": "N일 z-score", "category": "시계열"},
    "ts_decay": {"label": "시간가중 평활", "category": "시계열"},
    "ts_max": {"label": "N일 최댓값", "category": "시계열"},
    "ts_min": {"label": "N일 최솟값", "category": "시계열"},
    "ts_argmax": {"label": "최고 후 경과일", "category": "시계열"},
    "ts_argmin": {"label": "최저 후 경과일", "category": "시계열"},
    "ts_percentile": {"label": "N일 백분위", "category": "시계열"},
    "ts_autocorr": {"label": "자기상관", "category": "시계열"},
    "ts_halflife": {"label": "평균회귀 반감기", "category": "시계열"},
    "ts_corr": {"label": "동행성(상관)", "category": "관계"},
    "ts_regression": {"label": "선형적합(베타·잔차·R²)", "category": "관계"},
    "bar_index": {"label": "경과 영업일(추세축)", "category": "시계열"},
    "orthogonalize": {"label": "직교화", "category": "관계"},
    "rank": {"label": "횡단 순위", "category": "횡단"},
    "zscore": {"label": "횡단 표준화", "category": "횡단"},
    "normalize": {"label": "횡단 평균차감", "category": "횡단"},
    "scale": {"label": "비중 스케일", "category": "횡단"},
    "cs_dispersion": {"label": "횡단 산포", "category": "횡단"},
    "group_rank": {"label": "그룹 내 순위", "category": "그룹"},
    "group_aggregate": {"label": "그룹 집계", "category": "그룹"},
    "group_neutralize": {"label": "그룹 중립화", "category": "그룹"},
    "bucket": {"label": "구간분할", "category": "라벨"},
    "calendar": {"label": "달력 라벨", "category": "라벨"},
    "attribute": {"label": "분류(섹터·업종)", "category": "라벨"},
    "is_in": {"label": "범주 포함/제외", "category": "비교·논리"},
    "hump": {"label": "턴오버 억제", "category": "변환"},
    "winsorize": {"label": "이상치 클립", "category": "변환"},
    "modifier": {"label": "지속성/최근성", "category": "수식어"},
}


def meta_for(op: str) -> dict:
    return BLOCK_META.get(op, {"label": op, "category": "기타"})
