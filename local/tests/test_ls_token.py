"""LS access token 발급·캐시(계정 귀속) 회귀. 모의↔실전 전환 시 토큰 오재사용 금지."""
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
        def raise_for_status(self): pass
        def json(self): return {"access_token": next(it), "expires_in": 86400,
                                 "token_type": "Bearer"}
    return lambda *a, **k: _Resp()


def _creds(app_key, virtual):
    return {"app_key": app_key, "app_secret": "sec",
            "account_no": "5550-1234", "virtual": virtual}


def test_ls_token_issued_and_cached(tmp_path, monkeypatch):
    from localapp import ls_broker
    monkeypatch.setattr(ls_broker, "_TOKEN_CACHE", tmp_path / ".ls_token.json")
    monkeypatch.setattr(ls_broker.requests, "post",
                        _fake_token_source(["TOK_1", "TOK_2"]))
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("AK", True))
    b = ls_broker.LsBroker()
    assert b._token() == "TOK_1"
    assert b._token() == "TOK_1"   # 캐시 적중 — 재발급 없음


def test_ls_token_not_shared_across_accounts(tmp_path, monkeypatch):
    from localapp import ls_broker
    monkeypatch.setattr(ls_broker, "_TOKEN_CACHE", tmp_path / ".ls_token.json")
    monkeypatch.setattr(ls_broker.requests, "post",
                        _fake_token_source(["TOK_PAPER", "TOK_REAL"]))
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("PAPER", True))
    assert ls_broker.LsBroker()._token() == "TOK_PAPER"
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("REAL", False))
    assert ls_broker.LsBroker()._token() == "TOK_REAL", \
        "실전 broker가 모의 토큰 재사용 — 캐시가 계정에 귀속되지 않음"


def test_ls_token_refreshed_when_near_expiry(tmp_path, monkeypatch):
    """만료 30분 마진 이내 토큰은 재발급된다(LS 익일 07:00 만료 회전 보장)."""
    import json as _json
    from datetime import datetime, timedelta
    from localapp import ls_broker
    cache_file = tmp_path / ".ls_token.json"
    monkeypatch.setattr(ls_broker, "_TOKEN_CACHE", cache_file)
    monkeypatch.setattr(ls_broker.requests, "post",
                        _fake_token_source(["TOK_1", "TOK_2"]))
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("AK", True))
    b = ls_broker.LsBroker()
    assert b._token() == "TOK_1"                      # 최초 발급·캐시
    # 캐시 만료시각을 30분 마진 안(10분 후)으로 당김 → 다음 호출은 재발급해야 함
    cache = _json.loads(cache_file.read_text(encoding="utf-8"))
    cache[b._token_fp]["expires_at"] = (datetime.now() + timedelta(minutes=10)).isoformat()
    cache_file.write_text(_json.dumps(cache), encoding="utf-8")
    assert b._token() == "TOK_2", "만료 30분 마진 내 토큰이 재발급되지 않음"
