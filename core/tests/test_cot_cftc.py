"""macro.cot 피드(CFTC COT Legacy Futures-Only) 단위 테스트 — 네트워크 없음(fake fetch).

검증: 순수 파서(PIT +3일 lag·결측 drop·정정 keep-last)·시장 단위 원자성(실패 skip)·
멱등 merge·심볼 정합(data_fetcher.MACRO_COT_SYMBOLS ↔ 피드 _MARKETS 파생명 — 리터럴
드리프트를 CI가 차단).
"""

from __future__ import annotations

import pandas as pd

from quant_core import data_fetcher as df_mod
from quant_core.data.feeds import cot_cftc


def _rows(dates_vals):
    """[(date, long, short, oi), ...] → Socrata 행 리스트."""
    return [{"report_date_as_yyyy_mm_dd": f"{d}T00:00:00.000",
             "noncomm_positions_long_all": str(lo),
             "noncomm_positions_short_all": str(sh),
             "open_interest_all": str(oi)} for d, lo, sh, oi in dates_vals]


def test_parse_rows_pit_lag_and_values():
    df = cot_cftc.parse_rows(_rows([("2026-06-23", 300, 100, 5000)]))
    assert list(df.index) == [pd.Timestamp("2026-06-26")]      # 화(보고)+3일=금(공개) PIT
    assert df["spec_net"].iloc[0] == 200.0                     # 롱−숏
    assert df["oi"].iloc[0] == 5000.0


def test_parse_rows_drops_malformed_and_keeps_last_revision():
    rows = _rows([("2026-06-23", 300, 100, 5000),
                  ("2026-06-23", 310, 100, 5100)])             # 같은 보고일 정정 → 마지막
    rows.append({"report_date_as_yyyy_mm_dd": "2026-06-16T00:00:00.000",
                 "noncomm_positions_long_all": "abc",          # 비수치 → drop(가짜 0 금지)
                 "noncomm_positions_short_all": "1", "open_interest_all": "2"})
    df = cot_cftc.parse_rows(rows)
    assert len(df) == 1
    assert df["spec_net"].iloc[0] == 210.0


def test_fetch_all_market_atomicity_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)

    def fake(code):
        if code == "FAIL":
            return None                                        # 네트워크 실패 시장
        return _rows([("2026-06-16", 200, 50, 4000), ("2026-06-23", 300, 100, 5000)])

    markets = {"금선물": "088691", "원유선물": "FAIL"}
    res = cot_cftc.fetch_all(markets, fetch=fake)
    assert res["fail"] == 1 and res["markets"] == 1
    assert res["saved"]["금선물투기순포지션"] == 2              # 성공 시장은 2시리즈 저장
    assert "원유선물투기순포지션" not in res["saved"]           # 실패 시장은 두 시리즈 다 skip

    gold = df_mod._load_existing("금선물투기순포지션")
    assert len(gold) == 2 and gold["Close"].iloc[-1] == 200.0

    res2 = cot_cftc.fetch_all(markets, fetch=fake)             # 멱등 — 재수집해도 행수 동일
    assert len(df_mod._load_existing("금선물미결제약정")) == 2
    assert res2["saved"]["금선물투기순포지션"] == 2


def test_symbols_locked_to_feed_markets():
    """data_fetcher.MACRO_COT_SYMBOLS(리터럴) ↔ 피드 _MARKETS 파생명 정합 — 한쪽만 고치는
    드리프트를 CI가 차단(순환 import 회피를 위해 리터럴 이중정의 → 가드로 결속)."""
    derived = {m + s for m in cot_cftc._MARKETS for s in ("투기순포지션", "미결제약정")}
    assert set(df_mod.MACRO_COT_SYMBOLS) == derived
    assert set(cot_cftc.COT_SYMBOLS) == derived
