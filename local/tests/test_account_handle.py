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
