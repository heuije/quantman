"""flow.institutional_13f 피드(SEC 13F Data Sets) 단위 테스트 — 네트워크 없음(합성 zip).

검증: SUBMISSION (CIK,분기) 최신 dedup·주보고분기 판정 / INFOTABLE 옵션·비주식 제외 /
필러 오기입 CUSIP별 중앙값 5배 이상치 제외 / VALUE 단위 자동감지(천달러→달러) /
CUSIP→ticker 매핑·미매핑 드랍 / as_of=분기말+45일 종목별 저장.
"""

from __future__ import annotations

import io
import zipfile

import pandas as pd

from quant_core import data_fetcher as df_mod
from quant_core.data.feeds import institutional_13f as f


_SUBMISSION = (
    "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
    "acc1\t15-MAY-2024\t13F-HR\t111\t31-MAR-2024\n"
    "acc3\t10-MAY-2024\t13F-HR\t111\t31-MAR-2024\n"      # CIK 111 구신고 → acc1이 supersede
    "acc2\t16-MAY-2024\t13F-HR\t222\t31-MAR-2024\n"
    "acc4\t14-MAY-2024\t13F-HR\t444\t31-MAR-2024\n"
    "accold\t20-MAY-2024\t13F-HR\t333\t31-DEC-2023\n")   # 다른 분기 → 제외

_INFOTABLE = (
    "ACCESSION_NUMBER\tCUSIP\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL\n"
    "acc1\t037833100\t100\t1000\tSH\t\n"                 # 내재 0.1
    "acc2\t037833100\t100\t1000\tSH\t\n"                 # 내재 0.1
    "acc4\t037833100\t50000\t1000\tSH\t\n"               # 내재 50 → 중앙값 5배 밖(오기입 제외)
    "acc3\t037833100\t9\t9\tSH\t\n"                      # keep_acc 아님(구신고) → 무시
    "acc2\t594918104\t200\t1000\tSH\t\n"                 # 내재 0.2
    "acc1\t594918104\t5\t50\tSH\tCall\n"                 # 옵션 → 제외
    "acc1\t594918104\t3\t30\tPRN\t\n")                   # 비주식 → 제외


def _zip() -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", _SUBMISSION)
        z.writestr("INFOTABLE.tsv", _INFOTABLE)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_prep_submissions_mode_period_and_cik_dedup():
    pp, keep = f._prep_submissions(f._read_tsv(_zip(), "SUBMISSION.TSV"))
    assert pp == pd.Timestamp("2024-03-31")             # 최빈 분기(3F 4건 vs 1건)
    assert keep == {"acc1", "acc2", "acc4"}             # CIK 최신만·다른분기 제외


def test_aggregate_excludes_options_nonshares_and_outliers():
    zf = _zip()
    pp, keep = f._prep_submissions(f._read_tsv(zf, "SUBMISSION.TSV"))
    med = f._cusip_medians(zf, keep)
    agg = f._apply_scale(f._aggregate_holdings(zf, keep, med))
    # 037833100: acc1+acc2 유효(acc4 오기입 제외) → shares 2000·holders 2
    a = agg.loc["037833100"]
    assert a["institutional_shares"] == 2000
    assert a["institutional_holders"] == 2
    # 594918104: acc2 주식 1건만(옵션·PRN 제외) → shares 1000·holders 1
    b = agg.loc["594918104"]
    assert b["institutional_shares"] == 1000
    assert b["institutional_holders"] == 1
    # 단위 자동감지: 내재가격 중앙값(0.1,0.2)≪1 → 천달러 → x1000
    assert a["institutional_value"] == 200 * 1000       # (100+100)*1000


def test_clean_rows_pure():
    df = pd.read_csv(io.StringIO(_INFOTABLE), sep="\t", dtype=str)
    rows = f._clean_rows(df, {"acc1", "acc2"})
    # acc1/acc2 주식행만(옵션·PRN·acc4 제외). 037833100 2행 + 594918104 1행(acc2)
    assert len(rows) == 3
    assert set(rows["CUSIP"]) == {"037833100", "594918104"}


def test_outlier_mask_keeps_single_holder():
    """holders=1(중앙값=자기값)은 교차검증 불가 → 보존(문서화된 잔여 한계)."""
    rows = pd.DataFrame({"CUSIP": ["X"], "value": [1e9], "shares": [1.0]})
    med = {"X": 1e9}
    assert f._outlier_keep_mask(rows, med).all()


def test_parse_ftd_pure():
    text = ("SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
            "20250303|037833100|AAPL|85|APPLE INC|8.91\n"
            "20250303|BADROW\n")                         # 짧은 행 → skip
    out = dict(f._parse_ftd(text))
    assert out == {"037833100": "AAPL"}


def test_label_key_orders_both_namings():
    # 구 분기명·신 롤링명 혼재 시간 단조(신규 우선 정렬용)
    assert f._label_key("2013q2") == (2013, 2)
    assert f._label_key("01dec2024-28feb2025") == (2024, 4)
    assert f._label_key("2024q1") < f._label_key("01dec2024-28feb2025")


def test_ingest_quarter_maps_and_saves(monkeypatch, tmp_path):
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)
    monkeypatch.setattr(f, "INSTITUTIONAL_DIR", tmp_path / "institutional")
    agg = pd.DataFrame(
        {"institutional_value": [2e5, 1e5], "institutional_shares": [2000.0, 1000.0],
         "institutional_holders": [2.0, 1.0]},
        index=pd.Index(["037833100", "999999999"], name="CUSIP"))
    stats = f.ingest_quarter(pd.Timestamp("2024-03-31"), agg,
                             {"037833100": "AAPL"})       # 999999999 미매핑
    assert stats == {"symbols": 1, "unmapped": 1}
    d = f.load_institutional("AAPL")
    assert list(d.index) == [pd.Timestamp("2024-05-15")]  # 분기말+45일
    assert d.iloc[-1]["institutional_shares"] == 2000
