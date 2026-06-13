"""선물 브로커 하드닝 회귀 — 라이브 검증(2026-06-09 국내모의 60044290)에서 드러난 결함.

① odno 매칭: 발주응답 ODNO는 0패딩("0000003156"), 체결조회 odno는 공백패딩("      3156").
   lstrip("0")만으론 공백이 남아 매칭 실패 → trader가 체결을 영영 감지 못함(머니패스 치명).
② pending_orders: 미체결 쿼리가 취소주문(orgn_odno≠0)도 반환 → resting 신규주문으로 오보고.
③ 토큰 디스크 캐시: 없으면 프로세스/인스턴스마다 재발급 → KIS 앱키당 토큰 throttle(403).

필드명(qty=잔량·avg_idx=체결가·tot_ccld_qty=체결·rjct_qty)은 실측 결과 *정확*했다(파서 무결).

    cd platform/local && python -m pytest tests/test_futures_broker_hardening.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import kis_futures_broker as kfb
from localapp.kis_futures_broker import KisFuturesBroker, parse_ccnl_order_status, _canon_odno


# ── ① odno 정규화 (실측 VTTO5201R 형태) ─────────────────────────────────────────
def test_canon_odno_strips_space_and_zero_padding():
    assert _canon_odno("      3156") == "3156"   # 조회 공백패딩
    assert _canon_odno("0000003156") == "3156"   # 발주 0패딩
    assert _canon_odno(None) == "" and _canon_odno("0") == ""


def test_parse_ccnl_matches_zero_padded_order_against_space_padded_row():
    # 발주응답 ODNO(0패딩)로 조회 행(공백패딩 odno)을 찾아 체결가까지 정확히 반환해야 한다.
    resp = {"output1": [{"odno": "      3156", "orgn_odno": "0", "ord_qty": "1",
                         "tot_ccld_qty": "1", "qty": "0", "avg_idx": "1228.15000000",
                         "rjct_qty": "0"}]}
    s = parse_ccnl_order_status(resp, "0000003156")
    assert s["status"] == "filled"
    assert s["filled_qty"] == 1 and s["remain_qty"] == 0
    assert s["fill_price"] == 1228.15
    assert s["order_no"] == "3156"          # strip되어 반환


def test_parse_ccnl_resting_order_is_submitted():
    resp = {"output1": [{"odno": "      3342", "orgn_odno": "0", "ord_qty": "1",
                         "tot_ccld_qty": "0", "qty": "1", "ord_idx": "1189.30", "rjct_qty": "0"}]}
    s = parse_ccnl_order_status(resp, "3342")
    assert s["status"] == "submitted" and s["filled_qty"] == 0 and s["remain_qty"] == 1


# ── ② pending_orders 취소주문 제외 ──────────────────────────────────────────────
def test_pending_orders_excludes_cancel_artifacts():
    b = object.__new__(KisFuturesBroker)
    rows = [
        {"odno": "      3342", "orgn_odno": "0", "ord_qty": "1", "tot_ccld_qty": "0", "qty": "1"},     # 신규 resting
        {"odno": "      3355", "orgn_odno": "3342", "ord_qty": "1", "tot_ccld_qty": "0", "qty": "0"},  # 취소주문
    ]
    b._inquire_ccnl = lambda only_unfilled=False: {"output1": rows}
    pend = b.pending_orders()
    assert len(pend) == 1, "취소주문이 pending에 포함됨"
    assert pend[0]["order_no"] == "3342"


# ── ③ 토큰 디스크 캐시 ──────────────────────────────────────────────────────────
def _mk(fp):
    b = object.__new__(KisFuturesBroker)
    b.key, b.secret, b.base, b.virtual = "K", "S", "https://x", True
    b._token_fp = fp
    b._tok, b._tok_exp = None, 0.0
    return b


class _Resp:
    def __init__(self, tok):
        self.content = ('{"access_token":"%s","expires_in":86400}' % tok).encode("utf-8")

    def raise_for_status(self):
        pass


def test_token_disk_cache_shared_across_instances(tmp_path, monkeypatch):
    monkeypatch.setattr(kfb, "_FUT_TOKEN_CACHE", tmp_path / ".fut_token.json")
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp("TOK1")

    monkeypatch.setattr(kfb.requests, "post", fake_post)
    assert _mk("fpA")._token() == "TOK1"      # 최초 발급
    assert _mk("fpA")._token() == "TOK1"      # 다른 인스턴스 — 디스크 캐시 적중
    assert calls["n"] == 1, "재발급 발생 (캐시 미적중 → 403 throttle 위험)"


def test_token_cache_not_shared_across_fp(tmp_path, monkeypatch):
    monkeypatch.setattr(kfb, "_FUT_TOKEN_CACHE", tmp_path / ".fut_token.json")
    seq = iter(["TOKA", "TOKB"])
    monkeypatch.setattr(kfb.requests, "post", lambda *a, **k: _Resp(next(seq)))
    assert _mk("fpA")._token() == "TOKA"
    assert _mk("fpB")._token() == "TOKB"      # 다른 계정(fp) — 캐시 미적중, 각자 발급


# ── ④ US-F5: 주문/조회 POST·GET 하드닝 (국내선물 POST + 해외선물 전 메서드) ──────────
import json  # noqa: E402

import pytest  # noqa: E402


class _JsonResp:
    """_json(resp)=json.loads(content.decode)·raise_for_status 호환 가짜 응답."""
    def __init__(self, payload: dict, status: int = 200, raise_http: bool = False):
        self.content = json.dumps(payload).encode("utf-8")
        self.status_code = status
        self._raise = raise_http

    def raise_for_status(self):
        if self._raise:
            raise kfb.requests.HTTPError(f"{self.status_code} Server Error")


def _no_sleep(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)


def test_order_post_retries_egw00201_then_succeeds(monkeypatch):
    """주문 POST가 EGW00201(접수 전 rate-limit)에 한해 재시도 후 성공 — 종전엔 즉시 실패."""
    _no_sleep(monkeypatch)
    b = object.__new__(KisFuturesBroker)
    payloads = [
        {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당거래수 초과", "output": {}},
        {"rt_cd": "0", "msg_cd": "40600000", "output": {"ODNO": "0000001234"}},
    ]
    seq, calls = iter(payloads), {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _JsonResp(next(seq))
    monkeypatch.setattr(kfb.requests, "post", fake_post)

    resp = b._order_post("https://x/order", {}, {})
    assert calls["n"] == 2                       # EGW00201 → 재시도 → 성공
    assert resp["success"] is True and resp["order_no"]


def test_order_post_retries_egw00201_as_http_500(monkeypatch):
    """KIS는 rate-limit(EGW00201)을 HTTP 500 + 본문 msg_cd로도 준다 — 비-200 본문 msg_cd를
    먼저 확인해 재시도해야 한다(raise_for_status를 먼저 부르면 미작동). 주식 _post_retry 동형."""
    _no_sleep(monkeypatch)
    b = object.__new__(KisFuturesBroker)
    seq = iter([
        (_JsonResp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당거래수 초과"}, status=500), ),
        (_JsonResp({"rt_cd": "0", "msg_cd": "40600000", "output": {"ODNO": "0000005678"}}), ),
    ])
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return next(seq)[0]
    monkeypatch.setattr(kfb.requests, "post", fake_post)

    resp = b._order_post("https://x/order", {}, {})
    assert calls["n"] == 2                       # 500-EGW00201 → 재시도 → 성공
    assert resp["success"] is True and resp["order_no"]


def test_order_post_does_not_retry_5xx(monkeypatch):
    """EGW00201 아닌 5xx는 재시도 금지(접수됐을 수 있어 중복발주 위험) — 즉시 전파."""
    _no_sleep(monkeypatch)
    b = object.__new__(KisFuturesBroker)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        # 본문에 msg_cd 없음(순수 게이트웨이 5xx) → EGW00201 아님 → raise
        return _JsonResp({}, status=500, raise_http=True)
    monkeypatch.setattr(kfb.requests, "post", fake_post)

    with pytest.raises(kfb.requests.HTTPError):
        b._order_post("https://x/order", {}, {})
    assert calls["n"] == 1, "EGW00201 아닌 5xx 재시도하면 중복발주 위험"


def test_order_post_does_not_retry_normal_reject(monkeypatch):
    """EGW00201 아닌 거부(상/하한가 등)는 재시도 없이 정규형 그대로 반환(접수 여부 모호)."""
    _no_sleep(monkeypatch)
    b = object.__new__(KisFuturesBroker)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _JsonResp({"rt_cd": "1", "msg_cd": "40270000", "msg1": "상/하한가 오류"})
    monkeypatch.setattr(kfb.requests, "post", fake_post)

    resp = b._order_post("https://x/order", {}, {})
    assert calls["n"] == 1
    assert resp["success"] is False and resp["msg_cd"] == "40270000"


def test_read_get_retries_5xx_then_succeeds(monkeypatch):
    """읽기 GET(잔고·시세·체결조회)은 간헐 5xx에 재시도 — 해외 read도 _read_get 경유(US-F5)."""
    _no_sleep(monkeypatch)
    b = object.__new__(KisFuturesBroker)
    seq = iter([_JsonResp({}, status=500), _JsonResp({"output1": {"x": 1}}, status=200)])
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return next(seq)
    monkeypatch.setattr(kfb.requests, "get", fake_get)

    data = b._read_get("https://x", {}, {})
    assert calls["n"] == 2                        # 500 → 재시도 → 200
    assert data == {"output1": {"x": 1}}
