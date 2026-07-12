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
    by_ticker: dict[str, dict] = {}

    def add(ticker: str, korean: str, english: str, exchange: str):
        t = _safe_str(ticker)
        if not t:
            return
        prev = by_ticker.get(t)
        if prev is not None:
            # S&P500은 지수(거래소 아님) — 먼저 적재된 S&P500 항목엔 뒤따르는 NASDAQ/NYSE
            # 리스트가 실제 거래소를 채운다. x는 시장 필터(attribute Market)의 원천이라
            # 지수 라벨로 남으면 대형주 500종목이 거래소 필터에서 전부 빠진다.
            if prev["x"] == "S&P500" and exchange in ("NASDAQ", "NYSE"):
                prev["x"] = exchange
            return
        rec = {"t": t, "k": _safe_str(korean), "e": _safe_str(english), "x": _safe_str(exchange)}
        by_ticker[t] = rec
        db.append(rec)

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


# ── 심볼 검색 (발견성 — WS5) ──────────────────────────────────────────────────
# 챗·서버가 이름/통용어로 정확한 심볼을 찾는 단일 choke. 프로덕션 실측 부류:
# ① 봇이 종목코드를 추측해 오코드 연쇄(노바렉스→217270/100220…, 코스맥스엔비티 3연속 오답)
# ② 매크로 심볼(원달러환율=DEXKOUS)이 실재하는데 USDKRW류 추측만 하다 "조회 불가" 포기.
# 주식(ticker_db 9,400여 종)과 매크로/선물/지수(data_fetcher ALL_SYMBOLS)를 한 인덱스로 검색한다.

_SEARCH_INDEX: list[dict] | None = None


def _norm(s: str) -> str:
    """검색 정규화 — 소문자화 + 공백·구분자 제거('원/달러 환율'→'원달러환율')."""
    return "".join(str(s or "").lower().split()).replace("/", "").replace("-", "")


def _build_search_index() -> list[dict]:
    entries: list[dict] = []
    for rec in load_db():
        t = rec.get("t") or ""
        # KR 티커는 ".KS" 접미를 뗀 6자리 코드가 엔진·챗의 심볼키다.
        code = t[:-3] if t.endswith(".KS") else t
        name = rec.get("k") or rec.get("e") or code
        entries.append({"symbol": code, "name": name, "kind": "주식",
                        "exchange": rec.get("x") or "",
                        "_keys": [_norm(rec.get("k")), _norm(rec.get("e")), _norm(code)]})
    # 매크로·선물·지수 — 심볼키 자체가 한글 표시명(data_fetcher SSOT). 카테고리를 kind로.
    from quant_core import data_fetcher as _df
    for sym in list(_df.ALL_SYMBOLS) + list(_df.PRICE_ALIAS):
        entries.append({"symbol": sym, "name": sym,
                        "kind": _df.symbol_category(sym), "exchange": "",
                        "_keys": [_norm(sym)]})
    return entries


def search_symbols(query: str, limit: int = 8) -> list[dict]:
    """이름·통용어 fuzzy 검색 → 후보 [{symbol,name,kind,exchange}] (관련도순).

    점수: 정확일치 > 접두 > 부분포함 > (이름⊂질의 — '코스맥스엔비티 종목코드' 같은 문장 질의).
    반환 symbol은 엔진/챗이 그대로 쓰는 키(KR=6자리 코드·US=티커·매크로=한글 심볼키)."""
    global _SEARCH_INDEX
    q = _norm(query)
    if not q:
        return []
    if _SEARCH_INDEX is None:
        _SEARCH_INDEX = _build_search_index()
    scored: list[tuple[int, dict]] = []
    for e in _SEARCH_INDEX:
        best = 0
        for k in e["_keys"]:
            if not k:
                continue
            if k == q:
                s = 100
            elif k.startswith(q):
                s = 80
            elif q in k:
                s = 60
            elif len(k) >= 2 and k in q:
                # 이름⊂질의('코스맥스엔비티 종목코드')는 긴 이름일수록 특정적 — '스맥' 같은
                # 짧은 우연 포함이 이기지 않게 길이 가점(60 미만 유지 = 부분포함 티어 아래).
                s = min(59, 50 + len(k))
            elif len(q) >= 4 and len(k) >= 4 and k[:3] == q[:3]:
                s = 40   # 끝글자 오타 관용(공통 접두 3자·prod 실측: '노바렉수'·'носва렉스'류)
            else:
                continue
            best = max(best, s)
        if best:
            scored.append((best, e))
    scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))
    return [{"symbol": e["symbol"], "name": e["name"], "kind": e["kind"],
             "exchange": e["exchange"]} for _, e in scored[:limit]]


if __name__ == "__main__":
    build_db()
