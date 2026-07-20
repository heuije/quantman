"""LsFuturesBroker 응답 정규화 전수 — account 2-TR·order normalize·t0434 status·pending 필터.

fixture 출처: t0434 status 어휘와 CFOAQ00600 체결내역은 **실측**(2026-07-20 모의 캡처
`퀀트/measure/out/2026-07-20/raw.jsonl`). 나머지(account·quote 계열)는 아직 research 기반.
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_futures_broker as lfb


def _broker():
    return object.__new__(lfb.LsFuturesBroker)


def test_account_snapshot_merges_two_trs(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {
        "CFOAQ50600OutBlock2": {"EvalDpsamtTotamt": "50000000", "MnyOrdAbleAmt": "30000000",
                                "CsgnMgnTotamt": "8000000", "FutsEvalPnlAmt": "120000"}}, raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {
        "t0441OutBlock1": [{"expcode": "101V6000", "medosu": "매수", "jqty": "2",
                            "pamt": "342.25", "price": "343.10", "dtsunik1": "120000"}]}, raising=False)
    snap = b.account_snapshot()
    assert snap["account"]["equity"] == 50000000
    assert snap["account"]["order_cash"] == 30000000
    pos = snap["positions"][0]
    assert pos["symbol"] == "101V6000" and pos["side"] == "long" and pos["qty"] == 2


def test_account_snapshot_short_position(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {"CFOAQ50600OutBlock2": {"EvalDpsamtTotamt": "1", "MnyOrdAbleAmt": "1"}}, raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": [
        {"expcode": "101V6000", "medosu": "매도", "jqty": "1", "pamt": "340.0", "price": "339.0"}]}, raising=False)
    assert b.account_snapshot()["positions"][0]["side"] == "short"


def test_account_snapshot_raises_on_partial_failure(monkeypatch):
    """2-TR 중 하나 실패 → raise(라우터가 fetch_failed 표식; 0 위장 금지)."""
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: (_ for _ in ()).throw(RuntimeError("5xx")), raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": []}, raising=False)
    with pytest.raises(Exception):
        b.account_snapshot()


def test_account_snapshot_mock_equity_reconstructs_from_used_margin(monkeypatch):
    """모의(CFOAQ50600 평가 미제공): equity = 가용(OrdAbleAmt) + 잠긴증거금(UsePreargMgn).

    종전엔 equity를 가용증거금만으로 근사 → 포지션이 열려 증거금이 잠기면 가용액↓ → equity 가짜 하락
    (킬스위치 오발동·−98% 부류). 진짜 예탁자산 = 가용 + 잠긴 — 둘 다 CFOAQ10100에 있어 추정 없이 복원
    (2026-06-30 실전 프로브 발견: OrdAbleAmt 236M + UsePreargMgn 264M = EvalDpsamtTotamt 500M).
    사이징(order_cash)은 가용 그대로 — equity(킬스위치)만 복원."""
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {"CFOAQ50600OutBlock2": {}}, raising=False)  # 평가 미제공
    monkeypatch.setattr(b, "_margin_amounts_krw", lambda: (236_254_700, 263_745_300), raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": []}, raising=False)
    acct = b.account_snapshot()["account"]
    assert acct["order_cash"] == 236_254_700                  # 사이징 = 가용
    assert acct["equity"] == 500_000_000                      # 킬스위치 = 가용+잠긴 (가짜하락 없음)


def test_price_and_open(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {
        "t2111OutBlock": {"price": "343.10", "open": "342.00", "jnilclose": "341.50"}}, raising=False)
    assert b.price("101V6000") == 343.10
    assert b.today_open("101V6000") == 342.00


def test_today_open_zero_fallback(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {"t2111OutBlock": {"open": ""}}, raising=False)
    assert b.today_open("101V6000") == 0.0


def test_buy_market_normalizes_ordno(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00100InBlock1"]
        return {"rsp_cd": "00040", "rsp_msg": "정상", "CFOAT00100OutBlock2": {"OrdNo": "777"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    r = b.buy("101V6000", 1)
    assert r == {"success": True, "order_no": "777", "message": "정상", "msg_cd": "00040"}
    assert captured["body"]["BnsTpCode"] == "2"
    assert captured["body"]["FnoOrdprcPtnCode"] == "03"


def test_sell_uses_bnstp_1(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00100InBlock1"]
        return {"CFOAT00100OutBlock2": {"OrdNo": "8"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.sell("101V6000", 1)
    assert captured["body"]["BnsTpCode"] == "1"


def test_buy_limit_sends_double_price(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00100InBlock1"]
        return {"CFOAT00100OutBlock2": {"OrdNo": "9"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.buy_limit("101V6000", 1, 342.25)
    assert captured["body"]["FnoOrdprcPtnCode"] == "00"
    assert captured["body"]["FnoOrdPrc"] == 342.25   # double 포인트 — int 절삭 금지


def test_order_reject_no_ordno(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"rsp_cd": "99", "rsp_msg": "증거금부족"}, raising=False)
    r = b.buy("101V6000", 1)
    assert r["success"] is False and r["order_no"] == ""


def test_resv_not_implemented():
    import pytest
    b = _broker()
    with pytest.raises(NotImplementedError):
        b.buy_resv_limit("101V6000", 1, 342.0)
    with pytest.raises(NotImplementedError):
        b.sell_resv_limit("101V6000", 1, 342.0)


def test_order_status_filled_from_t0434(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "777", "orgordno": "0", "qty": "1", "cheqty": "1", "ordrem": "0",
         "cheprice": "343.10", "status": "완료"}]}, raising=False)
    st = b.order_status("777", symbol="101V6000")
    assert st["status"] == "filled" and st["filled_qty"] == 1 and st["fill_price"] == 343.10


def test_order_status_cancelled(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "5", "orgordno": "0", "qty": "1", "cheqty": "0", "ordrem": "0", "status": "취소확인"}]}, raising=False)
    assert b.order_status("5", symbol="101V6000")["status"] == "cancelled"


def test_order_status_unknown_when_absent(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": []}, raising=False)
    assert b.order_status("999", symbol="101V6000")["status"] == "unknown"


@pytest.mark.parametrize("status,cheqty,ordrem,expect", [
    ("접수", "0", "3", "submitted"),      # 살아있음
    ("완료", "3", "0", "filled"),         # 체결 종결
    ("완료", "1", "2", "partial"),        # 부분체결·잔량 있음
    ("완료", "0", "0", "cancelled"),      # 🔴 취소 종결 — 종전엔 submitted로 오판
    ("취소확인", "0", "0", "cancelled"),   # 취소 확인행
])
def test_order_status_maps_measured_t0434_vocabulary(monkeypatch, status, cheqty,
                                                     ordrem, expect):
    """실측 2026-07-20 t0434 어휘 `{접수, 취소확인, 완료}` 전 조합 매핑 고정.

    핵심은 4번째 행 — **`완료`가 체결과 취소 양쪽에 쓰인다**. 종전 판정은
    `"취소" in status` → `cheqty>0` 순으로만 봐서 `완료/cheqty=0`이 마지막
    `submitted`로 떨어졌고, 취소된 주문이 당일 내내 pending에 남아 "미체결 0"
    게이트(무인 자동 업데이트 조건)를 막았다. 잔량 0·체결 0이면 어떤 어휘가
    오든 종결이라는 계약을 여기서 잠근다.
    """
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "41", "orgordno": "0", "qty": "3", "cheqty": cheqty,
         "ordrem": ordrem, "cheprice": "1098.55", "status": status}]}, raising=False)
    assert b.order_status("41", symbol="101V6000")["status"] == expect


def test_pending_excludes_modify_cancel_rows(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "10", "orgordno": "0", "qty": "2", "cheqty": "0", "ordrem": "2", "medosu": "매수", "price": "342.0", "expcode": "101V6000"},
        {"ordno": "11", "orgordno": "10", "qty": "2", "cheqty": "0", "ordrem": "2", "medosu": "매수", "price": "342.0", "expcode": "101V6000"}]}, raising=False)
    pend = b.pending_orders()
    assert len(pend) == 1 and pend[0]["order_no"] == "10" and pend[0]["side"] == "buy"


def test_cancel_normalizes(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda p, t, body, **k: {"CFOAT00300OutBlock2": {"OrdNo": "99"}}, raising=False)
    r = b.cancel("10", "101V6000", 2)
    assert r["success"] is True and "order_no" not in r


# --- orderable_qty (CFOAQ10100 NewOrdAbleQty) — 모델 A 라이브 사이징 브로커 기준값 ---

_OQ_OK = {"CFOAQ10100OutBlock2": {"NewOrdAbleQty": "120", "LqdtOrdAbleQty": "0", "OrdAbleAmt": "500000000"}}


def test_orderable_qty_buy_parses_and_sends_inblock(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["path"] = path
        captured["tr"] = tr
        captured["ib"] = body["CFOAQ10100InBlock1"]
        return _OQ_OK
    monkeypatch.setattr(b, "_post", _post, raising=False)
    qty = b.orderable_qty("101V6000", "buy", 342.25)
    assert qty == 120
    assert captured["tr"] == "CFOAQ10100"
    assert captured["path"] == "/futureoption/accno"
    ib = captured["ib"]
    assert ib["BnsTpCode"] == "2"                  # 매수
    assert ib["FnoIsuNo"] == "101V6000"
    assert ib["FnoOrdPrc"] == 342.25               # double 포인트 — int 절삭 금지
    assert ib["FnoOrdprcPtnCode"] == "00"          # 지정가 고정


def test_orderable_qty_sell_uses_bnstp_1(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["ib"] = body["CFOAQ10100InBlock1"]
        return _OQ_OK
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.orderable_qty("101V6000", "sell", 342.0)
    assert captured["ib"]["BnsTpCode"] == "1"       # 매도


def test_orderable_qty_raises_when_outblock_absent(monkeypatch):
    """조회 실패(OutBlock2 부재) → raise. 호출자(Trader)가 catch해 카탈로그로 강등.
    ⚠ _orderable_amt_krw(soft 0폴백)와 달리 0 위장 금지 — 실패와 0계약 구분."""
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"rsp_cd": "99", "rsp_msg": "조회실패"}, raising=False)
    with pytest.raises(RuntimeError):
        b.orderable_qty("101V6000", "buy", 342.0)


def test_orderable_qty_zero_contracts_returns_zero(monkeypatch):
    """정상 응답의 NewOrdAbleQty=0(증거금 부족)은 0 반환 — raise 아님."""
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"CFOAQ10100OutBlock2": {"NewOrdAbleQty": "0"}}, raising=False)
    assert b.orderable_qty("101V6000", "buy", 342.0) == 0


def test_orderable_qty_bad_side_raises_value_error(monkeypatch):
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: _OQ_OK, raising=False)
    with pytest.raises(ValueError):
        b.orderable_qty("101V6000", "hold", 342.0)


# --- fills_on (CFOAQ00600 주문체결내역 기간조회) — 익일 회수 R2 -------------------
# fixture = 실측 2026-07-20 CFOAQ00600OutBlock3 형상 그대로(OrdNo·ExecQty int / 가격 str).


def _row(ordno, *, org=0, exec_qty=0, exec_prc="0.00", ctrct_time="",
         ord_tp="접수", ord_qty=1, ord_prc="1098.55"):
    """실측 행 1건. 체결행 = CtrctTime 있음 + ExecQty>0 + OrdTpNm 빈 문자열."""
    return {"OrdDt": "20260720", "OrdNo": ordno, "OrgOrdNo": org,
            "OrdTime": "083307085", "FnoIsuNo": "A0169000",
            "IsuNm": "코스피200 F 202609", "BnsTpNm": "매수", "MrcTpNm": "",
            "FnoOrdprcPtnCode": "00", "FnoOrdprcPtnNm": "지정가",
            "OrdPrc": ord_prc, "OrdQty": ord_qty, "OrdTpNm": ord_tp,
            "ExecTpNm": "환매", "ExecPrc": exec_prc, "ExecQty": exec_qty,
            "CtrctTime": ctrct_time, "CtrctNo": 0, "ExecNo": 0, "BnsplAmt": 0,
            "UnercQty": 0, "UserId": "tester", "CommdaCode": "40",
            "CommdaCodeNm": ""}


def _fills(monkeypatch, b, rows):
    monkeypatch.setattr(b, "_fills_raw",
                        lambda ymd: {"CFOAQ00600OutBlock3": rows}, raising=False)


def test_fills_on_sends_single_day_inblock(monkeypatch):
    """QrySrtDt=QryEndDt=제출일. AcntNo/InptPwd는 싣지 않는다(CFOAQ50600·CFOAQ10100 동일)."""
    b = _broker()
    captured = {}

    def _post(path, tr, body, **k):
        captured.update(path=path, tr=tr, ib=body["CFOAQ00600InBlock1"])
        return {"CFOAQ00600OutBlock3": []}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.fills_on("20260720", "코스피200선물")
    assert captured["tr"] == "CFOAQ00600"
    assert captured["path"] == "/futureoption/accno"
    ib = captured["ib"]
    assert ib["QrySrtDt"] == "20260720" and ib["QryEndDt"] == "20260720"
    assert ib["FnoClssCode"] == "00" and ib["StnlnSeqTp"] == "4"
    assert "AcntNo" not in ib and "InptPwd" not in ib


def test_fills_on_keeps_only_executed_rows(monkeypatch):
    """접수·확인 행(CtrctTime=""·ExecQty=0)은 제외 — 체결행만. int/str 타입 정규화."""
    b = _broker()
    _fills(monkeypatch, b, [
        _row(16),                                                   # 접수(무체결)
        _row(17, ord_tp="확인"),                                    # 확인(무체결)
        _row(18, exec_qty=438, exec_prc="1352.60",
             ctrct_time="090012345", ord_tp="", ord_qty=438),       # 체결
    ])
    out = b.fills_on("20260720")
    assert out == [{"odno": "18", "filled_qty": 438, "fill_price": 1352.60}]
    assert isinstance(out[0]["filled_qty"], int)                    # int 정규화
    assert isinstance(out[0]["fill_price"], float)                  # str "1352.60" → float


def test_fills_on_sums_modify_chain_to_root(monkeypatch):
    """정정 자식(41→43)의 체결을 원주문 41의 체결로 합산 — 원번호 조회 오종결 차단."""
    b = _broker()
    _fills(monkeypatch, b, [
        _row(41, ord_qty=5),                                        # 원주문 접수
        _row(43, org=41, exec_qty=5, exec_prc="1100.00",
             ctrct_time="101500000", ord_tp="", ord_qty=5),         # 정정 후 체결
    ])
    assert b.fills_on("20260720") == [
        {"odno": "41", "filled_qty": 5, "fill_price": 1100.0}]


def test_fills_on_resolves_multi_level_chain(monkeypatch):
    """다단 정정(16→59→60)도 뿌리 16으로 전이 해소."""
    b = _broker()
    _fills(monkeypatch, b, [
        _row(16, ord_qty=3),
        _row(59, org=16, ord_tp="확인", ord_qty=3),
        _row(60, org=59, exec_qty=3, exec_prc="1102.50",
             ctrct_time="112233444", ord_tp="", ord_qty=3),
    ])
    assert b.fills_on("20260720") == [
        {"odno": "16", "filled_qty": 3, "fill_price": 1102.5}]


def test_fills_on_chain_uses_qty_weighted_average(monkeypatch):
    """원주문 부분체결 + 정정 자식 체결 → 체결수량 가중평균가."""
    b = _broker()
    _fills(monkeypatch, b, [
        _row(41, exec_qty=2, exec_prc="1100.00", ctrct_time="100000000",
             ord_tp="", ord_qty=5),
        _row(43, org=41, exec_qty=3, exec_prc="1102.50",
             ctrct_time="101500000", ord_tp="", ord_qty=3),
    ])
    # (2×1100.00 + 3×1102.50) / 5 = 1101.50
    assert b.fills_on("20260720") == [
        {"odno": "41", "filled_qty": 5, "fill_price": 1101.5}]


def test_fills_on_cancel_child_does_not_change_total(monkeypatch):
    """취소도 자식 행을 만들지만 ExecQty=0이라 합계에 영향 없음."""
    b = _broker()
    _fills(monkeypatch, b, [
        _row(29, exec_qty=1, exec_prc="1098.55", ctrct_time="090000000",
             ord_tp="", ord_qty=2),
        _row(54, org=29, ord_tp="확인", ord_qty=1),                 # 취소 자식(무체결)
    ])
    assert b.fills_on("20260720") == [
        {"odno": "29", "filled_qty": 1, "fill_price": 1098.55}]


def test_fills_on_empty_response_returns_empty(monkeypatch):
    """비거래일 등 rsp_cd="00707"(내역 없음)은 정상 응답 — OutBlock3 부재 → []."""
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {
        "rsp_cd": "00707", "rsp_msg": "조회할 내역이 없습니다."}, raising=False)
    assert b.fills_on("20260719", "코스피200선물") == []


def test_root_ordno_breaks_self_reference_and_cycle():
    """자기참조·순환 응답에도 무한루프 금지(브로커 응답 무신뢰)."""
    assert lfb._root_ordno("7", {"7": "7"}) == "7"
    assert lfb._root_ordno("1", {"1": "2", "2": "1"}) in ("1", "2")
    assert lfb._root_ordno("3", {}) == "3"
