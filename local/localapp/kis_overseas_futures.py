"""KIS 해외선물옵션 *거래* 순수함수 — 주문바디(OTFM3001U)·잔고파싱(OTFM1412R)·시세 스케일.

해외는 모의투자 미지원(실전 전용). 국내선물(kis_futures_broker)과 API 전면 상이:
심볼=OVRS_FUTR_FX_PDNO(CME globex, 예 GCZ25), 잔고 output=행형 array, 통화 USD.
side 정규형 long(매수 02)/short(매도 01). 네트워크 없는 순수함수 — 단위검증 대상.

⚠ 시세(HHDFC55010000)는 raw 정수를 ffcode.mst의 sCalcDesz(계산소수점)로 스케일해야 정확
   (예: GC sCalcDesz -1 → raw 19225 = 1922.5). scale_overseas_price가 그 변환.
6종 CME 루트(분석 dashboard 정합): GC(금)·CL(원유)·NQ(나스닥)·NG(천연가스)·SI(은)·BTC(비트코인).
"""
from __future__ import annotations

_SIDE_CD = {"buy": "02", "sell": "01"}          # 주문 SLL_BUY_DVSN_CD
_POS_SIDE = {"02": "long", "01": "short"}       # 잔고 sll_buy_dvsn_cd → 정규 side


def build_overseas_order_body(*, cano: str, acnt_prdt_cd: str, symbol: str,
                              side: str, qty: int, price, order_type: str) -> dict:
    """OTFM3001U 주문 바디. side: buy|sell. order_type: limit|market.

    지정가: PRIC_DVSN_CD=1·FM_LIMIT_ORD_PRIC=price·CCLD_CNDT_CD=6(EOD).
    시장가: PRIC_DVSN_CD=2·FM_LIMIT_ORD_PRIC=""·CCLD_CNDT_CD=2.
    """
    if side not in _SIDE_CD:
        raise ValueError(f"side는 buy|sell: {side}")
    if order_type not in ("limit", "market"):
        raise ValueError(f"order_type는 limit|market: {order_type}")
    is_limit = order_type == "limit"
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,                       # 해외선물 "08"
        "OVRS_FUTR_FX_PDNO": symbol,                        # CME globex 코드
        "SLL_BUY_DVSN_CD": _SIDE_CD[side],
        "FM_LQD_USTL_CCLD_DT": "",
        "FM_LQD_USTL_CCNO": "",
        "PRIC_DVSN_CD": "1" if is_limit else "2",           # 1 지정 / 2 시장
        "FM_LIMIT_ORD_PRIC": str(price) if is_limit else "",
        "FM_STOP_ORD_PRIC": "",
        "FM_ORD_QTY": str(int(qty)),
        "FM_LQD_LMT_ORD_PRIC": "",
        "FM_LQD_STOP_ORD_PRIC": "",
        "CCLD_CNDT_CD": "6" if is_limit else "2",           # 6 지정가EOD / 2 시장가
        "CPLX_ORD_DVSN_CD": "0",
        "ECIS_RSVN_ORD_YN": "N",
        "FM_HDGE_ORD_SCRN_YN": "N",
    }


def build_overseas_cancel_body(*, cano: str, acnt_prdt_cd: str,
                               orgn_ord_dt: str, orgn_odno: str) -> dict:
    """OTFM3003U 주문취소 바디 (order-rvsecncl, tr_id OTFM3003U).

    취소는 *원주문일자*(ORGN_ORD_DT=현지거래일, 원주문 응답의 ORD_DT)와 원주문번호
    (ORGN_ODNO, "0" 포함 8자리)가 필수다 — 국내선물 취소(order_no만)와 다른 점.
    FM_HDGE_ORD_SCRN_YN="N"(필수), FM_MKPR_CVSN_YN="N"(취소 후 시장가 재주문 안 함). 순수함수.
    """
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "ORGN_ORD_DT": str(orgn_ord_dt),
        "ORGN_ODNO": str(orgn_odno),
        "FM_HDGE_ORD_SCRN_YN": "N",
        "FM_MKPR_CVSN_YN": "N",
    }


