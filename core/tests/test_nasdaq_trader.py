"""NASDAQ Trader 디렉터리 파서 — 보통주+ETF만, 워런트/우선주/테스트 제외, yf코드 정규화.

LP 보통단위(MLP)·ADR은 포함, SPAC 유닛은 제외하지 않음(데이터 자동큐레이션). 우선주는 심볼
특수문자($·-)로 차단. cd platform && PYTHONPATH=core pytest core/tests/test_nasdaq_trader.py -v
"""
from quant_core.data.feeds.nasdaq_trader import parse_directory

# nasdaqlisted: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot|ETF|NextShares
_NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|Q|N|N|100|Y|N
ABCDW|Acme Acquisition Corp - Warrant|S|N|N|100|N|N
ABCDU|Acme Acquisition Corp - Unit|S|N|N|100|N|N
TSTT|Nasdaq Test Issue Co|Q|Y|N|100|N|N
File Creation Time: 2026-06-30 18:00:00"""

# otherlisted: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|Test Issue|NASDAQ Symbol
_OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. Class B Common Stock|N|BRK.B|N|100|N|BRK B
JPM-A|JPMorgan Chase & Co. Depositary Shares Series A Preferred Stock|N|JPM-A|N|100|N|
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
GOOGL|Alphabet Inc. Class A Common Stock|N|GOOGL|N|100|N|GOOGL
TSM|Taiwan Semiconductor Manufacturing Company American Depositary Shares|N|TSM|N|100|N|
ET|Energy Transfer LP Common Units Representing Limited Partner Interests|N|ET|N|100|N|
AHL$D|Aspen Insurance Holdings Limited 5.625% Perpetual Series D|N|AHL$D|N|100|N|
File Creation Time: 2026-06-30 18:00:00"""


def test_includes_common_etf_adr_and_lp_units():
    by_code = {r["code"]: r for r in parse_directory(_NASDAQ, _OTHER)}
    assert by_code["AAPL"]["kind"] == "stock"
    assert by_code["QQQ"]["kind"] == "etf"
    assert by_code["SPY"]["kind"] == "etf"
    assert by_code["GOOGL"]["kind"] == "stock"
    assert by_code["TSM"]["kind"] == "stock"                # ADR 포함
    assert by_code["ET"]["kind"] == "stock"                 # LP 보통단위(MLP) 포함 — 오제외 금지
    assert by_code["ABCDU"]["kind"] == "stock"              # 유닛은 일괄제외 안 함(SPAC은 데이터 자동큐레이션)
    assert "BRK-B" in by_code                               # 클래스주 정규화 BRK.B→BRK-B
    assert "BRK.B" not in by_code
    assert "Apple" in by_code["AAPL"]["name"]


def test_excludes_warrant_preferred_testissue():
    by_code = {r["code"]: r for r in parse_directory(_NASDAQ, _OTHER)}
    for ex in ("ABCDW",            # 워런트(name)
               "TSTT",             # 테스트이슈
               "JPM-A",            # 우선주(심볼 '-')
               "AHL$D", "AHL-D"):  # 우선주(심볼 '$') — 정규화 전후 어느 형태도 없어야
        assert ex not in by_code, f"{ex} 제외돼야 함"


def test_dedupe_by_code():
    dup = _NASDAQ + "\nAAPL|Apple Inc. dup listing|Q|N|N|100|N|N"
    rows = parse_directory(dup, "ACT Symbol|x|x|x|x|x|x|x")
    assert sum(1 for r in rows if r["code"] == "AAPL") == 1


def test_handles_blank_and_malformed_lines():
    rows = parse_directory("Symbol|Security Name|h|h|h|h|h|h\n\n  \nGARBAGE", "ACT Symbol|h|h|h|h|h|h|h\n")
    assert rows == []
