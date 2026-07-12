"""P2-B S1 — 공식 KRX Open API 시장지표 피드 단위테스트.

추출기는 라이브 검증된 응답 샘플로, fetch는 주입 mock으로(네트워크 없음). 키 미설정 no-op 포함.
"""

from __future__ import annotations

import pandas as pd

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
_FUT = [
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 F 202609 (주간)", "ACC_OPNINT_QTY": "100"},
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 F 202609 (야간)", "ACC_OPNINT_QTY": "101"},  # 이중계산 방지
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 F 202612 (주간)", "ACC_OPNINT_QTY": "50"},
    {"PROD_NM": "코스닥150 선물", "ISU_NM": "코스닥150 F 202609 (주간)", "ACC_OPNINT_QTY": "30"},
    {"PROD_NM": "3년국채 선물", "ISU_NM": "3년국채 F 202609", "ACC_OPNINT_QTY": "9999"},      # 타상품 제외
]


def test_extractors():
    assert kx.extract_vkospi(_DRVPROD) == 92.71
    assert kx.extract_bond_index(_BOND_IDX) == 195.01
    assert kx.extract_ktb_yield(_KTS, "3") == 3.727
    assert kx.extract_ktb_yield(_KTS, "10") == 4.117      # 물가(1.494) 아닌 명목
    assert kx.extract_putcall(_OPT) == 0.77               # 77/100, 미니·코스닥 제외
    # 선물 OI: 주간 월물 합(100+50), 야간(101)·타상품(국채 9999) 제외
    assert kx.extract_futures_oi(_FUT, "코스피200 선물") == 150.0
    assert kx.extract_futures_oi(_FUT, "코스닥150 선물") == 30.0


_ETF_D1 = [
    {"ISU_CD": "A", "INVSTASST_NETASST_TOTAMT": "1000", "LIST_SHRS": "100", "NAV": "10"},
    {"ISU_CD": "B", "INVSTASST_NETASST_TOTAMT": "2000", "LIST_SHRS": "200", "NAV": "10"},
]
_ETF_D2 = [
    {"ISU_CD": "A", "INVSTASST_NETASST_TOTAMT": "1100", "LIST_SHRS": "110", "NAV": "10"},  # +10좌×10=+100
    {"ISU_CD": "B", "INVSTASST_NETASST_TOTAMT": "1900", "LIST_SHRS": "200", "NAV": "10"},  # 좌수불변(가격↓)→0
    {"ISU_CD": "C", "INVSTASST_NETASST_TOTAMT": "500", "LIST_SHRS": "50", "NAV": "10"},    # 신규(prev없음)→제외
]


def test_etf_aum_and_flow():
    assert kx.extract_etf_aum(_ETF_D2) == 3500.0          # 1100+1900+500
    # flow = Δ좌수×NAV: A=+100, B=0(가격효과 제외), C=신규제외
    assert kx.compute_etf_flow(_ETF_D1, _ETF_D2) == 100.0


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
    canned = {kx._SVC_DRVPROD: _DRVPROD, kx._SVC_BOND_IDX: _BOND_IDX,
              kx._SVC_KTS: _KTS, kx._SVC_FUT: _FUT}

    def fake(svc, bd, timeout=30):
        return canned[svc]

    res = kx.fetch_market_indicators("20260625", "20260626", fetch=fake)  # 목·금 2일
    assert res["ok"] and res["days"] == 2
    assert res["saved"]["코스피200변동성지수"] == 2
    assert res["saved"]["국고채10년"] == 2
    assert res["saved"]["코스피200선물미결제약정"] == 2

    vk = _df._load_existing("코스피200변동성지수")
    assert len(vk) == 2 and vk["Close"].iloc[0] == 92.71
    ktb10 = _df._load_existing("국고채10년")
    assert ktb10["Close"].iloc[-1] == 4.117
    oi = _df._load_existing("코스피200선물미결제약정")
    assert oi["Close"].iloc[-1] == 150.0      # 주간 합·야간 제외


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


# ── 만기물 패널 (S4) — 라이브 검증된 필드(2026-06-26) ──────────────────────────
_FUT_PANEL_ROWS = [
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 F 202609 (주간)", "TDD_OPNPRC": "1450",
     "TDD_HGPRC": "1460", "TDD_LWPRC": "1440", "TDD_CLSPRC": "1455", "SETL_PRC": "1455",
     "ACC_TRDVOL": "180000", "ACC_OPNINT_QTY": "145000"},
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 F 202609 (야간)", "TDD_CLSPRC": "1456",
     "ACC_OPNINT_QTY": "999"},                                    # 야간 제외
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 SP 2609-2612 (주간)", "TDD_CLSPRC": "12"},  # 스프레드 제외
    {"PROD_NM": "미니코스피200 선물", "ISU_NM": "미니코스피200 F 202609 (주간)", "TDD_CLSPRC": "1455"},  # 타상품 제외
    {"PROD_NM": "코스피200 선물", "ISU_NM": "코스피200 F 202612 (주간)", "TDD_CLSPRC": "1465",
     "SETL_PRC": "1465", "ACC_TRDVOL": "20", "ACC_OPNINT_QTY": "8000"},
    {"PROD_NM": "코스닥150 선물", "ISU_NM": "코스닥150 F 202609 (주간)", "TDD_OPNPRC": "1400.7",
     "TDD_HGPRC": "1488.2", "TDD_LWPRC": "1384.6", "TDD_CLSPRC": "1469.8", "SETL_PRC": "1469.8",
     "ACC_TRDVOL": "233538", "ACC_OPNINT_QTY": "437112"},               # KQ150 outright(2026-07-10 실측 형상)
]


