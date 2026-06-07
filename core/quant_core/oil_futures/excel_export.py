"""WTI 선물 분석 → 라이브 수식 엑셀(.xlsx) 생성.

목적: 대시보드의 백테스트 로직을 *엑셀 함수로 연결된* 파일로 내보내,
사용자가 엑셀에서 임계값·보유기간·롤비용을 바꾸면 즉시 재계산되게 한다
(원본 'Crude Oil WTI Futures Analysis' 엑셀의 업그레이드판).

설계:
- 입력칸(B2~B5): 방향/임계값/보유기간/롤비용 → 셀 참조로 전 수식에 반영.
- 데이터+수식 시트: 신호(장중 고저점 첫 터치)·진입(다음날 시가)·청산(보유기간
  후 종가)·롤횟수(보유 중 통과 월수 ≈ 만기 횟수)·수익률·PnL을 전부 =수식으로.
- 요약: COUNTIF/AVERAGE/SUM.

롤오버: 앱과 동일하게 '만기일' 시트에 실제 WTI 월물 만기일을 싣고, K열에서
COUNTIFS로 진입~청산 사이 만기 횟수를 정확히 카운트(entry<만기<=exit). 롤비용
입력칸(B5)을 바꾸면 수익률·PnL이 즉시 재계산된다(양수=콘탱고 비용, 음수=
backwardation 이익).

한계(엑셀 단순화): 수수료·슬리피지·MAE/MFE·walk-forward는 제외(gross 기준).
앱은 롤비용≠0일 때 롤당 소액 거래마찰($ commission+slippage)도 부과하나
엑셀은 term-structure 성분(롤횟수×롤비용)만 반영.
"""
from __future__ import annotations

import io

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side as XlSide
from openpyxl.utils import get_column_letter

from .backtest import wti_expiry_dates


# 디자인 토큰 (대시보드 팔레트와 통일)
_ACCENT = "D97757"
_HEAD_BG = "F7ECE5"
_INPUT_BG = "FFF4CC"
_BORDER = "E8E3DB"


def _thin_border() -> Border:
    s = XlSide(style="thin", color=_BORDER)
    return Border(left=s, right=s, top=s, bottom=s)


