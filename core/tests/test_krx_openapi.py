"""P2-B S1 — 공식 KRX Open API 시장지표 피드 단위테스트.

추출기는 라이브 검증된 응답 샘플로, fetch는 주입 mock으로(네트워크 없음). 키 미설정 no-op 포함.
"""

from __future__ import annotations

from quant_core.data.feeds import krx_openapi as kx
from quant_core import data_fetcher as _df


# ── 라이브 검증된 응답 샘플 (2026-06-26) ──────────────────────────────────────
_DRVPROD = [
    {"IDX_NM": "KRX 300", "CLSPRC_IDX": "1500.0"},
    {"IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "92.71"},
]
_BOND_IDX = [
    {"BND_IDX_GRP_NM": "KRX 채권지수", "TOT_EARNG_IDX": "195.01"},
    {"BND_IDX_GRP_NM": "KTB 지수", "TOT_EARNG_IDX": "15983.18"},
]
_KTS = [
    {"ISU_NM": "국고03500-2906(26-5)", "BND_EXP_TP_NM": "3",
     "GOVBND_ISU_TP_NM": "지표", "CLSPRC_YD": "3.727"},
    {"ISU_NM": "물가01125-3606(26-4)", "BND_EXP_TP_NM": "10",
     "GOVBND_ISU_TP_NM": "지표", "CLSPRC_YD": "1.494"},      # 물가연동 — 제외돼야
    {"ISU_NM": "국고04250-3606(26-6)", "BND_EXP_TP_NM": "10",
     "GOVBND_ISU_TP_NM": "지표", "CLSPRC_YD": "4.117"},      # 명목 10년 — 선택돼야
    {"ISU_NM": "국고02250-2806(25-4)", "BND_EXP_TP_NM": "3",
     "GOVBND_ISU_TP_NM": "경과", "CLSPRC_YD": "3.587"},      # 경과물 — 제외
]
_OPT = [
    {"PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "CALL", "ACC_TRDVOL": "100"},
    {"PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "PUT", "ACC_TRDVOL": "77"},
    {"PROD_NM": "코스피200 위클리(목) 옵션", "RGHT_TP_NM": "CALL", "ACC_TRDVOL": "0"},
    {"PROD_NM": "미니코스피200 옵션", "RGHT_TP_NM": "PUT", "ACC_TRDVOL": "9999"},  # 미니 — 제외
    {"PROD_NM": "코스닥150 옵션", "RGHT_TP_NM": "PUT", "ACC_TRDVOL": "8888"},      # 제외
]


def test_extractors():
    assert kx.extract_vkospi(_DRVPROD) == 92.71
    assert kx.extract_bond_index(_BOND_IDX) == 195.01
    assert kx.extract_ktb_yield(_KTS, "3") == 3.727
    assert kx.extract_ktb_yield(_KTS, "10") == 4.117      # 물가(1.494) 아닌 명목
    assert kx.extract_putcall(_OPT) == 0.77               # 77/100, 미니·코스닥 제외


def test_extractor_missing_returns_none():
    assert kx.extract_vkospi([]) is None
    assert kx.extract_ktb_yield(_KTS, "20") is None        # 없는 만기
    assert kx.extract_putcall([{"PROD_NM": "코스피200 옵션",
                                "RGHT_TP_NM": "PUT", "ACC_TRDVOL": "5"}]) is None  # CALL 0 → None


def test_inactive_without_key(monkeypatch):
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    assert kx.is_active() is False
    assert kx.fetch_market_indicators("20260625", "20260626").get("inactive") is True
    assert kx.fetch_putcall("20260625", "20260626").get("inactive") is True


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("KRX_API_KEY", "testkey")
    monkeypatch.setattr(_df, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_df, "mark_data_dirty", lambda: None)


def test_fetch_market_indicators_saves_series(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    canned = {kx._SVC_DRVPROD: _DRVPROD, kx._SVC_BOND_IDX: _BOND_IDX, kx._SVC_KTS: _KTS}

    def fake(svc, bd, timeout=30):
        return canned[svc]

    res = kx.fetch_market_indicators("20260625", "20260626", fetch=fake)  # 목·금 2일
    assert res["ok"] and res["days"] == 2
    assert res["saved"]["코스피200변동성지수"] == 2
    assert res["saved"]["국고채10년"] == 2

    vk = _df._load_existing("코스피200변동성지수")
    assert len(vk) == 2 and vk["Close"].iloc[0] == 92.71
    ktb10 = _df._load_existing("국고채10년")
    assert ktb10["Close"].iloc[-1] == 4.117


def test_fetch_network_fail_skips_day_not_marked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def fake(svc, bd, timeout=30):
        return None        # 네트워크 실패

    res = kx.fetch_market_indicators("20260626", "20260626", fetch=fake)
    assert res["ok"] is False and res["days"] == 0


def test_putcall_saves(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def fake(svc, bd, timeout=30):
        return _OPT

    res = kx.fetch_putcall("20260625", "20260626", fetch=fake)
    assert res["saved"]["옵션풋콜비율"] == 2
    pc = _df._load_existing("옵션풋콜비율")
    assert pc["Close"].iloc[0] == 0.77
