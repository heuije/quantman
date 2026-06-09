"""KIS access token 캐시가 계정(실전/모의 + appkey)에 귀속되는지 회귀.

KIS 토큰은 발급 도메인(VTS=모의 / REAL=실전)에 묶인다. 단일 캐시 파일을 만료시각만
으로 재사용하면, 모의→실전(또는 반대) 전환 후 24h 이내 첫 호출에서 *이전 계정의
토큰*을 그대로 재사용해 인증 실패(발주·시세 끊김)를 일으킨다. 캐시는 계정 지문이
일치할 때만 적중해야 한다.

    cd platform/local && python -m pytest tests/test_kis_token_cache.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def _fake_token_source(tokens):
    it = iter(tokens)

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": next(it), "expires_in": 86400}

    return lambda *a, **k: _Resp()


def _creds(app_key, virtual):
    # 모의(virtual)면 시세용 실전 앱키가 필수 — 토큰 캐시 테스트는 _token()(주문)만 호출하므로
    # 시세 앱키 존재만 충족하면 됨(quote 토큰은 발급 안 함).
    return {"app_key": app_key, "app_secret": "sec", "virtual": virtual,
            "account_no": "12345678-01",
            "quote_app_key": "QKEY", "quote_app_secret": "qsec"}


def test_token_cache_not_shared_across_accounts(tmp_path, monkeypatch):
    """모의 broker가 캐시한 토큰을 실전 broker가 재사용하면 안 된다."""
    from localapp import kis_broker
    monkeypatch.setattr(kis_broker, "_TOKEN_CACHE", tmp_path / ".kis_token.json")
    monkeypatch.setattr(kis_broker.requests, "post",
                        _fake_token_source(["TOK_PAPER", "TOK_REAL"]))

    monkeypatch.setattr(kis_broker, "load_kis", lambda: _creds("PAPER_KEY", True))
    paper = kis_broker.KisBroker()
    assert paper._token() == "TOK_PAPER"

    monkeypatch.setattr(kis_broker, "load_kis", lambda: _creds("REAL_KEY", False))
    real = kis_broker.KisBroker()
    # 실전 broker는 모의 토큰(TOK_PAPER)을 재사용하지 않고 자기 토큰을 새로 발급해야 한다.
    assert real._token() == "TOK_REAL", \
        "실전 broker가 모의 계정 토큰을 재사용함 — 캐시가 계정에 귀속되지 않음"


def test_token_cache_reused_within_same_account(tmp_path, monkeypatch):
    """같은 계정에선 캐시를 재사용해 토큰을 재발급하지 않는다(rate-limit 보호)."""
    from localapp import kis_broker
    monkeypatch.setattr(kis_broker, "_TOKEN_CACHE", tmp_path / ".kis_token.json")
    monkeypatch.setattr(kis_broker.requests, "post",
                        _fake_token_source(["TOK_1", "TOK_2"]))
    monkeypatch.setattr(kis_broker, "load_kis", lambda: _creds("PAPER_KEY", True))

    b = kis_broker.KisBroker()
    first = b._token()
    second = b._token()           # 캐시 적중 — 새 발급 없음
    assert first == second == "TOK_1"
