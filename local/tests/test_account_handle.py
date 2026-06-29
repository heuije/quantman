"""account_handle — fingerprint·account_id 회전 단위 검증 (keyring 무의존: 매핑 store를 주입)."""
from localapp import account_handle as ah


def test_fingerprint_kis_uses_account_and_mode():
    # KIS: 계좌번호+mode가 식별자 (appkey 무관)
    f1 = ah.fingerprint("kis", {"account_no": "12345678-01", "virtual": True})
    f2 = ah.fingerprint("kis", {"account_no": "1234567801", "virtual": True})  # 하이픈만 차이
    f3 = ah.fingerprint("kis", {"account_no": "12345678-01", "virtual": False})  # mode 차이
    assert f1 == f2          # 하이픈 정규화 → 동일
    assert f1 != f3          # 모의↔실전 = 다른 fingerprint


def test_fingerprint_ls_uses_appkey_and_mode():
    # LS: appkey=계좌단위 → appkey+mode가 식별자 (account_no cosmetic, 무시)
    f1 = ah.fingerprint("ls", {"app_key": "AK", "account_no": "11", "virtual": True})
    f2 = ah.fingerprint("ls", {"app_key": "AK", "account_no": "99", "virtual": True})  # 계좌만 차이
    f3 = ah.fingerprint("ls", {"app_key": "BK", "account_no": "11", "virtual": True})  # appkey 차이
    assert f1 == f2          # LS는 계좌번호 무시 → 동일
    assert f1 != f3          # appkey 다르면 다른 계좌


def test_account_id_stable_then_rotates_on_fingerprint_change():
    store = {}  # 주입형 매핑 store (keyring 대체)
    id1 = ah.resolve_account_id("kis_credentials", "FP_A", store)
    id2 = ah.resolve_account_id("kis_credentials", "FP_A", store)  # 동일 fingerprint
    id3 = ah.resolve_account_id("kis_credentials", "FP_B", store)  # 변경(모의→실전 등)
    assert id1 == id2        # 안정(재시작·재호출에도 동일)
    assert id3 != id1        # fingerprint 변경 → 새 uuid 회전
    assert len(id1) >= 16    # opaque uuid


def test_current_handles_lists_registered_slots(monkeypatch):
    from localapp import account_handle as ah
    # 슬롯 로더 스텁: KIS 선물만 등록(모의)
    monkeypatch.setattr(ah, "_slot_creds", lambda: {
        "kis_futures_credentials": ("kis", "kr_futures",
                                    {"account_no": "12345678-03", "virtual": True}),
    })
    monkeypatch.setattr(ah, "_load_map", lambda: {})
    saved = {}
    monkeypatch.setattr(ah, "_save_map", lambda m: saved.update(m))
    hs = ah.current_handles()
    assert len(hs) == 1
    h = hs[0]
    assert h["broker"] == "kis" and h["mode"] == "paper"
    assert "kr_futures" in h["asset_classes"]
    assert h["account_id"] and "account_no" not in h and "app_key" not in h  # INV-SEC
    assert h["nickname"]                                                     # 자동 라벨 ≥1


def test_active_account_ids_for_active_broker(monkeypatch):
    from localapp import account_handle as ah
    monkeypatch.setattr(ah, "_slot_creds", lambda: {
        "kis_credentials": ("kis", "kr_equity", {"account_no": "11111111-01", "virtual": False}),
        "ls_futures_credentials": ("ls", "kr_futures", {"app_key": "AK", "account_no": "x", "virtual": True}),
    })
    # rotation map은 호출 간 유지돼야 account_id가 안정(keyring을 dict로 대체).
    store = {}
    monkeypatch.setattr(ah, "_load_map", lambda: store)
    monkeypatch.setattr(ah, "_save_map", lambda m: store.update(m))
    monkeypatch.setattr(ah, "get_active_broker", lambda: "kis")
    ids = ah.active_account_ids()
    handles = {h["account_id"] for h in ah.current_handles() if h["broker"] == "kis"}
    assert set(ids) == handles            # 활성 브로커(kis) 핸들만


def test_local_health_reports_handles_no_secrets(monkeypatch):
    from localapp import analytics, account_handle as ah
    monkeypatch.setattr(ah, "current_handles", lambda: [
        {"account_id": "abc123", "broker": "ls", "asset_classes": ["kr_futures"],
         "mode": "paper", "nickname": "LS 모의 국내선물"}])
    monkeypatch.setattr(ah, "active_account_ids", lambda: ["abc123"])
    h = analytics.local_health()
    assert h["account_handles"][0]["account_id"] == "abc123"
    assert h["active_account_ids"] == ["abc123"]
    blob = str(h)
    assert "app_key" not in blob and "appkey" not in blob   # INV-SEC: 키 미포함
