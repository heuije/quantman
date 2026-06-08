"""ContractResolver.resolve_expiry — wrapper 가드 경로 단위검증 (M6, 네트워크 없음).

순수 해석(front_contract)은 core(test_futures_contract.py)에서 결정적으로 검증한다. 여기선
wrapper가 추가하는 로직만 본다: ① 비선물은 즉시 (None,None)(네트워크 안 탐), ② 마스터
미수신(다운로드 실패)이면 (None,None). 캐시(_fetched)를 오늘로 세팅해 _download(네트워크)를
우회한다 — 양성 해석 경로는 core front_contract 테스트가 담당.
"""
from __future__ import annotations

import datetime

from localapp.futures_contracts import ContractResolver


def test_resolve_expiry_equity_no_network():
    # 주식은 is_futures=False → 즉시 (None,None), _ensure(다운로드) 호출 안 함
    cr = ContractResolver()
    assert cr.resolve_expiry("005930") == (None, None)
    assert cr._fetched is None          # 네트워크 미접근 확인


def test_resolve_expiry_master_missing_returns_none():
    # 마스터 다운로드 실패(_dom/_ov=None) 상황 — 캐시를 오늘로 세팅해 _download 우회
    cr = ContractResolver()
    cr._fetched = datetime.date.today()
    cr._dom = None
    cr._ov = None
    assert cr.resolve_expiry("금선물") == (None, None)
    assert cr.resolve_expiry("코스피200선물") == (None, None)