def test_extract_futures_panel_outright_only():
    p = kx.extract_futures_panel(_FUT_PANEL_ROWS, "코스피200 선물")
    assert sorted(x["contract"] for x in p) == ["202609", "202612"]   # 야간·SP·미니·타상품 제외
    front = next(x for x in p if x["contract"] == "202609")
    assert front["Close"] == 1455.0 and front["High"] == 1460.0 and front["OI"] == 145000.0
    kq = kx.extract_futures_panel(_FUT_PANEL_ROWS, "코스닥150 선물")
    assert [x["contract"] for x in kq] == ["202609"] and kq[0]["Close"] == 1469.8


def test_fetch_futures_panel_saves_and_rebuilds(monkeypatch, tmp_path):
    """등록 상품 전체를 하루 1콜 응답에서 동시 추출·상품별 패널+연속물 저장."""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(_df, "FUTURES_PANEL_DIR", tmp_path / "futures_panel")

    def fake(svc, bd, timeout=30):
        return _FUT_PANEL_ROWS

    res = kx.fetch_futures_panel("20260625", "20260626", fetch=fake)   # 목·금 2일
    assert res["ok"] and res["rows"] == 6                              # (K200 2계약+KQ150 1계약) × 2일
    panel = _df.load_futures_panel("코스피200선물")
    assert sorted(panel["contract"].unique()) == ["202609", "202612"]
    cont = _df._load_existing("코스피200선물")                          # 연속물 서빙뷰 파생됨
    assert not cont.empty and cont["Close"].iloc[-1] == 1455.0         # 살아있는 최근월물(202609)
    kq_panel = _df.load_futures_panel("코스닥150선물")                  # KQ150도 같은 응답에서
    assert list(kq_panel["contract"].unique()) == ["202609"]
    kq_cont = _df._load_existing("코스닥150선물")
    assert not kq_cont.empty and kq_cont["Close"].iloc[-1] == 1469.8


def test_panel_symbols_locked_to_registry_and_catalog():
    """_FUT_PANEL(피드) ↔ data_fetcher.KRX_PANEL_FUTURES(카탈로그) ↔ exec_defaults(계약명세)
    정합 — 한쪽만 고치면 CI 실패. 특히 카탈로그 미등록 선물은 equity·USD로 조용히 오분류되므로
    (instrument_spec 기본값) 전 패널 심볼의 futures 등록을 잠근다."""
    from quant_core.exec_defaults import instrument_spec
    assert set(kx._FUT_PANEL) == set(_df.KRX_PANEL_FUTURES)
    for s in _df.KRX_PANEL_FUTURES:
        assert instrument_spec(s).asset_class == "futures", s
        assert instrument_spec(s).currency == "KRW", s
        assert _df.symbol_category(s) == "자산", s


def test_rebuild_skips_when_panel_shallower_than_existing(monkeypatch, tmp_path):
    """마이그레이션 안전: 얕은 패널(백필 진행중)이 기존 깊은 시리즈(옛 CSV 2010~)를 덮어쓰지 않음.
    (덮어쓰면 백필이 2010 도달 전까지 선물 백테스트가 얕은 데이터로 회귀)"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(_df, "FUTURES_PANEL_DIR", tmp_path / "futures_panel")
    deep = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 0.0},
                        index=pd.bdate_range("2010-01-04", "2010-03-01"))
    _df._save("코스피200선물", deep)                                    # 기존 깊은 시리즈
    panel = pd.DataFrame({"contract": "202609", "Open": 1400.0, "High": 1400.0, "Low": 1400.0,
                          "Close": 1400.0, "Settle": 1400.0, "Volume": 100.0, "OI": 100.0},
                         index=pd.to_datetime(["2026-06-25", "2026-06-26"]))
    _df.save_futures_panel("코스피200선물", panel)                      # 얕은 패널(최근분만)

    assert _df.rebuild_futures_continuous("코스피200선물") == 0         # 얕음 → skip
    kept = _df._load_existing("코스피200선물")
    assert kept.index.min() == pd.Timestamp("2010-01-04")             # 기존 깊은 시리즈 유지