def build_oil_excel(
    df: pd.DataFrame,
    side: str = "short",
    threshold: float = 100.0,
    horizon_days: int = 120,
    roll_cost_pct: float = 0.0,
    commission_pct: float = 0.0,
    slippage_pct: float = 0.0,
) -> bytes:
    """WTI OHLC DataFrame → 라이브 수식 .xlsx 바이트.

    df: load_wti() 출력 (date ASC, open/high/low/close/volume).
    side/threshold/... : 엑셀 입력칸 초기값 (열어서 바꾸면 재계산).
    """
    df = df.sort_values("date").reset_index(drop=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "백테스트"

    bold = Font(bold=True)
    title_font = Font(bold=True, size=14, color="20201D")
    accent_font = Font(bold=True, color=_ACCENT)
    input_fill = PatternFill("solid", fgColor=_INPUT_BG)
    head_fill = PatternFill("solid", fgColor=_HEAD_BG)
    border = _thin_border()
    center = Alignment(horizontal="center")

    # ── 제목 ─────────────────────────────────────────────────────────
    ws["A1"] = "WTI Crude Oil Futures — 라이브 백테스트"
    ws["A1"].font = title_font

    # ── 입력칸 (B2~B7) ───────────────────────────────────────────────
    inputs = [
        ("방향 (short/long)", side),
        ("임계값 ($)", threshold),
        ("보유기간 (영업일)", horizon_days),
        ("롤비용 (%/롤, 예 0.5)", roll_cost_pct * 100),
        ("슬리피지 (%/체결, 예 0.05)", slippage_pct * 100),
        ("수수료 (%/체결, 예 0.03)", commission_pct * 100),
    ]
    for i, (label, val) in enumerate(inputs):
        r = 2 + i
        ws[f"A{r}"] = label
        ws[f"A{r}"].font = bold
        c = ws[f"B{r}"]
        c.value = val
        c.fill = input_fill
        c.border = border
        c.alignment = center
    ws["C2"] = "← 노란 칸을 바꾸면 전체가 재계산됩니다"
    ws["C2"].font = Font(italic=True, color="6F6A62")

    # ── 요약 (D2~E6) ─────────────────────────────────────────────────
    # 데이터는 9행부터 시작 → 마지막 데이터 행 = 8 + len(df)
    n = len(df)
    last = 8 + n
    summary = [
        ("신호 횟수", f'=COUNTIF(F9:F{last},1)'),
        ("거래 성립", f'=COUNT(I9:I{last})'),
        ("승률", f'=IFERROR(COUNTIF(I9:I{last},">0")/COUNT(I9:I{last}),0)'),
        ("평균 수익률", f'=IFERROR(AVERAGE(I9:I{last}),0)'),
        ("누적 PnL ($, 1계약)", f'=SUM(J9:J{last})'),
    ]
    for i, (label, formula) in enumerate(summary):
        r = 2 + i
        ws[f"D{r}"] = label
        ws[f"D{r}"].font = accent_font
        cell = ws[f"E{r}"]
        cell.value = formula
        cell.font = bold
        cell.border = border
    # 퍼센트 표시
    ws["E4"].number_format = "0.0%"
    ws["E5"].number_format = "+0.00%;-0.00%"
    ws["E6"].number_format = "#,##0"

    # ── 데이터 + 수식 헤더 (8행) ─────────────────────────────────────
    headers = ["날짜", "시가", "고가", "저가", "종가",
               "신호", "진입가", "청산가", "수익률", "PnL($)", "롤횟수",
               "유효진입가", "유효청산가"]
    for j, h in enumerate(headers):
        c = ws.cell(row=8, column=1 + j, value=h)
        c.font = bold
        c.fill = head_fill
        c.border = border
        c.alignment = center

    # ── 데이터 + 행별 수식 (9행~) ────────────────────────────────────
    for idx in range(n):
        r = 9 + idx
        row = df.iloc[idx]
        ws.cell(row=r, column=1, value=pd.Timestamp(row["date"]).to_pydatetime())
        ws.cell(row=r, column=2, value=float(row["open"]))
        ws.cell(row=r, column=3, value=float(row["high"]))
        ws.cell(row=r, column=4, value=float(row["low"]))
        ws.cell(row=r, column=5, value=float(row["close"]))

        # F 신호: short=고가 위로 첫터치, long=저가 아래로 첫터치 (전일 비교)
        f = (
            f'=IF($B$2="short",'
            f'IF(AND(C{r}>=$B$3,C{r-1}<$B$3),1,""),'
            f'IF(AND(D{r}<=$B$3,D{r-1}>$B$3),1,""))'
        )
        ws.cell(row=r, column=6, value=f)
        # G 진입가 = 다음날 시가
        ws.cell(row=r, column=7,
                value=f'=IF(F{r}=1,IFERROR(OFFSET(B{r},1,0),""),"")')
        # H 청산가 = 진입 후 보유기간 종가 (= 신호행 기준 1+horizon 아래)
        ws.cell(row=r, column=8,
                value=f'=IF(F{r}=1,IFERROR(OFFSET(E{r},1+$B$4,0),""),"")')
        # K 롤횟수 = 진입~청산 사이 실제 WTI 만기일 수 (앱과 동일: entry<만기<=exit)
        # 만기일!A:A 에 만기일 리스트, COUNTIFS로 정확히 카운트
        ws.cell(row=r, column=11, value=(
            f'=IF(F{r}=1,IFERROR(COUNTIFS('
            f'만기일!$A:$A,">"&OFFSET(A{r},1,0),'
            f'만기일!$A:$A,"<="&OFFSET(A{r},1+$B$4,0)'
            f'),""),"")'
        ))
        # L 유효진입가 = 슬리피지 반영 (short 진입가↓, long 진입가↑)
        ws.cell(row=r, column=12, value=(
            f'=IF(G{r}="","",IF($B$2="short",G{r}*(1-$B$6/100),G{r}*(1+$B$6/100)))'
        ))
        # M 유효청산가 = 슬리피지 반영 (short 청산가↑, long 청산가↓)
        ws.cell(row=r, column=13, value=(
            f'=IF(H{r}="","",IF($B$2="short",H{r}*(1+$B$6/100),H{r}*(1-$B$6/100)))'
        ))
        # J PnL($) = [부호·(유효청산-유효진입) - 수수료(양레그)]·1000 - 롤비용
        #   수수료 = $B$7/100 × (유효진입+유효청산), 롤 = K×$B$5/100×진입notional
        ws.cell(row=r, column=10, value=(
            f'=IF(OR(L{r}="",M{r}=""),"",'
            f'(IF($B$2="short",L{r}-M{r},M{r}-L{r})-$B$7/100*(L{r}+M{r}))*1000'
            f'-K{r}*$B$5/100*G{r}*1000)'
        ))
        # I 수익률 = 순손익 / 진입 거래대금 (비용 반영된 net 기준)
        ws.cell(row=r, column=9,
                value=f'=IF(J{r}="","",J{r}/(G{r}*1000))')

    # 수익률·PnL·유효가 포맷
    for idx in range(n):
        r = 9 + idx
        ws.cell(row=r, column=9).number_format = "+0.00%;-0.00%"
        ws.cell(row=r, column=10).number_format = "#,##0"
        ws.cell(row=r, column=12).number_format = "0.00"
        ws.cell(row=r, column=13).number_format = "0.00"
        ws.cell(row=r, column=1).number_format = "yyyy-mm-dd"

    # ── 열 너비 ──────────────────────────────────────────────────────
    widths = [12, 9, 9, 9, 9, 7, 10, 10, 10, 12, 8, 11, 11]
    for j, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(1 + j)].width = w
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 14

    # 헤더 행 고정 (스크롤해도 보이게)
    ws.freeze_panes = "A9"

    # ── 만기일 시트 (롤횟수 COUNTIFS 참조용) ─────────────────────────
    exp_ws = wb.create_sheet("만기일")
    exp_ws["A1"] = "WTI 월물 만기일"
    exp_ws["A1"].font = bold
    exp_ws["B1"] = "(CME 규칙: 인도월 전월 25일의 3영업일 전. 백테스트!K열이 COUNTIFS로 참조)"
    exp_ws["B1"].font = Font(italic=True, color="6F6A62")
    expiries = wti_expiry_dates(
        pd.Timestamp(df["date"].iloc[0]), pd.Timestamp(df["date"].iloc[-1])
    )
    for i, e in enumerate(expiries):
        cell = exp_ws.cell(row=2 + i, column=1, value=pd.Timestamp(e).to_pydatetime())
        cell.number_format = "yyyy-mm-dd"
    exp_ws.column_dimensions["A"].width = 14
    exp_ws.column_dimensions["B"].width = 60

    # ── 로직 설명 시트 ───────────────────────────────────────────────
    doc = wb.create_sheet("로직설명")
    notes = [
        ["WTI 선물 백테스트 — 엑셀 로직 설명", ""],
        ["", ""],
        ["입력칸", "백테스트 시트 B2~B7의 노란 칸을 바꾸면 전체 재계산"],
        ["방향(B2)", "short=고가가 임계값 위로 첫 터치 시 매도, long=저가가 아래로 첫 터치 시 매수"],
        ["임계값(B3)", "신호 발생 가격선 ($)"],
        ["보유기간(B4)", "진입 후 청산까지 영업일 수"],
        ["롤비용(B5)", "만기 롤오버 1회당 % (양수=콘탱고 비용/음수=backwardation 이익)"],
        ["슬리피지(B6)", "체결 1회당 불리한 체결 비율 % (진입·청산 각각). short 진입가↓·청산가↑"],
        ["수수료(B7)", "체결 거래대금 대비 % (한국투자 등 우대율 계좌별 상이 → 직접 입력)"],
        ["신호(F열)", "장중 고가/저가 기준 전일 대비 첫 돌파 (히스테리시스 — 연속 돌파는 1회만)"],
        ["진입가(G열)", "신호 다음 영업일 시가 (look-ahead 제거)"],
        ["청산가(H열)", "진입 후 '보유기간' 영업일 후 종가"],
        ["유효진입가(L열)", "진입가에 슬리피지 반영 (=실제 체결가)"],
        ["유효청산가(M열)", "청산가에 슬리피지 반영"],
        ["롤횟수(K열)", "'만기일' 시트의 실제 WTI 만기일을 COUNTIFS로 카운트 (진입<만기≤청산) — 앱과 동일"],
        ["PnL(J열)", "[부호×(유효청산−유효진입) − 수수료×(유효진입+유효청산)]×1000 − 롤횟수×롤비용×진입대금"],
        ["수익률(I열)", "순손익(J) ÷ 진입 거래대금. 슬리피지·수수료·롤비용 모두 반영된 net"],
        ["만기일 시트", "롤횟수 계산의 기준 만기일 목록 (CME 규칙 기반 자동 생성)"],
        ["", ""],
        ["한계", "MAE/MFE·walk-forward는 미반영. 앱은 롤비용≠0 시 롤당 소액 거래마찰도 부과(엑셀은 term-structure 성분만)."],
    ]
    for i, (a, b) in enumerate(notes):
        doc.cell(row=1 + i, column=1, value=a).font = bold if i == 0 else Font()
        doc.cell(row=1 + i, column=2, value=b)
    doc.column_dimensions["A"].width = 16
    doc.column_dimensions["B"].width = 70

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
