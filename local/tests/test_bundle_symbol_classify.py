"""sync_client._classify_bundle_symbols — 번들 되먹임 루프 분류 seam 회귀 가드.

2026-07-13 유니버스 오염 인시던트의 근본: 파일명 stem을 `sym[0].isalpha()`로 티커 판정 →
한글·회사명·유닛/예약명 stem 1,146건을 해외 유니버스에 등록. shape predicate 전환 후
malformed stem이 overseas에 안 실리고 실티커·KR 코드는 유지되는지 고정한다.
"""
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parents[1]
_CORE = _LOCAL.parent / "core"
for _p in (str(_LOCAL), str(_CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from localapp.sync_client import _classify_bundle_symbols  # noqa: E402
from quant_core import data_fetcher as df  # noqa: E402


def test_classify_rejects_malformed_stems():
    macro = set(df.ALL_SYMBOLS)
    stems = [
        "AAPL", "BRK-B", "CON",        # 실티커(예약명 CON 포함) — overseas 유지
        "005930", "000660",            # KR 6자리 — kr_codes
        "AACT_U", "AAC_WS",            # 유닛/워런트 stem(언더스코어) — 거부
        "CON_", "PRN_",                # 예약명 sanitize stem — 거부
        "금", "삼성전자",               # 한글 표시명 — 거부(isalpha True였던 부류)
        "Apple Inc",                   # 회사명(공백) — 거부
        "JPM/D",                       # 우선주(슬래시) — 거부
    ]
    kr_codes, overseas = _classify_bundle_symbols(stems, macro)
    ov_codes = {o["code"] for o in overseas}
    assert ov_codes == {"AAPL", "BRK-B", "CON"}, f"malformed 제외·실티커 유지: {ov_codes}"
    assert set(kr_codes) == {"005930", "000660"}


def test_classify_skips_macro_symbols():
    macro = set(df.ALL_SYMBOLS)
    # 매크로 stem(한글/S&P500)은 overseas로 안 감 — ALL_SYMBOLS 멤버라 skip
    a_macro = next(iter(macro))
    kr_codes, overseas = _classify_bundle_symbols([a_macro, "AAPL"], macro)
    assert {o["code"] for o in overseas} == {"AAPL"}
    assert kr_codes == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
