"""자산군 커버리지 — 전략·포지션이 요구하는 자산군의 자격증명이 로컬에 있는지.

자동매매 사이클의 "커버리지 게이트"가 쓰는 단일 출처(SSOT). 자격증명은 로컬 keyring
(secrets_store)에만 있으므로 이 판정은 로컬 전용(서버·코어는 자격증명을 모른다 — 보안 불변식).

카테고리 어휘는 core의 instrument_category와 동일: kr_equity | kr_futures | us_equity | us_futures.
계좌(자격증명 슬롯) → 커버 카테고리:
  - 주식 계좌: KIS(load_kis)는 국내+미국 주식을 한 계좌로 처리 → {kr_equity, us_equity}.
              LS(load_ls)는 국내주식 → {kr_equity}; 해외주식은 실전(virtual=False)만 + {us_equity}
              (LS 모의는 해외주식 미제공).
  - 국내선물 계좌(load_kis_futures / load_ls_futures)               → {kr_futures}.
  - 해외선물 계좌(load_kis_overseas_futures / load_ls_overseas_futures) → {us_futures}.
"""
from __future__ import annotations

from collections.abc import Iterable

from quant_core.exec_defaults import instrument_category

from . import secrets_store


def covered_categories() -> set[str]:
    """활성 브로커 + 등록된 자격증명 슬롯이 커버하는 자산군 집합."""
    cov: set[str] = set()
    if secrets_store.get_active_broker() == "ls":
        ls = secrets_store.load_ls()
        if ls:
            cov.add("kr_equity")
            # LS 해외주식은 실전 전용 — 모의(virtual)는 미제공(IGW40014/002US/01900,
            # ls_broker.py:176-179·297-315, 2026-06-23 실측). 모의 us_equity 전략은
            # 게이트가 skip(실패 발주·naked-leg 방지), 실전은 정상 커버.
            if not ls.get("virtual", True):
                cov.add("us_equity")
        if secrets_store.load_ls_futures():
            cov.add("kr_futures")
        if secrets_store.load_ls_overseas_futures():
            cov.add("us_futures")
    else:  # kis (기본)
        if secrets_store.load_kis():
            cov.update(("kr_equity", "us_equity"))   # KIS 주식계좌 = 국내+미국주식
        if secrets_store.load_kis_futures():
            cov.add("kr_futures")
        if secrets_store.load_kis_overseas_futures():
            cov.add("us_futures")
    return cov


def missing_categories(symbols: Iterable[str]) -> set[str]:
    """주어진 심볼들이 요구하는 자산군 중 자격증명 미등록(미커버)인 집합.

    빈 집합 = 전부 커버. instrument_category는 core 순수함수(데이터셋 심볼 분류)."""
    required = {instrument_category(s) for s in symbols if s}
    return required - covered_categories()
