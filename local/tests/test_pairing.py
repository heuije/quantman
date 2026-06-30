"""pairing — poll_for_token 취소(stop_event) 단위 검증 (네트워크 무의존: 폴링 전 취소)."""
import threading

import pytest

from localapp import pairing


def test_poll_for_token_raises_on_preset_stop_event():
    # stop_event가 이미 set이면 루프 첫 진입에서 PairingCancelled — 첫 요청도 안 보낸다.
    ev = threading.Event()
    ev.set()
    with pytest.raises(pairing.PairingCancelled):
        pairing.poll_for_token("dummy-device-code", stop_event=ev)


def test_pairing_cancelled_is_distinct_from_timeout():
    # 취소(PairingCancelled)는 만료·시간초과(TimeoutError)와 구분돼야 gui가 '실패' 아닌
    # '취소'로 표시한다.
    assert issubclass(pairing.PairingCancelled, Exception)
    assert not issubclass(pairing.PairingCancelled, TimeoutError)
