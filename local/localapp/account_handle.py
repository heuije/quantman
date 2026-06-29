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
