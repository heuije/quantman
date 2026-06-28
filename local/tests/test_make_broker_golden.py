"""make_broker 객체 그래프 골든 — KIS byte-identical 불변식(P2 회귀 가드).

make_broker의 반환 '구조'(타입·leg·resolve 콜백)를 잠가, P2(라우터 optional stock leg)가
기존 KIS 2조합을 한 바이트도 안 바꾸는지 보장한다.

hermetic: 실 keyring·네트워크와 무관하게 — (1) 브로커/리졸버 __init__을 무력화하고
(2) make_broker가 분기에 *실제 조회하는 바인딩*을 패치한다. 주의: runner는 `load_kis`를
모듈 레벨(runner.load_kis)로 import하고, `get_active_broker`·futures 로더는 함수 내 지연
import(secrets_store 조회)다 — 그래서 패치 대상 모듈이 다르다(아래).
"""
import pytest

from localapp import runner, secrets_store
from localapp.broker_router import BrokerRouter
from localapp.futures_contracts import ContractResolver
from localapp.kis_broker import KisBroker
from localapp.kis_futures_broker import KisFuturesBroker


@pytest.fixture
def hermetic(monkeypatch):
    # 브로커·리졸버 생성자 무력화 — 구조만 검증(실 creds·네트워크 무관)
    monkeypatch.setattr(KisBroker, "__init__", lambda self: None)
    monkeypatch.setattr(KisFuturesBroker, "__init__", lambda self: None)
    monkeypatch.setattr(ContractResolver, "__init__", lambda self: None)

    def setcreds(*, kis=None, kis_fut=None, kis_ovf=None, broker="kis"):
        # load_kis: runner 모듈 레벨 바인딩(make_broker line 67)
        monkeypatch.setattr(runner, "load_kis", lambda: kis)
        # get_active_broker·futures 로더: 함수 내 지연 import → secrets_store 조회
        monkeypatch.setattr(secrets_store, "get_active_broker", lambda: broker)
        monkeypatch.setattr(secrets_store, "load_kis_futures", lambda: kis_fut)
        monkeypatch.setattr(secrets_store, "load_kis_overseas_futures", lambda: kis_ovf)
    return setcreds


_CREDS = {"app_key": "k", "app_secret": "s", "account_no": "123-01", "virtual": False}


def test_kis_stock_only_returns_bare_kisbroker(hermetic):
    hermetic(kis=_CREDS)
    b = runner.make_broker()
    assert type(b) is KisBroker            # bare — 라우터 미경유 (무변경)


def test_kis_stock_plus_kr_futures_returns_router(hermetic):
    from quant_core.futures_contract import dataset_for_contract
    hermetic(kis=_CREDS, kis_fut=_CREDS)
    b = runner.make_broker()
    assert type(b) is BrokerRouter
    assert type(b._stock) is KisBroker
    assert type(b._futures) is KisFuturesBroker
    assert b._d4c is dataset_for_contract  # dataset_for_code 미주입 = 기본값 (불변식)


def test_kis_no_credentials_raises(hermetic):
    hermetic()                             # 자격증명 전무
    with pytest.raises(RuntimeError):
        runner.make_broker()
