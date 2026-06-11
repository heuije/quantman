"""미국 예약주문(order-resv) body 구성·라우팅 단위 검증.

KIS 공식 spec(해외주식 예약주문접수 TTTT3014U/TTTT3016U)대로 발주 body를 만드는지 확인.
매수·매도 모두 **지정가(00)** — 예약매도 MOO(31)는 모의 지원 미검증이라 모의=실전 통일
위해 지정가로 발주한다. 실계좌·네트워크 없이 _post_retry를 가로채 body만 캡처한다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import market_index
from localapp.kis_broker import KisBroker


def _broker(virtual: bool):
    """__init__(자격증명 로드) 우회 — 발주 body 구성에 필요한 속성만 주입."""
    b = KisBroker.__new__(KisBroker)
    b.virtual = virtual
    b.cano = "12345678"
    b.acnt_cd = "01"
    return b


def _capture(b):
    """_post_retry를 가로채 (path, tr, body)를 기록하고 성공 응답 반환."""
    seen = {}

    def fake(path, tr, body, **kw):
        seen.update(path=path, tr=tr, body=body)
        return {"rt_cd": "0", "msg1": "정상", "msg_cd": "APBK", "output": {"ODNO": "0000123456"}}

    b._post_retry = fake
    return seen


@pytest.fixture(autouse=True)
def _identity_ticker(monkeypatch):
    # 마스터 인덱스 미로드 환경에서도 PDNO 정규화가 동작하도록 identity로 고정.
    monkeypatch.setattr(market_index, "kis_ticker_of", lambda s: s)


def test_buy_resv_limit_body_virtual():
    b = _broker(virtual=True)
    seen = _capture(b)
    r = b._submit_overseas_resv("AAPL", 3, "buy", 150.259, "NAS")

    assert r["success"] is True
    assert r["order_no"] == "0000123456"
    assert seen["path"] == "/uapi/overseas-stock/v1/trading/order-resv"
    assert seen["tr"] == "VTTT3014U"            # 모의 미국 예약매수
    body = seen["body"]
    assert body["ORD_DVSN"] == "00"             # 지정가
    assert body["FT_ORD_QTY"] == "3"
    assert body["FT_ORD_UNPR3"] == "150.26"     # 소수 USD($0.01) — 반올림
    assert body["OVRS_EXCG_CD"] == "NASD"
    assert body["PDNO"] == "AAPL"
    assert body["CANO"] == "12345678"
    assert body["ACNT_PRDT_CD"] == "01"


def test_buy_resv_limit_tr_real():
    b = _broker(virtual=False)
    seen = _capture(b)
    b._submit_overseas_resv("MSFT", 1, "buy", 400.0, "NAS")
    assert seen["tr"] == "TTTT3014U"            # 실전 미국 예약매수


def test_sell_resv_limit_body():
    """예약매도도 지정가(00) — MOO(31) 아님(모의=실전 통일). 가격을 body에 싣는다."""
    b = _broker(virtual=True)
    seen = _capture(b)
    r = b._submit_overseas_resv("TSLA", 5, "sell", 250.501, "NYS")

    assert r["success"] is True
    assert seen["tr"] == "VTTT3016U"            # 모의 미국 예약매도
    body = seen["body"]
    assert body["ORD_DVSN"] == "00"             # 지정가 (MOO 아님)
    assert body["FT_ORD_UNPR3"] == "250.50"     # 소수 USD($0.01) — 반올림
    assert body["FT_ORD_QTY"] == "5"
    assert body["OVRS_EXCG_CD"] == "NYSE"


def test_unsupported_market_no_call():
    """아시아 등 미지원 시장은 _post_retry 호출 없이 실패 반환."""
    b = _broker(virtual=True)
    called = {"n": 0}
    b._post_retry = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    r = b._submit_overseas_resv("7203", 1, "buy", 100.0, "TSE")
    assert r["success"] is False
    assert called["n"] == 0


def test_public_wrappers_route_by_exchange(monkeypatch):
    """buy_resv_limit/sell_resv_limit가 exchange_of로 시장을 정해 라우팅(양방향 지정가)."""
    monkeypatch.setattr(market_index, "exchange_of", lambda s: "AMS")
    b = _broker(virtual=True)
    seen = _capture(b)

    b.buy_resv_limit("FOO", 2, 12.345)
    assert seen["tr"] == "VTTT3014U"
    assert seen["body"]["ORD_DVSN"] == "00"
    assert seen["body"]["FT_ORD_UNPR3"] == "12.35"
    assert seen["body"]["OVRS_EXCG_CD"] == "AMEX"

    b.sell_resv_limit("FOO", 2, 9.871)
    assert seen["tr"] == "VTTT3016U"
    assert seen["body"]["ORD_DVSN"] == "00"      # 지정가 (MOO 아님)
    assert seen["body"]["FT_ORD_UNPR3"] == "9.87"


# ── Trader 라우팅: _reserved_us 플래그가 예약 broker 메서드로 분기시키는지 ──────

def _trader_with_stub(monkeypatch, currency: str):
    """__init__ 우회 + 부수효과 helper(intents/_after_submit) 무력화한 Trader."""
    from localapp import trader as tr

    monkeypatch.setattr(tr.intents, "new_intent_id", lambda: "i1")
    monkeypatch.setattr(tr.intents, "begin", lambda *a, **k: None)
    monkeypatch.setattr(tr.intents, "mark_submitted", lambda *a, **k: None)
    monkeypatch.setattr(tr.intents, "mark_failed", lambda *a, **k: None)
    monkeypatch.setattr(tr.Trader, "_after_submit", lambda *a, **k: None)
    monkeypatch.setattr(tr.intents, "is_active", lambda *a, **k: False)
    monkeypatch.setattr(tr, "_currency_of", lambda s: currency)

    broker = MagicMock()
    broker.buy_resv_limit.return_value = {"success": True, "order_no": "R1"}
    broker.sell_resv_limit.return_value = {"success": True, "order_no": "R2"}
    broker.buy_limit.return_value = {"success": True, "order_no": "L1"}
    broker.buy.return_value = {"success": True, "order_no": "M1"}
    broker.sell_limit.return_value = {"success": True, "order_no": "L2"}
    broker.sell.return_value = {"success": True, "order_no": "M2"}
    broker.price.return_value = 100.0        # _safe_price(=_us_limit fresh가) — 실수 보장
    return _new_trader(tr, broker), broker


def _new_trader(tr, broker):
    t = tr.Trader.__new__(tr.Trader)
    t.broker = broker
    return t


def test_submit_buy_routes_to_reserved_when_us(monkeypatch):
    t, broker = _trader_with_stub(monkeypatch, currency="USD")
    t._reserved_us = True
    policy = {"buy_tolerance_pct": 3.0}
    t._submit_buy("s1", "T", {}, "AAPL", 2, 100.0, policy, [], catchup=False)
    broker.buy_resv_limit.assert_called_once()
    broker.buy_limit.assert_not_called()
    broker.buy.assert_not_called()
    # limit = 신선한가(price=100) × (1+3%) = 103.0 (전일종가 ref_price=100과 무관하게 fresh)
    assert broker.buy_resv_limit.call_args[0][2] == 103.0


def test_submit_buy_fresh_price_overrides_stale_ref(monkeypatch):
    """예약매수 limit은 전일종가(ref_price)가 아니라 신선한 현재가(_safe_price)로 계산."""
    t, broker = _trader_with_stub(monkeypatch, currency="USD")
    t._reserved_us = True
    broker.price.return_value = 120.0        # 프리마켓 갭상승 (전일종가 100 대비 +20%)
    policy = {"buy_tolerance_pct": 3.0}
    t._submit_buy("s1", "T", {}, "AAPL", 2, 100.0, policy, [], catchup=False)
    # limit = 120 × 1.03 = 123.6 (전일종가 100×1.03=103이 아니라 fresh 기준) → 갭 미체결 회피
    assert broker.buy_resv_limit.call_args[0][2] == 123.6


def test_submit_sell_routes_to_reserved_limit_when_us(monkeypatch):
    """미국 예약매도 = 지정가(00) reserved (MOO 아님)."""
    t, broker = _trader_with_stub(monkeypatch, currency="USD")
    t._reserved_us = True
    policy = {"sell_tolerance_pct": 3.0}
    t._submit_sell("s1", "T", "AAPL", 2, 100.0, policy, "청산", [])
    broker.sell_resv_limit.assert_called_once()
    broker.sell_limit.assert_not_called()
    broker.sell.assert_not_called()
    # limit = 신선한가(100) × (1−3%) = 97.0 (개장가보다 낮아 매도 체결)
    assert broker.sell_resv_limit.call_args[0][2] == 97.0


def test_submit_sell_us_live_limit_when_flag_off(monkeypatch):
    """미국 종가청산(예약 아님) = 라이브 지정가 sell_limit — 시장가(broker.sell) 아님."""
    t, broker = _trader_with_stub(monkeypatch, currency="USD")
    t._reserved_us = False                   # 종가 청산 cycle은 신규 Trader라 항상 False
    policy = {"sell_tolerance_pct": 3.0}
    t._submit_sell("s1", "T", "AAPL", 2, 100.0, policy, "당일청산", [])
    broker.sell_limit.assert_called_once()
    broker.sell.assert_not_called()          # 국내 시장가 경로로 빠지지 않음
    broker.sell_resv_limit.assert_not_called()
    assert broker.sell_limit.call_args[0][2] == 97.0   # 100 × (1−3%)


def test_submit_sell_krx_market_when_flag_off(monkeypatch):
    """국내(KRW)는 예약 여부 무관 시장가(broker.sell) — US 분기로 새지 않음."""
    t, broker = _trader_with_stub(monkeypatch, currency="KRW")
    t._reserved_us = False
    policy = {"sell_tolerance_pct": 3.0}
    t._submit_sell("s1", "T", "005930", 2, 100.0, policy, "청산", [])
    broker.sell.assert_called_once()
    broker.sell_limit.assert_not_called()
    broker.sell_resv_limit.assert_not_called()


def test_submit_buy_no_reserve_when_flag_off(monkeypatch):
    t, broker = _trader_with_stub(monkeypatch, currency="USD")
    t._reserved_us = False
    policy = {"buy_tolerance_pct": 3.0}
    t._submit_buy("s1", "T", {}, "AAPL", 2, 100.0, policy, [], catchup=False)
    broker.buy_resv_limit.assert_not_called()
    broker.buy.assert_called_once()        # 예약 아님 → 즉시 경로(국내 시장가; US는 진입 cycle에서만 발생)


def test_submit_buy_no_reserve_for_krx_even_if_flag_on(monkeypatch):
    """USD가 아니면(국내) 예약 라우팅 안 함 — 같은 cycle에 섞여도 안전."""
    t, broker = _trader_with_stub(monkeypatch, currency="KRW")
    t._reserved_us = True
    policy = {"buy_tolerance_pct": 1.0}
    t._submit_buy("s1", "T", {}, "005930", 2, 100.0, policy, [], catchup=False)
    broker.buy_resv_limit.assert_not_called()
    broker.buy.assert_called_once()        # 국내 = 시장가


# ── M1: 거래소 코드 단일 출처(_OVERSEAS_EXCD) 회귀 ──────────────────────────────
# 이전엔 거래소 코드 맵이 kis_broker 안에 4벌 흩어져 있어(345·440·460·537) 한 곳을
# 고치고 다른 곳을 잊으면 잘못된 EXCD로 발주/조회되는 결함 class가 있었다. M1에서
# 단일 출처 _OVERSEAS_EXCD로 합쳤다. quote endpoint는 short(NAS), 주문/잔고는
# NASD-form을 같은 출처에서 끌어 쓰는지 고정한다.

def _capture_get(b):
    seen = {}

    def fake(path, tr, body, **kw):
        seen.update(path=path, tr=tr, body=body)
        return {"output": {}}

    b._get_retry = fake
    return seen


def test_open_overseas_uses_short_excd_from_single_source(monkeypatch):
    monkeypatch.setattr(market_index, "kis_ticker_of", lambda s: s)
    b = _broker(virtual=True)
    b.quote_base = "https://example"
    seen = _capture_get(b)
    # known 시장은 short 코드 그대로, _OVERSEAS_EXCD 키 집합으로 검증.
    b._open_overseas("AAPL", "NYS")
    assert seen["body"]["EXCD"] == "NYS"
    # 미지원/알 수 없는 시장은 "NAS"로 안전 기본.
    b._open_overseas("FOO", "ZZZ")
    assert seen["body"]["EXCD"] == "NAS"


def test_price_overseas_uses_short_excd_from_single_source(monkeypatch):
    monkeypatch.setattr(market_index, "kis_ticker_of", lambda s: s)
    b = _broker(virtual=True)
    b.quote_base = "https://example"
    seen = _capture_get(b)
    b._price_overseas("AAPL", "AMS")
    assert seen["body"]["EXCD"] == "AMS"


def test_buying_power_usd_uses_nasd_form_from_single_source(monkeypatch):
    monkeypatch.setattr(market_index, "kis_ticker_of", lambda s: s)
    monkeypatch.setattr(market_index, "exchange_of", lambda s: "NYS")
    b = _broker(virtual=True)
    seen = _capture_get(b)
    b.buying_power_usd("MSFT", 400.0)
    # 주문/잔고 계열은 NASD-form — 단일 출처 _OVERSEAS_EXCD 값과 일치.
    assert seen["body"]["OVRS_EXCG_CD"] == "NYSE"
    assert seen["body"]["OVRS_EXCG_CD"] == KisBroker._OVERSEAS_EXCD["NYS"]
