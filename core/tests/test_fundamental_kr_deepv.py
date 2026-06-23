"""KR 펀더멘털 deep 백필 가드 — years 범위 확장 시 기존 얕은 종목을 강제 재수집.

fresh_days 스킵이 deepv 마커로 gating돼야 과거 이력 backfill이 막히지 않는다 — 기존 종목이
얕은 parquet으로 80일간 묶여 과거 PER이 안 채워지던 문제(삼성 2026-03 공백)의 근본수정.
"""
import pandas as pd

import quant_core.data.feeds.fundamental_kr as fk


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(fk, "default_manifest_path", lambda: tmp_path / "_manifest.json")
    (tmp_path / "fundamentals").mkdir(parents=True, exist_ok=True)


def test_fetch_deep_backfill_forces_shallow_reattempt(monkeypatch, tmp_path):
    """deepv 미완 종목은 fresh해도 강제 재수집, deepv 완료+fresh는 skip."""
    _isolate(monkeypatch, tmp_path)
    fetched: list = []
    monkeypatch.setattr(fk, "fetch_one",
                        lambda c, years: (fetched.append(c), (pd.DataFrame({"ttm_ni": [1.0]}), False))[1])

    # A·B 모두 최근 parquet(fresh). A만 deepv 완료(deep), B는 얕음(deepv 없음).
    fk.write_parquet_atomic(pd.DataFrame({"x": [1]}), fk._fund_path("A"))
    fk.write_parquet_atomic(pd.DataFrame({"x": [1]}), fk._fund_path("B"))
    fk._write_deepv("A")

    fk.fetch(["A", "B"], [2025, 2026], budget_calls=9000)

    assert fetched == ["B"]              # 얕은 B만 강제 재수집, deep된 A는 fresh로 skip
    assert fk._deepv_done("B")            # B도 이제 deep 완료 → 다음엔 fresh로 skip


def test_deepv_version_gating(monkeypatch, tmp_path):
    """deepv 마커 버전이 현재보다 낮으면 미완 판정 — 범위 재확장 시 재백필 트리거."""
    _isolate(monkeypatch, tmp_path)
    fk._write_deepv("X")
    assert fk._deepv_done("X")            # 방금 현재 버전으로 기록 → 완료
    monkeypatch.setattr(fk, "_DEEP_VERSION", fk._DEEP_VERSION + 1)
    assert not fk._deepv_done("X")        # 버전 상향 → 옛 마커는 미완(재백필 대상)


def test_deepv_written_on_empty(monkeypatch, tmp_path):
    """확정 무데이터(013)도 deepv 기록 — 매 cron 재시도 폭주 방지(빈결과 종목도 deep 완료 처리)."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(fk, "fetch_one", lambda c, years: (pd.DataFrame(), False))   # 빈
    fk.fetch(["Z"], [2025, 2026], budget_calls=9000)
    assert fk._deepv_done("Z") and fk._marker_path("Z").exists()
