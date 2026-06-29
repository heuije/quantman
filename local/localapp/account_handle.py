"""계좌 핸들 — 슬롯 자격증명을 비민감 핸들(opaque account_id + 메타)로.

account_id는 로컬 랜덤 uuid(서버엔 이것만). fingerprint(KIS=계좌번호+mode, LS=appkey+mode)별로
안정 — fingerprint가 바뀌면(모의→실전 재등록 등) 새 uuid로 회전해 옛 핸들 바인딩을 자동 무력화한다.
INV-SEC: app_key/secret/account_no 값은 핸들에 미포함(fingerprint는 단방향 해시, 로컬 store에만).
"""
from __future__ import annotations
import hashlib
import json
import uuid

import keyring

from .config import KEYRING_SERVICE
from .secrets_store import (get_active_broker, load_kis, load_kis_futures,
                            load_kis_overseas_futures, load_ls, load_ls_futures,
                            load_ls_overseas_futures)

_HANDLE_MAP = "account_handles"   # keyring: {slot_key: {"account_id":..., "fingerprint":..., "nickname":...}}


def fingerprint(broker: str, creds: dict) -> str:
    """슬롯의 안정 식별자(단방향). KIS=계좌번호+mode, LS=appkey+mode(계좌번호 cosmetic)."""
    mode = "v" if creds.get("virtual", True) else "r"
    if broker == "ls":
        ident = str(creds.get("app_key", ""))        # appkey=계좌단위
    else:
        ident = str(creds.get("account_no", "")).replace("-", "").strip()  # KIS=계좌번호
    return hashlib.sha256(f"{broker}|{ident}|{mode}".encode()).hexdigest()[:24]


def resolve_account_id(slot_key: str, fp: str, store: dict) -> str:
    """slot_key의 account_id를 store에서 가져오되, fingerprint가 바뀌었으면 새 uuid 발급."""
    ent = store.get(slot_key)
    if ent and ent.get("fingerprint") == fp:
        return ent["account_id"]
    new_id = uuid.uuid4().hex
    store[slot_key] = {"account_id": new_id, "fingerprint": fp,
                       "nickname": (ent or {}).get("nickname", "")}
    return new_id


def _load_map() -> dict:
    raw = keyring.get_password(KEYRING_SERVICE, _HANDLE_MAP)
    return json.loads(raw) if raw else {}


def _save_map(m: dict) -> None:
    keyring.set_password(KEYRING_SERVICE, _HANDLE_MAP, json.dumps(m))


# slot_key → (broker, asset_class, loader)
_SLOTS = [
    ("kis_credentials",                   "kis", "kr_equity",        load_kis),
    ("kis_futures_credentials",           "kis", "kr_futures",       load_kis_futures),
    ("kis_overseas_futures_credentials",  "kis", "us_futures",       load_kis_overseas_futures),
    ("ls_credentials",                    "ls",  "kr_equity",        load_ls),
    ("ls_futures_credentials",            "ls",  "kr_futures",       load_ls_futures),
    ("ls_overseas_futures_credentials",   "ls",  "us_futures",       load_ls_overseas_futures),
]

_LABEL = {"kr_equity": "국내주식", "kr_futures": "국내선물", "us_futures": "해외선물"}


def _slot_creds() -> dict:
    """등록된 슬롯만 {slot_key: (broker, asset_class, creds)} — 테스트 스텁 지점."""
    out = {}
    for key, broker, ac, loader in _SLOTS:
        c = loader()
        if c:
            out[key] = (broker, ac, c)
    return out


def current_handles() -> list[dict]:
    """등록 슬롯 → 핸들 목록(비민감). account_id 회전 매핑을 persist."""
    store = _load_map()
    handles = []
    for slot_key, (broker, ac, creds) in _slot_creds().items():
        fp = fingerprint(broker, creds)
        aid = resolve_account_id(slot_key, fp, store)
        mode = "paper" if creds.get("virtual", True) else "live"
        nick = store[slot_key].get("nickname") or \
            f"{broker.upper()} {'모의' if mode == 'paper' else '실전'} {_LABEL.get(ac, ac)}"
        handles.append({"account_id": aid, "broker": broker,
                        "asset_classes": [ac], "mode": mode, "nickname": nick})
    _save_map(store)
    return handles


def active_account_ids() -> list[str]:
    """활성 브로커의 핸들 account_id 집합 — 사이클 가드(P5-3)가 멤버십 검사."""
    ab = get_active_broker()
    return [h["account_id"] for h in current_handles() if h["broker"] == ab]
