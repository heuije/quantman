"""해외선물 자격증명 슬롯 + 순수함수 단위검증."""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_overseas_creds_roundtrip(monkeypatch):
    import localapp.secrets_store as ss
    store = {}
    monkeypatch.setattr(ss.keyring, "set_password", lambda s, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(ss.keyring, "get_password", lambda s, k: store.get(k))
    assert ss.load_kis_overseas_futures() is None
    ss.save_kis_overseas_futures("AK", "SK", "80012345-08", virtual=False)
    c = ss.load_kis_overseas_futures()
    assert c == {"app_key": "AK", "app_secret": "SK",
                 "account_no": "80012345-08", "virtual": False}
