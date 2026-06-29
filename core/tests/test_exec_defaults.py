from quant_core.exec_defaults import instrument_spec, is_futures, instrument_category


def test_mini_kospi200_spec():
    sp = instrument_spec("미니코스피200선물")
    assert sp.asset_class == "futures"
    assert sp.multiplier == 50_000.0
    assert sp.currency == "KRW"
    assert sp.tick == 0.05
    assert is_futures("미니코스피200선물")
    assert instrument_category("미니코스피200선물") == "kr_futures"


def test_regular_kospi200_unchanged():
    sp = instrument_spec("코스피200선물")
    assert sp.multiplier == 250_000.0
