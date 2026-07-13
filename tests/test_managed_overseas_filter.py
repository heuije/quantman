"""save_managed_overseas — malformed 해외 심볼 원천 차단 검증(shape predicate).

KIS 미국 마스터의 우선주·유닛·클래스주('/' 표기)는 yfinance가 못 받고, 번들 되먹임 루프가
파일명 stem(유닛 'AACT_U'·예약명 sanitize 'CON_'·한글 표시명 '금'·회사명 'Apple Inc')을
티커로 오분류해 유니버스를 오염시킨다(실측 1,146건). shape predicate 1개(SSOT)로 유일한
write 경로에서 차단하고, 기존 항목도 다음 저장 때 정리되는지(self-clean) 고정한다.

    cd platform && pytest tests/test_managed_overseas_filter.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core import data_fetcher as df  # noqa: E402


@pytest.fixture
def _tmp_path(tmp_path, monkeypatch):
    p = tmp_path / "managed_overseas_stocks.json"
    monkeypatch.setattr(df, "MANAGED_OVERSEAS_PATH", p)
    return p


# ── shape predicate (SSOT) ───────────────────────────────────────────────────

@pytest.mark.parametrize("code", [
    "AAPL", "MSFT", "BRK-B",       # 보통주·클래스주(단일 대시)
    "CON", "PRN",                  # Windows 예약명이지만 실재 티커 — shape는 valid(파일명 안전화는 sanitize 소관)
    "BRK-A-PR", "C-PR-J",          # F2: 다중 대시(nasdaq_trader가 다중 점→다중 대시로 낼 수 있음)
    "8", "0700",                   # 숫자형(HK/JP류) — shape 허용(오분류 안전쪽, 실제 US엔 미유입)
])
def test_predicate_accepts_valid_shapes(code):
    assert df.is_valid_overseas_symbol(code), code


@pytest.mark.parametrize("code", [
    "Apple Inc",                   # 공백(회사명)
    "AACT_U", "AAC_WS", "CON_", "PRN_",   # 언더스코어(유닛/워런트/예약명 sanitize stem)
    "금", "삼성전자",               # 한글 표시명(str.isalpha()가 True로 오분류하던 부류)
    "JPM/D", "RAC/UN",             # 슬래시(우선주/유닛)
    "brk-b",                       # 소문자
    "", "  ",                      # 빈/공백
    "-AAPL", "AAPL-",              # 대시 경계(세그먼트 없음)
])
def test_predicate_rejects_malformed(code):
    assert not df.is_valid_overseas_symbol(code), code


def test_save_drops_malformed(_tmp_path):
    df.save_managed_overseas([
        {"code": "AAPL", "name": "Apple"},
        {"code": "JPM/D", "name": "JPM Pref D"},      # 우선주(슬래시) — 제외
        {"code": "RAC/UN", "name": "Unit"},           # 유닛(슬래시) — 제외
        {"code": "AACT_U", "name": "SPAC Unit"},      # 유닛(언더스코어 stem) — 제외
        {"code": "Apple Inc", "name": "회사명"},       # 회사명(공백) — 제외
        {"code": "금", "name": "표시명"},              # 한글 표시명 — 제외
        {"code": "CON_", "name": "예약명 stem"},       # sanitize stem — 제외
        {"code": "BRK-B", "name": "Berkshire B"},     # 정당 클래스주(대시) — 유지
        {"code": "CON", "name": "Concentra"},         # 실재 티커(예약명이나 shape valid) — 유지
        {"code": "", "name": "빈코드"},                # 빈 — 제외
    ])
    codes = {s["code"] for s in df.load_managed_overseas()}
    assert codes == {"AAPL", "BRK-B", "CON"}, f"malformed 제외·실티커 유지돼야: {codes}"


def test_existing_malformed_self_cleans_on_next_save(_tmp_path):
    """기존 파일에 malformed가 있어도 다음 저장(시드 cron) 때 정리된다(load+union→save overwrite)."""
    _tmp_path.write_text(json.dumps([
        {"code": "JPM/D", "name": "old pref"},
        {"code": "AACT_U", "name": "old unit"},
        {"code": "금", "name": "old 표시명"},
        {"code": "AAPL", "name": "Apple"},
    ], ensure_ascii=False), encoding="utf-8")
    # 시드 패턴: 기존 ∪ 신규를 다시 save
    df.save_managed_overseas(df.load_managed_overseas() + [{"code": "MSFT", "name": "MS"}])
    codes = {s["code"] for s in df.load_managed_overseas()}
    assert codes == {"AAPL", "MSFT"}, f"기존 malformed 정리되고 신규 추가돼야: {codes}"


def test_dedupe_still_works(_tmp_path):
    df.save_managed_overseas([
        {"code": "AAPL", "name": "Apple"},
        {"code": "AAPL", "name": "dup"},
    ])
    assert len(df.load_managed_overseas()) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
