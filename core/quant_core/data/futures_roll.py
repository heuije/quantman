"""선물 만기물 패널 → 연속 시계열 합성 (롤 시점 · 갭 조정).

만기물별 일봉 패널을 백테스트용 단일 연속 시계열로 이어붙인다. **롤 시점(roll_method)과
롤 갭 처리(series_adjust)는 데이터에 굽지 않고 백테스트 파라미터로 여기서 적용**한다 —
KRX fut_bydd_trd 만기물 패널이 진실원천, 이 함수가 서빙뷰(연속물)를 파생한다.

패널 스키마: DatetimeIndex(날짜 중복 — 하루에 여러 만기물) × 컬럼
    contract(str "YYYYMM" 만기월) · Open · High · Low · Close · Settle · Volume · OI
만기·롤은 **패널에서 직접 도출**(2번째 목요일 달력 계산이 아니라, 각 계약이 패널에
마지막으로 존재하는 날 = 실제 최종거래일). 휴장 이동에 강건하고 재현·검증가능하다.

roll_method:
    at_expiry     — 근월을 최종거래일까지 보유(만기일 정산 꼬리 포함) 후 익일 차월. 무가공 기본.
    days_before_N — 만기 N영업일 전 차월로 롤(왜곡된 만기 꼬리 회피).
    volume_cross  — 차월 거래량이 근월을 처음 역전하는 날 롤(유동성 추종).
    oi_cross      — 차월 미결제약정이 근월을 처음 역전하는 날 롤.
series_adjust:
    none        — 원본 이어붙임. 롤 시점 베이시스 갭이 그대로(그날 실제 수익률에 반영). 무가공.
    ratio       — 비율 역조정. 롤 갭을 제거해 **수익률 정확 보존**, 최근=실제가·양수 보존.
    back_adjust — 가법 역조정(Panama). 가격 점프차 보존, 최근=실제가(대형 갭이면 음수 가능).

roll_cost_pct 는 만기물 가격차 데이터가 *없을* 때의 폴백 — 패널이 있으면 실제 베이시스를
쓰므로 무시한다(데이터 우선, spec.py §선물 연속물 주석과 동일 계약).
"""
from __future__ import annotations

import re

import pandas as pd

_OUT_COLS = ["Open", "High", "Low", "Close", "Volume"]
_DAYS_BEFORE = re.compile(r"^days_before[:_](\d+)$")


def _mark(contract_df: pd.DataFrame, date) -> float | None:
    """그 계약의 해당일 종가 — Close 우선, 없으면 Settle(비유동 원월·정산일 폴백)."""
    if date is None or date not in contract_df.index:
        return None
    row = contract_df.loc[date]
    c = row.get("Close")
    if pd.notna(c):
        return float(c)
    s = row.get("Settle")
    return float(s) if pd.notna(s) else None