def parse_overseas_ccld_order_status(resp: dict, order_no) -> dict:
    """OTFM3116R(inquire-ccld 당일주문내역) output에서 order_no 행 →
    {order_no,status,filled_qty,remain_qty,fill_price} (국내 parse_ccnl_order_status와 동일 shape).

    canonical odno 비교(lstrip "0"). status: filled(잔량0·체결>0)/partial(체결>0)/submitted(체결0)/
    unknown(행 없음). fm_ccld_pric=실제 체결가(FM=정형가, raw 스케일 불요).
    """
    rows = resp.get("output")
    if not isinstance(rows, list):
        rows = []
    target = str(order_no).lstrip("0")
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("odno", "")).lstrip("0") == target:
            def _i(k):
                try:
                    return int(float(r.get(k, 0) or 0))
                except (ValueError, TypeError):
                    return 0
            filled, remain = _i("fm_ccld_qty"), _i("fm_ord_rmn_qty")
            if remain == 0 and filled > 0:
                status = "filled"
            elif filled > 0:
                status = "partial"
            else:
                status = "submitted"
            return {"order_no": str(r.get("odno", "")), "status": status,
                    "filled_qty": filled, "remain_qty": remain,
                    "fill_price": float(r.get("fm_ccld_pric", 0) or 0)}
    return {"order_no": str(order_no), "status": "unknown",
            "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}


def parse_overseas_orderable_qty(resp: dict) -> int:
    """OTFM3304R(inquire-psamount 주문가능조회) output → 신규주문가능 계약수(fm_new_ord_psbl_qty).

    KIS가 가격·증거금·예수금을 반영해 산출한 *주문가능 계약수*. 해외선물 사이징의 상한 클램프로
    쓴다(국내주식 buying_power_usd max_qty와 동일 역할). 파싱 불가/없음 → 0. 순수함수.
    """
    out = resp.get("output")
    if isinstance(out, list):
        out = out[0] if out else {}
    if not isinstance(out, dict):
        return 0
    try:
        return int(float(out.get("fm_new_ord_psbl_qty", 0) or 0))
    except (ValueError, TypeError):
        return 0


def parse_overseas_balance(resp: dict) -> dict:
    """OTFM1412R(미결제내역=잔고) → {positions:[{symbol,side,qty,avg_price,eval_price,eval_pnl,currency}]}.

    output = 행형 array. 0수량 제외. side: 02 매수→long / 01 매도→short.
    """
    rows = resp.get("output")
    if not isinstance(rows, list):
        rows = []
    positions = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            qty = int(float(r.get("fm_ustl_qty", 0) or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty == 0:
            continue
        positions.append({
            "symbol": str(r.get("ovrs_futr_fx_pdno", "") or "").strip(),
            "side": _POS_SIDE.get(str(r.get("sll_buy_dvsn_cd", "")).strip(), ""),
            "qty": qty,
            "avg_price": float(r.get("fm_ccld_avg_pric", 0) or 0),
            "eval_price": float(r.get("fm_now_pric", 0) or 0),
            "eval_pnl": float(r.get("fm_evlu_pfls_amt", 0) or 0),
            "currency": str(r.get("crcy_cd", "") or "").strip(),
        })
    return {"positions": positions}


def parse_overseas_deposit(resp: dict) -> dict:
    """OTFM1411R(예수금현황 inquire-deposit) output → {order_cash, equity, margin_total, eval_pnl}.

    CRCY_CD=TKR(TOT_KRW)로 요청해 KIS가 KRW 환산한 계좌 요약을 받는다(G1/G3 — 해외선물 주문
    사이징·kill-switch가 *해외선물 계좌* 기준이 되도록). 필드는 공식 스펙(해외선물옵션 주문_계좌.xlsx
    '예수금현황' OTFM1411R):
      · fm_ord_psbl_amt      주문가능금액 → order_cash(사이징 예산 base; trader가 KRW로 받아 event_buy_qty
                              가 USD 선물이면 fx 환산[US-F1]). 종전 미노출이라 국내선물/주식 현금으로 잘못
                              사이징하던 G1 결함을 닫는다.
      · fm_tot_asst_evlu_amt 총자산평가금액 → equity(통합 equity 합산; kill-switch가 해외선물 손익 인지[G3]).
      · fm_brkg_mgn_amt      위탁증거금  → margin_total(표시).
      · fm_fuop_evlu_pfls_amt 선물옵션평가손익 → eval_pnl(표시).
    output은 단일 object(국내 output2와 동형). ⚠ 모의 미지원 — 필드 *값* 라이브 대조는 첫 실거래(스펙 기반 구현)."""
    out = resp.get("output")
    if isinstance(out, list):
        out = out[0] if out else {}
    if not isinstance(out, dict):
        out = {}

    def _num(key: str) -> float:
        try:
            return float(out.get(key, 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    return {
        "order_cash": _num("fm_ord_psbl_amt"),        # 주문가능금액(KRW, TKR 요청 시)
        "equity": _num("fm_tot_asst_evlu_amt"),        # 총자산평가금액(KRW) — kill-switch
        "margin_total": _num("fm_brkg_mgn_amt"),       # 위탁증거금
        "eval_pnl": _num("fm_fuop_evlu_pfls_amt"),     # 선물옵션평가손익
    }


def scale_overseas_price(raw, scalc_desz: int) -> float:
    """해외 시세 raw 값을 sCalcDesz(계산소수점)로 스케일. raw×10^scalc_desz.

    예: GC sCalcDesz=-1 → "19225"×10^-1 = 1922.5. 빈값/이상치는 0.0.
    """
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        return float(s) * (10.0 ** scalc_desz)
    except (ValueError, TypeError):
        return 0.0
