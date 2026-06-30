"""P1 — 해외 유니버스 rebuild(overwrite) + 안전 가드 (네트워크 없음, monkeypatch).

_rebuild_overseas_universe = NASDAQ Trader(권위·정제) ∪ KIS US master(거래가능)를 overwrite.
append(누적)가 아니라 rebuild라 정크/상장폐지가 빠지고, fetch 실패·비정상 카운트면 기존 보존.
"""

from app import main as appmain
from quant_core import data_fetcher
from quant_core.data.feeds import nasdaq_trader


def _capture_save(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(data_fetcher, "save_managed_overseas",
                        lambda rows: saved.update(rows=rows))
    return saved


def test_rebuild_overwrites_with_nt_union_kis(monkeypatch):
    nt = [{"code": f"S{i}", "name": f"n{i}", "kind": "stock"} for i in range(9000)]
    nt.append({"code": "AAPL", "name": "Apple", "kind": "stock"})
    monkeypatch.setattr(nasdaq_trader, "fetch", lambda: nt)
    monkeypatch.setattr(appmain.kis_master_cache, "get_master_list",
                        lambda: [{"ticker": "AAPL", "name": "Apple", "market": "NAS"},
                                 {"ticker": "KISONLY", "name": "X", "market": "NYS"},
                                 {"symbol": "005930", "name": "삼성", "market": "KOSPI"}])
    saved = _capture_save(monkeypatch)
    n = appmain._rebuild_overseas_universe()
    codes = {r["code"] for r in saved["rows"]}
    assert "AAPL" in codes and "S0" in codes        # NASDAQ Trader 권위 정의
    assert "KISONLY" in codes                       # KIS US master(거래가능) — 데이터⊇거래
    assert "005930" not in codes                    # KR(KOSPI)은 해외 아님 → 제외
    assert n == len(nt)


def test_rebuild_guard_skips_on_fetch_fail(monkeypatch):
    def _boom():
        raise RuntimeError("network")
    monkeypatch.setattr(nasdaq_trader, "fetch", _boom)
    saved = _capture_save(monkeypatch)
    assert appmain._rebuild_overseas_universe() == 0
    assert "rows" not in saved                      # overwrite 안 함 — 기존 유니버스 보존


def test_rebuild_guard_skips_on_insane_count(monkeypatch):
    monkeypatch.setattr(nasdaq_trader, "fetch",
                        lambda: [{"code": "AAPL", "name": "Apple", "kind": "stock"}])  # 1개=비정상
    saved = _capture_save(monkeypatch)
    assert appmain._rebuild_overseas_universe() == 0
    assert "rows" not in saved                      # 반쪽/빈 디렉터리로 덮어쓰기 방지
