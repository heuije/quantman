from quant_core.exec_defaults import instrument_spec, is_futures, instrument_category


def test_mini_kospi200_spec():
    sp = instrument_spec("미니코스피200선물")
    assert sp.asset_class == "futures"
    assert sp.multiplier == 50_000.0
    assert sp.currency == "KRW"
    # 미니 호가단위는 정규(0.05)와 다르다 — 실측 2026-07-20(LS t8435 마스터 + 0.05
    # 배수 지정가의 `01403 호가단위` 거부). 데이터가 정규 앨리어스라 놓치기 쉬운 값.
    assert sp.tick == 0.02
    assert is_futures("미니코스피200선물")
    assert instrument_category("미니코스피200선물") == "kr_futures"


def test_regular_kospi200_unchanged():
    sp = instrument_spec("코스피200선물")
    assert sp.multiplier == 250_000.0
    assert sp.tick == 0.05          # 미니 교정(0.02)이 정규로 번지지 않았는지 고정
