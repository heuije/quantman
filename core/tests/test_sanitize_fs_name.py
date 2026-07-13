"""sanitize_fs_name — 심볼→파일명 안전화 SSOT 단위 테스트.

2026-07-13 실유저 무발주 인시던트: 실재 티커 CON(Concentra)·PRN이 파일명이 되면
Windows 로컬앱의 tar 추출이 [WinError]로 전체 중단됨. 정상 심볼은 byte-identical 보존,
예약장치명·금지문자·말미점공백만 결정적으로 remap해야 한다.
"""
import pytest

from quant_core.parquet_io import sanitize_fs_name


@pytest.mark.parametrize("s", ["AAPL", "코스피200선물", "005930", "COST", "BRK.B", "COIN"])
def test_normal_names_are_byte_identical(s):
    assert sanitize_fs_name(s) == s


@pytest.mark.parametrize("s", ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9", "con", "Prn"])
def test_windows_reserved_names_are_remapped(s):
    out = sanitize_fs_name(s)
    assert out == s + "_"
    # 확장자 무관 예약어라 remap 후 stem이 더는 예약명이 아니어야
    assert out.upper() not in {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def test_illegal_chars_replaced():
    assert sanitize_fs_name("BRK/B") == "BRK_B"     # 기존 '/'→'_' 동작 보존
    assert sanitize_fs_name("A:B") == "A_B"
    assert sanitize_fs_name('a"b') == "a_b"
    assert sanitize_fs_name("a\\b") == "a_b"


def test_trailing_dot_and_space_stripped():
    assert sanitize_fs_name("FOO.") == "FOO"
    assert sanitize_fs_name("FOO ") == "FOO"


def test_determinism_no_mapping_table_needed():
    # 같은 입력 → 같은 출력(원본↔안전키 1:1). write·read·bundle이 이 함수만 공유하면 정합.
    for s in ["CON", "AAPL", "BRK/B", "코스피200선물"]:
        assert sanitize_fs_name(s) == sanitize_fs_name(s)
