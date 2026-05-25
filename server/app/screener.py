"""종목 스크리너 엔진 + 프리셋.

`daily_metrics` (krx_cache 메모리 스냅샷) 위에서 ScreenerSpec을 평가해
조건에 맞는 종목 리스트를 반환.

V1 가능한 필드 (KRX 차단으로 fdr StockListing only):
  close, open, high, low, volume, trade_value, pct_change_1d, change_won,
  market_cap, shares_listed, market(KOSPI/KOSDAQ), kind, is_pref/is_managed/is_halt

V1.1 추가 예정 (NAVER 스크래핑 또는 KRX 우회 후):
  per, pbr, eps, dividend_yield, foreign_ratio, rsi_14, ma_5/20/60,
  momentum_3m, volume_ratio_20d, dist_52w_high_pct

기본 universe 필터 (모든 스펙에 자동 적용):
  - 우선주·관리종목·거래정지·ETF/ETN/REITs 제외
  - 시장: 스펙의 markets로 제한 (기본: KOSPI+KOSDAQ)
  - 최소 거래대금: 5억원 (유동성 확보, 스펙으로 override 가능)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from . import krx_cache

log = logging.getLogger("app.screener")

# V1에서 평가 가능한 필드만 화이트리스트로 (오타·미지원 필드 명확히 에러)
V1_NUMERIC_FIELDS = {
    "close", "open", "high", "low", "volume", "trade_value",
    "pct_change_1d", "change_won", "market_cap", "shares_listed",
    # V1.1 — NAVER 펀더멘털
    "per", "pbr", "eps", "bps", "dps", "dividend_yield", "foreign_rate",
    "high_52w", "low_52w",
    # V1.1 — 기술적 지표 (시총 상위 500종목만 채워짐, 그 외는 null → 룰에서 fail)
    "rsi_14", "atr_14", "atr_14_pct", "bb_pct", "bb_width",
    "momentum_12_1m", "volume_ratio_20d",
    "pct_change_5d", "pct_change_20d", "pct_change_252d",
    "ma_dev_20d", "ma_dev_60d", "ma_dev_200d", "ma_gap_20_60",
    "high_dev_20d", "log_return_1d", "streak",
}
V1_CATEGORY_FIELDS = {"market", "kind"}
V1_BOOL_FIELDS = {"is_pref", "is_managed", "is_halt"}
V1_ALL_FIELDS = V1_NUMERIC_FIELDS | V1_CATEGORY_FIELDS | V1_BOOL_FIELDS

SUPPORTED_OPS = {">", ">=", "<", "<=", "between", "in", "not_in"}

KR_MARKETS = ("KOSPI", "KOSDAQ")
US_MARKETS = ("NAS", "NYS", "AMS")       # 미국(스테이지1) — us_metrics_cache 소스

ScreenerOp = Literal[">", ">=", "<", "<=", "between", "in", "not_in"]


@dataclass
class ScreenerRule:
    field: str
    op: ScreenerOp
    value: Any                          # number | [min,max] | str[] (in/not_in)


@dataclass
class ScreenerSpec:
    rules: list[ScreenerRule]
    markets: list[str] = field(default_factory=lambda: ["KOSPI", "KOSDAQ"])
    exclude: list[str] = field(default_factory=lambda:
        ["managed", "halt", "pref", "etf_etn", "reits"])
    min_trade_value: float = 500_000_000     # 5억원
    sort_field: str | None = None
    sort_order: Literal["asc", "desc"] = "desc"
    limit: int = 20


# ── Spec parsing ──────────────────────────────────────────────────────────────

class ScreenerError(ValueError):
    """잘못된 ScreenerSpec — 사용자 에러로 400 응답에 매핑."""


def parse_spec(raw: dict) -> ScreenerSpec:
    """dict → ScreenerSpec. 알 수 없는 필드·연산자는 즉시 ScreenerError."""
    if not isinstance(raw, dict):
        raise ScreenerError("spec must be an object")

    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ScreenerError("rules must be a list")
    rules = []
    for i, r in enumerate(rules_raw):
        if not isinstance(r, dict):
            raise ScreenerError(f"rules[{i}] must be an object")
        f = r.get("field")
        op = r.get("op")
        v = r.get("value")
        if f not in V1_ALL_FIELDS:
            raise ScreenerError(
                f"rules[{i}].field '{f}' not supported in V1. "
                f"Supported: {sorted(V1_ALL_FIELDS)}")
        if op not in SUPPORTED_OPS:
            raise ScreenerError(
                f"rules[{i}].op '{op}' not supported. "
                f"Supported: {sorted(SUPPORTED_OPS)}")
        if op == "between" and (not isinstance(v, list) or len(v) != 2):
            raise ScreenerError(f"rules[{i}].value must be [min, max] for 'between'")
        if op in ("in", "not_in") and not isinstance(v, list):
            raise ScreenerError(f"rules[{i}].value must be a list for '{op}'")
        rules.append(ScreenerRule(field=f, op=op, value=v))

    sort = raw.get("sort") or {}
    sort_field = sort.get("field") if isinstance(sort, dict) else None
    if sort_field and sort_field not in V1_NUMERIC_FIELDS:
        raise ScreenerError(
            f"sort.field '{sort_field}' must be numeric. "
            f"Supported: {sorted(V1_NUMERIC_FIELDS)}")
    sort_order = (sort.get("order") if isinstance(sort, dict) else None) or "desc"
    if sort_order not in ("asc", "desc"):
        raise ScreenerError("sort.order must be 'asc' or 'desc'")

    markets = raw.get("markets") or ["KOSPI", "KOSDAQ"]
    valid = set(KR_MARKETS) | set(US_MARKETS)
    if not isinstance(markets, list) or not markets or not all(m in valid for m in markets):
        raise ScreenerError(f"markets must be subset of {sorted(valid)}")
    is_us = all(m in US_MARKETS for m in markets)
    is_kr = all(m in KR_MARKETS for m in markets)
    if not (is_us or is_kr):
        raise ScreenerError(
            "markets는 국내(KOSPI/KOSDAQ) 또는 미국(NAS/NYS/AMS) 중 한쪽만 "
            "선택할 수 있습니다 (통화·임계가 다름).")

    exclude = raw.get("exclude") or ["managed", "halt", "pref", "etf_etn", "reits"]
    if not isinstance(exclude, list):
        raise ScreenerError("exclude must be a list")

    limit = int(raw.get("limit") or 20)
    if not (1 <= limit <= 30):
        raise ScreenerError("limit must be 1..30 (시스템 전역 보유 한도)")

    # 거래대금 최소 임계: 국내 5억원(KRW) / 미국 0(USD, S&P500은 이미 유동성 큐레이션)
    default_mtv = 0.0 if is_us else 500_000_000.0
    raw_mtv = raw.get("min_trade_value")
    min_trade_value = float(raw_mtv) if raw_mtv is not None else default_mtv

    return ScreenerSpec(
        rules=rules, markets=markets, exclude=exclude,
        min_trade_value=min_trade_value,
        sort_field=sort_field, sort_order=sort_order, limit=limit,
    )


# ── Rule evaluation ──────────────────────────────────────────────────────────

def _eval_rule(metric: dict, rule: ScreenerRule) -> bool:
    """한 종목·한 룰. null 값은 fail (보수적)."""
    v = metric.get(rule.field)
    if v is None:
        return False
    op = rule.op
    target = rule.value
    if op == ">":
        return v > target
    if op == ">=":
        return v >= target
    if op == "<":
        return v < target
    if op == "<=":
        return v <= target
    if op == "between":
        lo, hi = target
        return lo <= v <= hi
    if op == "in":
        return v in target
    if op == "not_in":
        return v not in target
    return False


def _universe_filter(metric: dict, spec: ScreenerSpec) -> bool:
    """기본 universe — market·exclude·min_trade_value."""
    if metric["market"] not in spec.markets:
        return False
    excl = spec.exclude
    if "managed" in excl and metric.get("is_managed"):
        return False
    if "halt" in excl and metric.get("is_halt"):
        return False
    if "pref" in excl and metric.get("is_pref"):
        return False
    if "etf_etn" in excl and metric.get("kind") == "etf_etn":
        return False
    if "reits" in excl and metric.get("kind") == "reits":
        return False
    tv = metric.get("trade_value") or 0
    if tv < spec.min_trade_value:
        return False
    return True


def run(spec: ScreenerSpec) -> list[dict]:
    """스펙 평가 — 매칭 종목 [{symbol, name, market, ...주요 필드}, ...].

    markets가 미국(NAS/NYS/AMS)이면 us_metrics_cache, 국내면 krx_cache에서 평가.
    parse_spec이 혼합을 막으므로 한쪽만 결정된다.
    """
    if all(m in US_MARKETS for m in spec.markets):
        from . import us_metrics_cache
        metrics = us_metrics_cache.get_all_metrics()
        if not metrics:
            raise ScreenerError(
                "미국 종목 데이터가 아직 준비되지 않았습니다. 잠시 후 다시 시도하세요.")
    else:
        metrics = krx_cache.get_all_metrics()
        if not metrics:
            raise ScreenerError("KRX 스냅샷이 아직 로드되지 않았습니다. 잠시 후 다시 시도하세요.")

    matched = []
    for m in metrics.values():
        if not _universe_filter(m, spec):
            continue
        if not all(_eval_rule(m, r) for r in spec.rules):
            continue
        matched.append(m)

    if spec.sort_field:
        # null 안전 정렬
        matched.sort(
            key=lambda m: (m.get(spec.sort_field) is None,
                            m.get(spec.sort_field) or 0),
            reverse=(spec.sort_order == "desc"),
        )

    out = []
    for m in matched[: spec.limit]:
        out.append({
            "symbol": m["symbol"], "name": m["name"], "market": m["market"],
            "close": m["close"], "pct_change_1d": m["pct_change_1d"],
            "market_cap": m["market_cap"], "trade_value": m["trade_value"],
            "volume": m["volume"],
            # 펀더멘털 (V1.1)
            "per": m.get("per"), "pbr": m.get("pbr"),
            "dividend_yield": m.get("dividend_yield"),
            "foreign_rate": m.get("foreign_rate"),
        })
    return out


# ── 프리셋 (V1) ──────────────────────────────────────────────────────────────

PRESETS: dict[str, dict] = {
    "marcap_top": {
        "title": "시가총액 상위",
        "desc": "시가총액 큰 우량주 위주 (KOSPI+KOSDAQ 합산 시총 상위).",
        "spec": {
            "rules": [
                {"field": "market_cap", "op": ">=", "value": 1_000_000_000_000},  # 1조 이상
            ],
            "sort": {"field": "market_cap", "order": "desc"},
            "limit": 20,
        },
    },
    "trade_value_top": {
        "title": "거래대금 상위",
        "desc": "오늘 가장 활발하게 거래된 종목. 유동성 보장.",
        "spec": {
            "rules": [
                {"field": "market_cap", "op": ">=", "value": 100_000_000_000},   # 시총 1000억+
            ],
            "sort": {"field": "trade_value", "order": "desc"},
            "limit": 20,
        },
    },
    "gainers_today": {
        "title": "오늘 상승률 상위",
        "desc": "오늘 등락률 상위 우량주 (시총 1000억 이상, 상한가 직전 제외).",
        "spec": {
            "rules": [
                {"field": "pct_change_1d", "op": "between", "value": [3, 25]},
                {"field": "market_cap", "op": ">=", "value": 100_000_000_000},
            ],
            "sort": {"field": "pct_change_1d", "order": "desc"},
            "limit": 20,
        },
    },
    "dip_buy_candidates": {
        "title": "낙폭 우량주",
        "desc": "오늘 -3% 이하로 크게 빠진 시총 1000억+ 종목 (저점 매수 후보).",
        "spec": {
            "rules": [
                {"field": "pct_change_1d", "op": "<=", "value": -3},
                {"field": "market_cap", "op": ">=", "value": 100_000_000_000},
            ],
            "sort": {"field": "market_cap", "order": "desc"},
            "limit": 20,
        },
    },
    "volume_surge": {
        "title": "거래량 활발 + 변동 작음",
        "desc": "거래대금 활발하지만 등락률 ±2% 이내 — 큰 변동 없이 거래 늘어난 종목.",
        "spec": {
            "rules": [
                {"field": "pct_change_1d", "op": "between", "value": [-2, 2]},
                {"field": "market_cap", "op": ">=", "value": 200_000_000_000},
                {"field": "trade_value", "op": ">=", "value": 5_000_000_000},  # 50억 이상
            ],
            "sort": {"field": "trade_value", "order": "desc"},
            "limit": 20,
        },
    },
    # ── V1.1 — NAVER 펀더멘털 기반 ────────────────────────────────────────────
    "low_pbr_value": {
        "title": "저PBR 가치주",
        "desc": "PBR 0.3~1.0 + 시총 1000억+ — 자산 대비 저평가된 우량주.",
        "spec": {
            "rules": [
                {"field": "pbr", "op": "between", "value": [0.3, 1.0]},
                {"field": "market_cap", "op": ">=", "value": 100_000_000_000},
            ],
            "sort": {"field": "pbr", "order": "asc"},
            "limit": 20,
        },
    },
    "high_dividend": {
        "title": "고배당 우량주",
        "desc": "배당수익률 3% 이상 + 시총 3000억+ — 현금흐름 좋은 배당주.",
        "spec": {
            "rules": [
                {"field": "dividend_yield", "op": ">=", "value": 3},
                {"field": "market_cap", "op": ">=", "value": 300_000_000_000},
            ],
            "sort": {"field": "dividend_yield", "order": "desc"},
            "limit": 20,
        },
    },
    "near_52w_high": {
        "title": "52주 신고가 근접",
        "desc": "현재가가 52주 고점 대비 -3% 이내 — 강한 모멘텀 종목.",
        "spec": {
            "rules": [
                {"field": "market_cap", "op": ">=", "value": 200_000_000_000},
                # close >= high_52w * 0.97 표현이 불가하므로 별도 룰 — 일단 high_52w 필드로 정렬
                # V1.1에선 보수적으로 "close > 0 AND high_52w > 0"만 체크하고 정렬로 좁힘
            ],
            "sort": {"field": "high_52w", "order": "desc"},  # 임시 — V1.2에서 % 가까움 계산
            "limit": 20,
        },
    },
    "foreign_hold_growth": {
        "title": "외국인 보유 비중 높음",
        "desc": "외국인 보유율 10% 이상 + 시총 3000억+ — 외국인 신뢰 받는 종목.",
        "spec": {
            "rules": [
                {"field": "foreign_rate", "op": ">=", "value": 10},
                {"field": "market_cap", "op": ">=", "value": 300_000_000_000},
            ],
            "sort": {"field": "foreign_rate", "order": "desc"},
            "limit": 20,
        },
    },
    # ── V1.1 — 기술적 지표 기반 (시총 상위 500종목만 채워짐) ─────────────────
    "rsi_oversold": {
        "title": "RSI 과매도 반등 후보",
        "desc": "RSI(14) 30 미만 + 오늘 양봉 (반등 시작 신호).",
        "spec": {
            "rules": [
                {"field": "rsi_14", "op": "<", "value": 30},
                {"field": "pct_change_1d", "op": ">", "value": 0},
                {"field": "market_cap", "op": ">=", "value": 200_000_000_000},
            ],
            "sort": {"field": "rsi_14", "order": "asc"},
            "limit": 20,
        },
    },
    "golden_cross": {
        "title": "골든크로스 (20일 > 60일선)",
        "desc": "20일선이 60일선 위 + 종가가 20일선 위 — 중기 상승 추세 종목.",
        "spec": {
            "rules": [
                {"field": "ma_gap_20_60", "op": ">", "value": 0},  # MA20 > MA60
                {"field": "ma_dev_20d", "op": ">", "value": 0},    # close > MA20
                {"field": "market_cap", "op": ">=", "value": 200_000_000_000},
            ],
            "sort": {"field": "ma_gap_20_60", "order": "desc"},
            "limit": 20,
        },
    },
    "momentum_3m_top": {
        "title": "단기 모멘텀 상위",
        "desc": "최근 20거래일 수익률 + 시총 2000억+ — 단기 상승 모멘텀 종목.",
        "spec": {
            "rules": [
                {"field": "pct_change_20d", "op": ">", "value": 0},
                {"field": "market_cap", "op": ">=", "value": 200_000_000_000},
            ],
            "sort": {"field": "pct_change_20d", "order": "desc"},
            "limit": 20,
        },
    },
    # ── 미국 (S&P500 스테이지1) — 시총·거래대금·1일 등락 기반 (USD) ────────────
    "us_marcap_top": {
        "title": "미국 시총 상위",
        "desc": "S&P500 시가총액 상위 — 미국 대형주 위주.",
        "spec": {
            "markets": ["NAS", "NYS", "AMS"],
            "rules": [],
            "sort": {"field": "market_cap", "order": "desc"},
            "limit": 20,
        },
    },
    "us_trade_value_top": {
        "title": "미국 거래대금 상위",
        "desc": "S&P500 중 오늘 거래대금 상위 — 유동성 큰 종목 (시총 $100억+).",
        "spec": {
            "markets": ["NAS", "NYS", "AMS"],
            "rules": [
                {"field": "market_cap", "op": ">=", "value": 10_000_000_000},  # $10B
            ],
            "sort": {"field": "trade_value", "order": "desc"},
            "limit": 20,
        },
    },
    "us_gainers_today": {
        "title": "미국 오늘 상승률 상위",
        "desc": "S&P500 중 오늘 상승률 상위 (시총 $100억+).",
        "spec": {
            "markets": ["NAS", "NYS", "AMS"],
            "rules": [
                {"field": "pct_change_1d", "op": "between", "value": [1, 30]},
                {"field": "market_cap", "op": ">=", "value": 10_000_000_000},
            ],
            "sort": {"field": "pct_change_1d", "order": "desc"},
            "limit": 20,
        },
    },
}


# ── 필드 카탈로그 (커스터마이징 UI용) ─────────────────────────────────────────
# 사용자가 룰을 직접 조립할 때 빈칸형으로 노출할 필드 메타. (라벨·단위·그룹)
FIELD_CATALOG: list[dict] = [
    {"key": "market_cap", "label": "시가총액", "unit": "원", "group": "규모"},
    {"key": "trade_value", "label": "거래대금", "unit": "원", "group": "규모"},
    {"key": "volume", "label": "거래량", "unit": "주", "group": "규모"},
    {"key": "close", "label": "현재가", "unit": "원", "group": "가격"},
    {"key": "pct_change_1d", "label": "당일 등락률", "unit": "%", "group": "가격"},
    {"key": "pct_change_5d", "label": "5일 등락률", "unit": "%", "group": "가격"},
    {"key": "pct_change_20d", "label": "20일 등락률", "unit": "%", "group": "가격"},
    {"key": "pct_change_252d", "label": "1년 등락률", "unit": "%", "group": "가격"},
    {"key": "per", "label": "PER", "unit": "배", "group": "펀더멘털"},
    {"key": "pbr", "label": "PBR", "unit": "배", "group": "펀더멘털"},
    {"key": "dividend_yield", "label": "배당수익률", "unit": "%", "group": "펀더멘털"},
    {"key": "foreign_rate", "label": "외국인 보유율", "unit": "%", "group": "펀더멘털"},
    {"key": "rsi_14", "label": "RSI(14)", "unit": "", "group": "기술적"},
    {"key": "ma_dev_20d", "label": "20일선 이격도", "unit": "%", "group": "기술적"},
    {"key": "ma_gap_20_60", "label": "20-60일선 갭", "unit": "%", "group": "기술적"},
    {"key": "volume_ratio_20d", "label": "거래량 비율(20일)", "unit": "배", "group": "기술적"},
]


def field_catalog() -> list[dict]:
    """커스터마이징 UI가 노출할 필드 메타. parse_spec이 받는 필드의 부분집합."""
    return FIELD_CATALOG


def list_presets() -> list[dict]:
    """프리셋 카탈로그 — UI에서 카드로 표시할 메타 + 편집 가능한 spec.

    market_group("KR"|"US")로 국내/미국 프리셋을 구분 (웹이 컨텍스트별로 노출)."""
    out = []
    for k, p in PRESETS.items():
        markets = p["spec"].get("markets") or list(KR_MARKETS)
        group = "US" if all(m in US_MARKETS for m in markets) else "KR"
        out.append({"key": k, "title": p["title"], "desc": p["desc"],
                    "spec": p["spec"], "market_group": group})
    return out


def run_preset(key: str) -> list[dict]:
    p = PRESETS.get(key)
    if not p:
        raise ScreenerError(f"unknown preset '{key}'. Available: {list(PRESETS)}")
    spec = parse_spec(p["spec"])
    return run(spec)