def _by_contract(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """패널 → {계약월: 일봉(날짜 오름차순·중복 제거)}. 만기월 문자열 정렬 = 시간순."""
    groups: dict[str, pd.DataFrame] = {}
    for c, g in panel.groupby("contract", sort=False):
        groups[str(c)] = g[~g.index.duplicated(keep="last")].sort_index()
    return dict(sorted(groups.items()))


def _switch_dates(order: list[str], byc: dict[str, pd.DataFrame],
                  udates: list, method: str) -> dict[int, object]:
    """근월 c[i]→차월 c[i+1] 전환일 S[i]. 그 날부터 c[i+1]을 active로 사용."""
    pos = {d: k for k, d in enumerate(udates)}
    mb = _DAYS_BEFORE.match(method)
    n_before = int(mb.group(1)) if mb else None
    sw: dict[int, object] = {}
    for i in range(len(order) - 1):
        cur, nxt = byc[order[i]], byc[order[i + 1]]
        expiry = cur.index.max()                      # 데이터 주도 만기 = 마지막 존재일
        after = [d for d in udates if d > expiry]
        if not after:                                 # 만기 이후 거래일 없음 = 아직 살아있는
            sw[i] = None                              # 최근월물(만기 미도래) → 롤하지 않음
            continue
        default_after = after[0]                      # 만기 익일(at_expiry·폴백)

        if n_before is not None:
            lo = pos[cur.index.min()]
            j = pos[expiry] - n_before
            sw[i] = udates[j] if j > lo else (udates[lo + 1] if lo + 1 < len(udates) else default_after)
        elif method in ("volume_cross", "oi_cross"):
            field = "Volume" if method == "volume_cross" else "OI"
            hit = None
            for d in cur.index:                       # 근월 생존구간·양 계약 동시 존재일
                if d in nxt.index:
                    cv, nv = cur.loc[d, field], nxt.loc[d, field]
                    if pd.notna(cv) and pd.notna(nv) and float(nv) > float(cv):
                        hit = d
                        break
            sw[i] = hit if hit is not None else default_after
        else:                                         # at_expiry (기본)
            sw[i] = default_after
    return sw


def _active_by_date(order: list[str], udates: list,
                    sw: dict[int, object]) -> list[tuple[object, str]]:
    """각 거래일 → active 계약. 전환일 S[i] 도달 시 i 전진."""
    out, i = [], 0
    for d in udates:
        while i < len(order) - 1 and sw.get(i) is not None and d >= sw[i]:
            i += 1
        out.append((d, order[i]))
    return out


def _segment_offsets(order: list[str], byc: dict[str, pd.DataFrame], sw: dict[int, object],
                     surviving: list, mode: str, last_seg: int) -> dict[int, float]:
    """세그먼트별 누적 조정계수 — 마지막 **active** 세그먼트=단위(0/1), 뒤에서 앞으로 베이시스 누적.

    베이시스 = 전환일 직전 근월 마지막 active일에 근월·차월 **동시 종가** 차(가법)/비(비율).
    같은 날 두 계약 가격이라 순수 베이시스(가격효과 제거). anchor는 order의 마지막이 아니라
    실제 active된 마지막 세그먼트(살아있는 최근월물) — 미사용 원월물 베이시스 오염 방지."""
    ident = 1.0 if mode == "ratio" else 0.0
    off = {last_seg: ident}
    for i in range(last_seg - 1, -1, -1):
        s = sw.get(i)
        prev = [d for d in surviving if s is None or d < s]
        b = prev[-1] if prev else None
        cur_p = _mark(byc[order[i]], b)
        nxt_p = _mark(byc[order[i + 1]], b)
        if cur_p and nxt_p:
            off[i] = off[i + 1] * (nxt_p / cur_p) if mode == "ratio" else off[i + 1] + (nxt_p - cur_p)
        else:
            off[i] = off[i + 1]
    return off


def build_continuous(panel: pd.DataFrame, roll_method: str = "at_expiry",
                     series_adjust: str = "none",
                     roll_cost_pct: float | None = None) -> pd.DataFrame:
    """만기물 패널 → 연속 OHLCV(DatetimeIndex, [Open,High,Low,Close,Volume]).

    roll_cost_pct 는 패널 존재 시 무시(실제 베이시스 우선). 빈 패널 → 빈 프레임.
    """
    if panel is None or panel.empty:
        return pd.DataFrame(columns=_OUT_COLS)
    if "contract" not in panel.columns:
        raise ValueError("패널에 'contract' 컬럼 필요")

    byc = _by_contract(panel.sort_index())
    order = list(byc.keys())
    udates = sorted(panel.index.unique())
    sw = _switch_dates(order, byc, udates, roll_method) if len(order) > 1 else {}
    active = _active_by_date(order, udates, sw)

    rows = {d: byc[c].loc[d] for d, c in active if d in byc[c].index}
    raw = pd.DataFrame(rows).T
    raw.index = pd.DatetimeIndex([d for d, _ in active if d in rows])

    out = pd.DataFrame(index=raw.index)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        out[col] = pd.to_numeric(raw[col], errors="coerce") if col in raw else float("nan")
    if "Settle" in raw:                               # Close 결손(비유동일) → Settle 폴백
        miss = out["Close"].isna()
        out.loc[miss, "Close"] = pd.to_numeric(raw["Settle"], errors="coerce")[miss]
    out["Volume"] = out["Volume"].fillna(0.0)
    out = out.dropna(subset=["Close"])

    if series_adjust in ("ratio", "back_adjust") and len(order) > 1:
        surviving = list(out.index)
        seg_of = {c: k for k, c in enumerate(order)}
        d2seg = {d: seg_of[c] for d, c in active if d in out.index}
        last_seg = max(d2seg.values())            # 마지막 active 세그먼트(살아있는 최근월물)
        off = _segment_offsets(order, byc, sw, surviving, series_adjust, last_seg)
        factors = pd.Series([off[d2seg[d]] for d in out.index], index=out.index, dtype="float64")
        px = ["Open", "High", "Low", "Close"]
        out[px] = out[px].mul(factors, axis=0) if series_adjust == "ratio" else out[px].add(factors, axis=0)

    return out[_OUT_COLS]
