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
