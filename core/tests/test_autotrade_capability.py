from quant_core.autotrade_capability import autotrade_capability, Capability


def test_kr_futures_open_all_four_cells():
    for broker in ("kis", "ls"):
        for mode in ("paper", "live"):
            cap = autotrade_capability(broker, mode, "kr_futures")
            assert cap.status == "ok", (broker, mode)


def test_overseas_futures_paper_blocked_pending_wiring():
    # Phase 1: 해외선물(KIS·LS 공통) 배선 전 — "준비 중"으로 통일 차단.
    # (LS 해외선물 모의는 본래 지원되나 배선이 Phase 2라 차단 — 사유를 "모의 미지원"으로 쓰지 않는다.)
    cap = autotrade_capability("kis", "paper", "us_futures")
    assert cap.status == "blocked"
    assert "준비" in cap.reason


def test_us_futures_blocked_in_phase1_pending_wiring():
    for broker in ("kis", "ls"):
        for mode in ("paper", "live"):
            assert autotrade_capability(broker, mode, "us_futures").status == "blocked"


def test_ls_us_equity_paper_blocked():
    cap = autotrade_capability("ls", "paper", "us_equity")
    assert cap.status == "blocked"
    assert "모의" in cap.reason


def test_kis_equity_verified_true():
    assert autotrade_capability("kis", "paper", "kr_equity").verified is True
    assert autotrade_capability("kis", "live", "us_equity").verified is True


def test_kr_futures_unverified_in_phase1():
    assert autotrade_capability("kis", "paper", "kr_futures").verified is False


def test_unknown_inputs_blocked_not_crash():
    assert autotrade_capability("kis", "paper", "jp_equity").status == "blocked"
    assert autotrade_capability("etrade", "paper", "kr_equity").status == "blocked"


def test_ls_kr_equity_ok_unverified():
    cap = autotrade_capability("ls", "live", "kr_equity")
    assert cap.status == "ok"
    assert cap.verified is False
