"""WTI 선물 분석 → 라이브 수식 엑셀(.xlsx) 생성.

목적: 대시보드의 백테스트 로직을 *엑셀 함수로 연결된* 파일로 내보내,
사용자가 엑셀에서 임계값·보유기간·롤비용을 바꾸면 즉시 재계산되게 한다
(원본 'Crude Oil WTI Futures Analysis' 엑셀의 업그레이드판).

설계:
- 입력칸(B2~B5): 방향/임계값/보유기간/롤비용 → 셀 참조로 전 수식에 반영.
- 데이터+수식 시트: 신호(장중 고저점 첫 터치)·진입(다음날 시가)·청산(보유기간
  후 종가)·롤횟수(보유 중 통과 월수 ≈ 만기 횟수)·수익률·PnL을 전부 =수식으로.
- 요약: COUNTIF/AVERAGE/SUM.

한계(엑셀 단순화): 수수료·슬리피지·MAE/MFE·walk-forward는 제외(gross 기준).
롤횟수는 '캘린더 월 변화 수'로 근사(엑셀에서 투명하게 보이도록) — 앱의 정확한
만기일 카운트와 미세 차이 가능. 롤비용 0이면 영향 없음.
"""
from __future__ import annotations

import io

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side as XlSide
from openpyxl.utils import get_column_letter


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

    # ── 입력칸 (B2~B5) ───────────────────────────────────────────────
    inputs = [
        ("방향 (short/long)", side),
        ("임계값 ($)", threshold),
        ("보유기간 (영업일)", horizon_days),
        ("롤비용 (%/롤, 예 0.5)", roll_cost_pct * 100),
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
               "신호", "진입가", "청산가", "수익률", "PnL($)", "롤횟수"]
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
        # K 롤횟수 = 진입~청산 사이 캘린더 월 변화 수 (≈ 만기 통과 횟수)
        ws.cell(row=r, column=11, value=(
            f'=IF(F{r}=1,IFERROR('
            f'(YEAR(OFFSET(A{r},1+$B$4,0))*12+MONTH(OFFSET(A{r},1+$B$4,0)))'
            f'-(YEAR(OFFSET(A{r},1,0))*12+MONTH(OFFSET(A{r},1,0)))'
            f',""),"")'
        ))
        # I 수익률 = 부호*(청산/진입-1) - 롤횟수*롤비용
        ws.cell(row=r, column=9, value=(
            f'=IF(OR(G{r}="",H{r}=""),"",'
            f'IF($B$2="short",-(H{r}/G{r}-1),(H{r}/G{r}-1))-K{r}*$B$5/100)'
        ))
        # J PnL($) = 수익률 * 진입notional(진입가*1000)
        ws.cell(row=r, column=10,
                value=f'=IF(I{r}="","",I{r}*G{r}*1000)')

    # 수익률·PnL 포맷
    for idx in range(n):
        r = 9 + idx
        ws.cell(row=r, column=9).number_format = "+0.00%;-0.00%"
        ws.cell(row=r, column=10).number_format = "#,##0"
        ws.cell(row=r, column=1).number_format = "yyyy-mm-dd"

    # ── 열 너비 ──────────────────────────────────────────────────────
    widths = [12, 9, 9, 9, 9, 7, 10, 10, 10, 12, 8]
    for j, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(1 + j)].width = w
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 14

    # 헤더 행 고정 (스크롤해도 보이게)
    ws.freeze_panes = "A9"

    # ── 로직 설명 시트 ───────────────────────────────────────────────
    doc = wb.create_sheet("로직설명")
    notes = [
        ["WTI 선물 백테스트 — 엑셀 로직 설명", ""],
        ["", ""],
        ["입력칸", "백테스트 시트 B2~B5의 노란 칸을 바꾸면 전체 재계산"],
        ["방향", "short=고가가 임계값 위로 첫 터치 시 매도, long=저가가 아래로 첫 터치 시 매수"],
        ["신호(F열)", "장중 고가/저가 기준 전일 대비 첫 돌파 (히스테리시스 — 연속 돌파는 1회만)"],
        ["진입가(G열)", "신호 다음 영업일 시가 (look-ahead 제거)"],
        ["청산가(H열)", "진입 후 '보유기간' 영업일 후 종가"],
        ["롤횟수(K열)", "진입~청산 사이 캘린더 월 변화 수 ≈ 선물 만기 강제 롤오버 횟수"],
        ["수익률(I열)", "부호×(청산/진입−1) − 롤횟수×롤비용. short는 하락이 이익"],
        ["PnL(J열)", "수익률 × 진입가 × 1000배럴(1계약)"],
        ["", ""],
        ["한계", "수수료·슬리피지·MAE/MFE·walk-forward는 미반영(gross 기준). 롤횟수는 월 근사."],
        ["주의", "롤비용 양수=콘탱고 비용(차감), 음수=backwardation 이익(가산)"],
    ]
    for i, (a, b) in enumerate(notes):
        doc.cell(row=1 + i, column=1, value=a).font = bold if i == 0 else Font()
        doc.cell(row=1 + i, column=2, value=b)
    doc.column_dimensions["A"].width = 16
    doc.column_dimensions["B"].width = 70

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
