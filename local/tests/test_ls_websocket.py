"""LsWebSocket 단위 테스트 — 시세 파싱·구독 관리·도메스틱 필터 (live WS 불요)."""
import json

from localapp.ls_websocket import LsWebSocket


class _FakeBroker:
    def __init__(self, virtual=True):
        self.virtual = virtual

    def _token(self):
        return "TOK"


def test_on_data_s3_calls_on_tick():
    got = []
    ws = LsWebSocket(_FakeBroker(), lambda s, p: got.append((s, p)))
    ws._on_data("S3_", "005930", {"price": "73500", "shcode": "005930"})
    assert got == [("005930", 73500.0)]


def test_on_data_uses_shcode_over_trkey():
    """어느 TR(S3_/K3_)이 전달했든 body shcode로 종목 식별."""
    got = []
    ws = LsWebSocket(_FakeBroker(), lambda s, p: got.append((s, p)))
    ws._on_data("K3_", "ignored_trkey", {"price": "5000", "shcode": "035720"})
    assert got == [("035720", 5000.0)]


def test_on_data_skips_zero_or_bad_price():
    got = []
    ws = LsWebSocket(_FakeBroker(), lambda s, p: got.append((s, p)))
    ws._on_data("S3_", "005930", {"price": "0", "shcode": "005930"})
    ws._on_data("S3_", "005930", {"price": "", "shcode": "005930"})
    assert got == []


def test_on_data_ignores_non_quote_tr():
    got = []
    ws = LsWebSocket(_FakeBroker(), lambda s, p: got.append((s, p)))
    ws._on_data("SC1", "", {"price": "100", "shcode": "005930"})
    assert got == []


def test_is_domestic_stock():
    assert LsWebSocket._is_domestic_stock("005930") is True
    assert LsWebSocket._is_domestic_stock("000660") is True
    assert LsWebSocket._is_domestic_stock("코스피200선물") is False
    assert LsWebSocket._is_domestic_stock("") is False


def test_subscribe_filters_non_domestic():
    ws = LsWebSocket(_FakeBroker(), lambda s, p: None)
    n = ws.subscribe(["005930", "코스피200선물", "000660"])
    assert n == 2
    assert ws._symbols == {"005930", "000660"}


def test_subscribe_dedup():
    ws = LsWebSocket(_FakeBroker(), lambda s, p: None)
    assert ws.subscribe(["005930"]) == 1
    assert ws.subscribe(["005930"]) == 0   # 이미 구독


def test_sync_subscriptions_diff():
    ws = LsWebSocket(_FakeBroker(), lambda s, p: None)
    ws.subscribe(["005930", "000660"])
    res = ws.sync_subscriptions({"000660", "035720"})   # 005930 out, 035720 in
    assert res["added"] == 1 and res["removed"] == 1
    assert ws._symbols == {"000660", "035720"}


def test_sub_msg_via_base():
    ws = LsWebSocket(_FakeBroker(), lambda s, p: None)
    msg = json.loads(ws._sub_msg("S3_", "005930"))
    assert msg["header"]["token"] == "TOK"
    assert msg["header"]["tr_type"] == "1"
    assert msg["body"] == {"tr_cd": "S3_", "tr_key": "005930"}


def test_ws_url_inherited_port():
    assert ":29443/websocket" in LsWebSocket(_FakeBroker(True), lambda s, p: None)._ws_url
    assert ":9443/websocket" in LsWebSocket(_FakeBroker(False), lambda s, p: None)._ws_url
