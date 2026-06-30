"""LsOrderWebSocket 단위 테스트 — 체결통보 정규화·메시지 라우팅 (live WS 불요).

SC1/C01 → KIS H0STCNI0 evt 키 정규화가 _on_exec_event 계약을 만족하는지 검증.
"""
import json

from localapp.ls_order_websocket import (
    LsOrderWebSocket,
    _norm_c01,
    _norm_sc1,
)


class _FakeBroker:
    def __init__(self, virtual=True):
        self.virtual = virtual

    def _token(self):
        return "FAKE_TOKEN"


class _Acct:
    def __init__(self, tok):
        self._tok = tok

    def _token(self):
        return self._tok


class _FakeRouter:
    """BrokerRouter 미러 — 주식·선물 별도 계좌(토큰). _stock/_futures 내부 속성."""

    def __init__(self, virtual=True):
        self.virtual = virtual
        self._stock = _Acct("STK_TOK")
        self._futures = _Acct("FUT_TOK")


class _CaptureWs:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(json.loads(msg))


def test_norm_sc1_maps_to_kis_keys():
    body = {"ordno": "86382", "shtnIsuno": "A005930", "execqty": "168",
            "execprc": "2598000", "exectime": "095636107", "gubun": "B"}
    evt = _norm_sc1(body)
    assert evt["CNTG_YN"] == "2"
    assert evt["ODER_NO"] == "86382"
    assert evt["STCK_SHRN_ISCD"] == "005930"   # A 접두 제거
    assert evt["CNTG_QTY"] == "168"
    assert evt["CNTG_UNPR"] == "2598000"
    assert evt["STCK_CNTG_HOUR"] == "095636107"
    assert evt["SELN_BYOV_CLS"] == "02"        # gubun B=매수
    assert evt["RFUS_YN"] == "N"


def test_norm_sc1_sell_side():
    evt = _norm_sc1({"ordno": "1", "shtnIsuno": "A000660", "execqty": "10",
                     "execprc": "100", "exectime": "0900", "gubun": "S"})
    assert evt["SELN_BYOV_CLS"] == "01"        # 매도


def test_norm_c01_maps_to_kis_keys():
    body = {"ordno": "0000034881", "expcode": "KR4101V30005", "chevol": "3",
            "cheprice": "1340.4", "chetime": "140833807", "dosugb": "2"}
    evt = _norm_c01(body)
    assert evt["CNTG_YN"] == "2"
    assert evt["ODER_NO"] == "0000034881"
    assert evt["STCK_SHRN_ISCD"] == "KR4101V30005"
    assert evt["CNTG_QTY"] == "3"
    assert evt["CNTG_UNPR"] == "1340.4"
    assert evt["STCK_CNTG_HOUR"] == "140833807"


def test_ws_url_port_by_virtual():
    assert ":29443/websocket" in LsOrderWebSocket(_FakeBroker(True), lambda e: None)._ws_url
    assert ":9443/websocket" in LsOrderWebSocket(_FakeBroker(False), lambda e: None)._ws_url


def test_sub_msg_token_trtype_trkey():
    ws = LsOrderWebSocket(_FakeBroker(), lambda e: None)
    msg = json.loads(ws._sub_msg("SC1", token="TOK"))
    assert msg["header"]["token"] == "TOK"
    assert msg["header"]["tr_type"] == "1"
    assert msg["body"] == {"tr_cd": "SC1", "tr_key": ""}
    # 해지
    assert json.loads(ws._sub_msg("SC1", sub=False, token="TOK"))["header"]["tr_type"] == "2"


def test_initial_subs_routes_account_tokens():
    """SC1=주식 계좌 토큰·C01=선물 계좌 토큰 (BrokerRouter·2026-06-26 _token 회귀 가드)."""
    ws = LsOrderWebSocket(_FakeRouter(), lambda e: None)
    cap = _CaptureWs()
    ws._initial_subs(cap)
    by_tr = {m["body"]["tr_cd"]: m["header"]["token"] for m in cap.sent}
    assert by_tr == {"SC1": "STK_TOK", "C01": "FUT_TOK"}


def test_initial_subs_stock_only_skips_c01():
    """선물 미설정(_futures 없음) → C01 구독 skip, SC1만(주식 토큰)."""
    ws = LsOrderWebSocket(_FakeBroker(), lambda e: None)   # 주식 단일 브로커
    cap = _CaptureWs()
    ws._initial_subs(cap)
    assert [m["body"]["tr_cd"] for m in cap.sent] == ["SC1"]
    assert cap.sent[0]["header"]["token"] == "FAKE_TOKEN"


