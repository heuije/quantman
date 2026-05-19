"""
티커 데이터베이스 빌더.
KRX 한국 종목 (KOSPI + KOSDAQ) + 주요 글로벌 종목 (S&P500 + NASDAQ + NYSE)
→ data/ticker_db.json 저장 (자동완성 로컬 DB)

각 항목: {"t": ticker, "k": 한국어명, "e": 영어명, "x": 거래소}
"""

import json
import pandas as pd
import FinanceDataReader as fdr
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "ticker_db.json"


def _safe_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def build_db(verbose: bool = True) -> list[dict]:
    db: list[dict] = []
    seen: set[str] = set()

    def add(ticker: str, korean: str, english: str, exchange: str):
        t = _safe_str(ticker)
        if not t or t in seen:
            return
        seen.add(t)
        db.append({"t": t, "k": _safe_str(korean), "e": _safe_str(english), "x": _safe_str(exchange)})

    # ── KRX (KOSPI + KOSDAQ) ─────────────────────────────────────────────────
    for market in ["KOSPI", "KOSDAQ"]:
        if verbose:
            print(f"  {market} 로딩 중...")
        try:
            df = fdr.StockListing(market)
            # FDR KRX 컬럼: Code (6자리), Name (한국어)
            code_col = next((c for c in ["Code", "Symbol"] if c in df.columns), None)
            name_col = next((c for c in ["Name", "종목명"] if c in df.columns), None)
            if code_col and name_col:
                for _, row in df.iterrows():
                    code = _safe_str(row[code_col])
                    name_kr = _safe_str(row[name_col])
                    if code:
                        add(f"{code}.KS", name_kr, "", market)
            if verbose:
                print(f"    → {len([x for x in db if x['x'] == market])}종목")
        except Exception as e:
            if verbose:
                print(f"    [오류] {market}: {e}")

    # ── 미국 (S&P500 + NASDAQ + NYSE) ────────────────────────────────────────
    us_counts_before = len(db)
    for market in ["S&P500", "NASDAQ", "NYSE"]:
        if verbose:
            print(f"  {market} 로딩 중...")
        try:
            df = fdr.StockListing(market)
            sym_col  = next((c for c in ["Symbol", "Code"] if c in df.columns), None)
            name_col = next((c for c in ["Name", "종목명"] if c in df.columns), None)
            if sym_col and name_col:
                for _, row in df.iterrows():
                    sym  = _safe_str(row[sym_col])
                    name = _safe_str(row[name_col])
                    if sym:
                        add(sym, "", name, market)
            if verbose:
                print(f"    → {len([x for x in db if x['x'] == market])}종목 (누적 추가)")
        except Exception as e:
            if verbose:
                print(f"    [오류] {market}: {e}")

    if verbose:
        us_count = len(db) - us_counts_before
        print(f"  미국 종목 합계: {us_count}개")

    # ── 저장 ─────────────────────────────────────────────────────────────────
    DB_PATH.parent.mkdir(exist_ok=True)
    DB_PATH.write_text(json.dumps(db, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if verbose:
        print(f"\n데이터베이스 저장 완료: {DB_PATH}")
        print(f"총 {len(db):,}개 종목")
    return db


def load_db() -> list[dict]:
    """DB 파일이 있으면 로드, 없으면 빌드."""
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return build_db(verbose=False)


if __name__ == "__main__":
    build_db()