def test_on_message_sc1_data_calls_on_exec():
    got = []
    ws = LsOrderWebSocket(_FakeBroker(), lambda e: got.append(e))
    ws._on_message(None, json.dumps({
        "header": {"tr_cd": "SC1"},
        "body": {"ordno": "86382", "shtnIsuno": "A005930", "execqty": "10",
                 "execprc": "60000", "exectime": "095636", "gubun": "B"}}))
    assert len(got) == 1
    assert got[0]["ODER_NO"] == "86382"
    assert got[0]["CNTG_QTY"] == "10"


def test_on_message_c01_data_calls_on_exec():
    got = []
    ws = LsOrderWebSocket(_FakeBroker(), lambda e: got.append(e))
    ws._on_message(None, json.dumps({
        "header": {"tr_cd": "C01"},
        "body": {"ordno": "40520", "expcode": "KR4101V30005", "chevol": "3",
                 "cheprice": "1414.17", "chetime": "130337", "dosugb": "2"}}))
    assert len(got) == 1
    assert got[0]["ODER_NO"] == "40520"
    assert got[0]["CNTG_UNPR"] == "1414.17"


def test_on_message_ack_does_not_call_on_exec():
    got = []
    ws = LsOrderWebSocket(_FakeBroker(), lambda e: got.append(e))
    ws._on_message(None, json.dumps({
        "header": {"tr_type": "1", "tr_cd": None, "rsp_cd": "00000",
                   "rsp_msg": "정상처리되었습니다"}, "body": None}))
    assert got == []


def test_on_message_non_json_ignored():
    got = []
    ws = LsOrderWebSocket(_FakeBroker(), lambda e: got.append(e))
    ws._on_message(None, "PINGPONG")
    ws._on_message(None, "")
    assert got == []


class _FuturesOnlyRouter:
    """선물 전용 LS 라우터 — 주식 계좌 미설정(_stock=None). 실 BrokerRouter처럼 언더스코어
    속성(_token) 접근을 AttributeError로 막아, 종전 'or self.broker' fallback이 router 자신을
    반환해 router._token()으로 SC1 구독을 거짓 실패시키던 버그(2026-06-30 LS 선물 모의)를 재현."""

    def __init__(self, virtual=True):
        self.virtual = virtual
        self._stock = None                 # 주식계좌 미설정 (선물 전용)
        self._futures = _Acct("FUT_TOK")

    def __getattr__(self, name):           # BrokerRouter.__getattr__ 미러 — _token 위임 차단
        raise AttributeError(name)


def test_stock_broker_none_when_router_has_no_stock():
    """주식계좌 없는 라우터(_stock=None) → _stock_broker()=None.

    종전 'getattr(...,"_stock",None) or self.broker'는 None or router → router 자신을 반환해
    router._token()이 AttributeError('_token')로 SC1 구독을 거짓 실패시켰다. 라우터면 _stock을
    그대로 반환(None이면 SC1 skip)해야 한다."""
    ws = LsOrderWebSocket(_FuturesOnlyRouter(), lambda e: None)
    assert ws._stock_broker() is None


def test_initial_subs_futures_only_router_skips_sc1_no_token_error():
    """선물 전용 라우터 → SC1(주식) 구독 skip, C01(선물)만 발송. _token 에러·예외 없음."""
    ws = LsOrderWebSocket(_FuturesOnlyRouter(), lambda e: None)
    cap = _CaptureWs()
    ws._initial_subs(cap)                  # raise 없어야 함(종전엔 _token 접근으로 진입)
    by_tr = {m["body"]["tr_cd"]: m["header"]["token"] for m in cap.sent}
    assert by_tr == {"C01": "FUT_TOK"}     # SC1 미발송(주식계좌 없음), C01만 선물 토큰으로


def test_normalized_evt_satisfies_on_exec_contract():
    """정규화 evt가 _on_exec_event가 읽는 키를 모두 포함 (계약 보장)."""
    for norm, body in (
        (_norm_sc1, {"ordno": "1", "shtnIsuno": "A005930", "execqty": "1",
                     "execprc": "100", "exectime": "0900", "gubun": "B"}),
        (_norm_c01, {"ordno": "1", "expcode": "X", "chevol": "1",
                     "cheprice": "1.0", "chetime": "0900", "dosugb": "2"}),
    ):
        evt = norm(body)
        for k in ("CNTG_YN", "ODER_NO", "STCK_SHRN_ISCD", "CNTG_QTY",
                  "CNTG_UNPR", "STCK_CNTG_HOUR", "RFUS_YN"):
            assert k in evt, f"{norm.__name__} 누락 키: {k}"
