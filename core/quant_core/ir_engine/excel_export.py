"""인사이트 엔진 분석 → '증빙' 엑셀(.xlsx) 생성.

목적: 챗봇/빌더가 IR 백테스트를 돌리면 *결과만* 주는 게 아니라, **어떤 데이터를 어떤
연산으로** 산출했는지 엑셀로 증빙한다. 선물 분석의 build_oil_excel(라이브 수식)과 같은
취지 — 데이터 + 수식이 든 파일로, 사용자가 직접 검증한다.

엔진은 정수주·현금·마진·지연체결을 가진 **이벤트 구동 NAV 시뮬레이션**이라 셀 수식으로
*정확히* 복제할 수 없다(naive cumprod(가중×수익)은 엔진과 불일치). 그래서 라이브 수식은
엔진의 **정본 자산곡선(equity) 위에서 정의식 변환**(일수익·낙폭·CAGR·샤프·MDD)만 한다 —
이 값들은 엔진 metrics와 *정확히 일치*(증빙)하고, '일일 추가비용(bps)' 노란 칸으로
사후 민감도를 라이브로 본다(0이면 엔진과 동일). 전략 로직/파라미터 변경의 라이브 재계산은
'변수 조정'(/ir/strategy 재실행)이 담당 — 엑셀은 산술·비용 검증 도구다.

시트:
- 백테스트: 엔진 자산곡선 + 라이브 정의식(일수익·조정수익·조정자산·낙폭) + 지표 비교.
- 거래내역: 엔진 trades(정적 값) — 검증 앵커.
- 원자료: 보유종목 종가(정적 값) — '어떤 데이터'.
- 일별비중: 엔진 EOD 기여 비중 패널(정적 값) — '어떤 포지션'.
- 지표·설명: 엔진 metrics 전체 + 전략 정의(IR) + 방법론·한계.
"""
from __future__ import annotations

import io
import json
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side as XlSide
from openpyxl.utils import get_column_letter

from .spec import StrategyIR

# 디자인 토큰 (네이비/골드 — DESIGN.md 계열, 인쇄 가독 우선 라이트)
_ACCENT = "A6791F"     # 골드(진하게 — 텍스트 가독)
_HEAD_BG = "E9EDF3"    # 헤더 배경(라이트 네이비그레이)
_INPUT_BG = "FFF4CC"   # 입력칸(노랑 — 편집 가능 관례)
_SPEC_BG = "EFEAE3"
_BORDER = "D8DEE7"
_NOTE = "6F6A62"

_MAX_SYMS = 40   # 원자료·일별비중 가로 폭 상한(대형 유니버스 — 초과 시 명시 생략)

# 백테스트 시트 레이아웃 행 좌표
_ROW_TITLE = 1
_ROW_INPUT = 3
_ROW_SUMHEAD = 5
_ROW_SUM0 = 6        # 지표 비교 5행: 6~10
_ROW_DHEAD = 12
_ROW_D0 = 13         # 데이터 첫 행

# 엔진 metrics 키 → 한글 라벨 (없는 키는 원문 사용)
_METRIC_LABELS = {
    "total_return": "누적수익률(%)", "cagr": "연복리수익률 CAGR(%)",
    "mdd": "최대낙폭 MDD(%)", "sharpe": "샤프(연)", "sortino": "소르티노(연)",
    "calmar": "칼마", "n_trades": "거래 수", "win_rate": "승률(%)",
    "avg_hold": "평균 보유일", "avg_trade_return": "평균 거래수익률(%)",
    "bench_total": "벤치마크 누적(%)", "bench_cagr": "벤치마크 CAGR(%)",
    "bench_mdd": "벤치마크 MDD(%)", "excess_return": "초과수익(%)",
    "var_95": "VaR 95(%)", "cvar_95": "CVaR 95(%)", "beta": "베타",
    "profit_factor": "손익비 PF", "payoff_ratio": "페이오프",
}


def _thin_border() -> Border:
    s = XlSide(style="thin", color=_BORDER)
    return Border(left=s, right=s, top=s, bottom=s)


def _num(v: Any):
    """엔진 값 → 엑셀 셀 값. NaN/None은 빈칸으로(엑셀에서 비교 시 노이즈 방지)."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f        # NaN → None
    except (TypeError, ValueError):
        return v


def _ir_dict(ir: Any) -> dict:
    if hasattr(ir, "model_dump"):
        return ir.model_dump(mode="json")
    return ir if isinstance(ir, dict) else {}


def _held_symbols(result: dict) -> tuple[list[str], int]:
    """보유 종목(비중 패널 기준, 총 절대비중 내림차순). (상한 적용 목록, 전체 수)."""
    syms: list[str] = []
    w = result.get("weight")
    if isinstance(w, pd.DataFrame) and not w.empty:
        order = w.abs().sum().sort_values(ascending=False)
        syms = [str(s) for s in order.index]
    if not syms:
        tdf = result.get("trades")
        if isinstance(tdf, pd.DataFrame) and "종목" in tdf.columns:
            syms = list(dict.fromkeys(str(s) for s in tdf["종목"].tolist()))
    return syms[:_MAX_SYMS], len(syms)


def build_strategy_excel(
    ir: StrategyIR | dict,
    dataset: dict[str, pd.DataFrame],
    result: dict,
    *,
    name: str | None = None,
) -> bytes:
    """SIMULATE(1회 백테스트) 결과 → 증빙 .xlsx 바이트.

    ir: 백테스트한 StrategyIR(또는 dict) — '전략 정의' 시트 렌더.
    dataset: {symbol: OHLCV DataFrame} — 보유종목 종가('원자료' 시트).
    result: run_strategy_ir/strategy_from_spec 의 *원본* dict(pandas 객체 포함) —
        equity(Series)·trades(DataFrame)·weight(DataFrame)·metrics(dict) 필요.
    """
    equity = result.get("equity")
    if not isinstance(equity, pd.Series) or equity.empty:
        raise ValueError("build_strategy_excel: result['equity'] (pd.Series) 필요 — "
                         "SIMULATE(1회 백테스트) 결과만 지원합니다.")
    metrics = result.get("metrics") or {}
    disp = name or (_ir_dict(ir).get("name") or "전략")

    wb = Workbook()
    bold = Font(bold=True)
    title_font = Font(bold=True, size=14, color="20201D")
    accent_font = Font(bold=True, color=_ACCENT)
    italic = Font(italic=True, color=_NOTE)
    input_fill = PatternFill("solid", fgColor=_INPUT_BG)
    head_fill = PatternFill("solid", fgColor=_HEAD_BG)
    border = _thin_border()
    center = Alignment(horizontal="center")

    # ── 시트 1: 백테스트 (엔진 자산곡선 + 라이브 정의식 수식) ────────────────────
    ws = wb.active
    ws.title = "백테스트"
    n = len(equity)
    last = _ROW_D0 + n - 1

    ws.cell(_ROW_TITLE, 1, f"{disp} — 백테스트 증빙 (라이브 수식)").font = title_font

    # 입력칸: 일일 추가비용(bps). 0이면 엔진 그대로, >0이면 사후 비용 민감도.
    ws.cell(_ROW_INPUT, 1, "일일 추가비용 (bps)").font = bold
    inp = ws.cell(_ROW_INPUT, 2, 0)
    inp.fill = input_fill
    inp.border = border
    inp.alignment = center
    note = ws.cell(_ROW_INPUT, 3,
                   "← 노란 칸을 바꾸면 '조정' 열·지표가 라이브 재계산 (0 = 엔진과 동일)")
    note.font = italic

    # 지표 비교: 엔진(정본 값) vs 엑셀(자산곡선 위 정의식 수식). bps=0이면 일치 = 증빙.
    for j, h in enumerate(("지표", "엔진", "엑셀(라이브수식)", "비고")):
        c = ws.cell(_ROW_SUMHEAD, 1 + j, h)
        c.font = bold
        c.fill = head_fill
        c.border = border
        c.alignment = center
    sums = [
        ("누적수익률(%)", _num(metrics.get("total_return")),
         f"=(E{last}/E{_ROW_D0}-1)*100", "bps=0이면 엔진과 일치"),
        ("연복리수익률 CAGR(%)", _num(metrics.get("cagr")),
         f"=IFERROR(((E{last}/E{_ROW_D0})^(252/COUNT(E{_ROW_D0}:E{last}))-1)*100,\"\")", ""),
        ("샤프(연)", _num(metrics.get("sharpe")),
         f"=IFERROR(AVERAGE(D{_ROW_D0 + 1}:D{last})/STDEV.S(D{_ROW_D0 + 1}:D{last})*SQRT(252),\"\")",
         "엔진=일별 표본 std"),
        ("최대낙폭 MDD(%)", _num(metrics.get("mdd")),
         f"=MIN(F{_ROW_D0}:F{last})*100", ""),
        ("연변동성(%)", "—",
         f"=IFERROR(STDEV.S(D{_ROW_D0 + 1}:D{last})*SQRT(252)*100,\"\")", "엔진 미보고"),
    ]
    for i, (label, eng, formula, memo) in enumerate(sums):
        r = _ROW_SUM0 + i
        ws.cell(r, 1, label).font = accent_font
        ce = ws.cell(r, 2, eng)
        ce.number_format = "0.00"
        ce.border = border
        cf = ws.cell(r, 3, formula)
        cf.number_format = "0.00"
        cf.border = border
        ws.cell(r, 4, memo).font = italic

    # 데이터 + 수식 헤더
    dheaders = ["날짜", "자산(엔진)", "일수익률", "조정수익률", "조정자산", "낙폭"]
    for j, h in enumerate(dheaders):
        c = ws.cell(_ROW_DHEAD, 1 + j, h)
        c.font = bold
        c.fill = head_fill
        c.border = border
        c.alignment = center

    idx_list = list(equity.index)
    val_list = [float(v) for v in equity.to_numpy()]
    for i in range(n):
        r = _ROW_D0 + i
        ws.cell(r, 1, pd.Timestamp(idx_list[i]).to_pydatetime())  # A 날짜
        ws.cell(r, 2, val_list[i])                                # B 자산(엔진, 정본 값)
        if i == 0:
            # 첫 행: 수익률 없음. 조정자산 시작 = 엔진 자산(겹쳐 그려져 bps=0 검증).
            ws.cell(r, 5, f"=B{r}")                                # E 조정자산 시작
            ws.cell(r, 6, f"=E{r}/MAX(E${_ROW_D0}:E{r})-1")       # F 낙폭(=0)
        else:
            ws.cell(r, 3, f"=B{r}/B{r - 1}-1")                    # C 일수익률(엔진 자산)
            ws.cell(r, 4, f"=C{r}-$B${_ROW_INPUT}/10000")         # D 조정수익률(−bps)
            ws.cell(r, 5, f"=E{r - 1}*(1+D{r})")                  # E 조정자산(복리)
            ws.cell(r, 6, f"=E{r}/MAX(E${_ROW_D0}:E{r})-1")       # F 낙폭

    for i in range(n):
        r = _ROW_D0 + i
        ws.cell(r, 1).number_format = "yyyy-mm-dd"
        ws.cell(r, 2).number_format = "#,##0"
        ws.cell(r, 3).number_format = "0.00%"
        ws.cell(r, 4).number_format = "0.00%"
        ws.cell(r, 5).number_format = "#,##0"
        ws.cell(r, 6).number_format = "0.00%"
    for col, w in zip("ABCDEF", (12, 14, 11, 11, 14, 10)):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["C"].width = 18   # 비고 노트 가독
    ws.freeze_panes = f"A{_ROW_D0}"

    # ── 시트 2: 거래내역 (엔진 trades — 정적 값, 검증 앵커) ──────────────────────
    tdf = result.get("trades")
    tr = wb.create_sheet("거래내역")
    n_tr = len(tdf) if isinstance(tdf, pd.DataFrame) else 0
    tr.cell(1, 1, f"거래내역 — {disp} (거래 {n_tr}건, 엔진 결과 정적 값)").font = title_font
    tr.cell(2, 1, "거래 수").font = accent_font
    tr.cell(2, 2, n_tr).font = bold
    tr.cell(2, 3, "← 엔진 백테스트가 산출한 확정 거래(라이브 수식 아님 — 검증 기준)").font = italic
    t_cols = ["종목", "진입일", "청산일", "보유일", "진입가", "청산가", "수익률(%)", "청산사유"]
    for j, h in enumerate(t_cols):
        c = tr.cell(4, 1 + j, h)
        c.font = bold
        c.fill = head_fill
        c.border = border
        c.alignment = center
    if isinstance(tdf, pd.DataFrame) and n_tr:
        for i, (_, row) in enumerate(tdf.iterrows()):
            r = 5 + i
            tr.cell(r, 1, str(row.get("종목", "")))
            tr.cell(r, 2, pd.Timestamp(row["진입일"]).to_pydatetime()
                    if pd.notna(row.get("진입일")) else None).number_format = "yyyy-mm-dd"
            tr.cell(r, 3, pd.Timestamp(row["청산일"]).to_pydatetime()
                    if pd.notna(row.get("청산일")) else None).number_format = "yyyy-mm-dd"
            tr.cell(r, 4, int(row.get("보유일")) if pd.notna(row.get("보유일")) else None)
            tr.cell(r, 5, round(float(row["진입가"]), 4)
                    if pd.notna(row.get("진입가")) else None).number_format = "#,##0.00"
            tr.cell(r, 6, round(float(row["청산가"]), 4)
                    if pd.notna(row.get("청산가")) else None).number_format = "#,##0.00"
            tr.cell(r, 7, round(float(row["수익률(%)"]), 4)
                    if pd.notna(row.get("수익률(%)")) else None).number_format = "+0.00;-0.00"
            tr.cell(r, 8, str(row.get("청산사유", "")))
    for col, w in zip("ABCDEFGH", (10, 12, 12, 8, 11, 11, 10, 14)):
        tr.column_dimensions[col].width = w
    tr.freeze_panes = "A5"

    # 보유종목 목록(원자료·일별비중 공용)
    syms, n_syms_total = _held_symbols(result)

    # ── 시트 3: 원자료 (보유종목 종가 — 정적 값, '어떤 데이터') ──────────────────
    raw = wb.create_sheet("원자료")
    raw.cell(1, 1, f"원자료 — 보유종목 종가 ({disp})").font = title_font
    raw.cell(2, 1, "각 열 = 종목 종가(Close). 엔진은 전체 OHLCV 사용 — 여기 종가는 마킹·수익 기준.").font = italic
    if n_syms_total > len(syms):
        raw.cell(3, 1, f"※ 보유종목 {n_syms_total}개 중 비중 상위 {len(syms)}개만 표시(가로 폭 상한).").font = italic
    raw.cell(5, 1, "날짜").font = bold
    raw.cell(5, 1).fill = head_fill
    raw.cell(5, 1).border = border
    closes: dict[str, pd.Series] = {}
    for j, s in enumerate(syms):
        c = raw.cell(5, 2 + j, s)
        c.font = bold
        c.fill = head_fill
        c.border = border
        c.alignment = center
        df = dataset.get(s)
        if isinstance(df, pd.DataFrame) and "Close" in df.columns:
            closes[s] = df["Close"].reindex(equity.index)
    for i in range(n):
        r = 6 + i
        raw.cell(r, 1, pd.Timestamp(idx_list[i]).to_pydatetime()).number_format = "yyyy-mm-dd"
        for j, s in enumerate(syms):
            ser = closes.get(s)
            v = ser.iloc[i] if ser is not None else None
            cell = raw.cell(r, 2 + j, None if v is None or pd.isna(v) else float(v))
            cell.number_format = "#,##0.00"
    raw.column_dimensions["A"].width = 12
    for j in range(len(syms)):
        raw.column_dimensions[get_column_letter(2 + j)].width = 11
    raw.freeze_panes = "B6"

    # ── 시트 4: 일별비중 (엔진 EOD 기여 비중 — 정적 값, '어떤 포지션') ───────────
    wpanel = result.get("weight")
    wp = wb.create_sheet("일별비중")
    wp.cell(1, 1, f"일별 비중 — 엔진 EOD 기여 패널 ({disp})").font = title_font
    wp.cell(2, 1, "양수=롱·음수=숏 (자기자본 대비 비중). 엔진이 기록한 일별 포지션 — '어떤 연산'의 가중치.").font = italic
    wp.cell(4, 1, "날짜").font = bold
    wp.cell(4, 1).fill = head_fill
    wp.cell(4, 1).border = border
    for j, s in enumerate(syms):
        c = wp.cell(4, 2 + j, s)
        c.font = bold
        c.fill = head_fill
        c.border = border
        c.alignment = center
    if isinstance(wpanel, pd.DataFrame) and not wpanel.empty:
        wp_al = wpanel.reindex(equity.index)
        for i in range(n):
            r = 5 + i
            wp.cell(r, 1, pd.Timestamp(idx_list[i]).to_pydatetime()).number_format = "yyyy-mm-dd"
            for j, s in enumerate(syms):
                v = wp_al[s].iloc[i] if s in wp_al.columns else None
                cell = wp.cell(r, 2 + j, None if v is None or pd.isna(v) else float(v))
                cell.number_format = "0.00%"
    wp.column_dimensions["A"].width = 12
    for j in range(len(syms)):
        wp.column_dimensions[get_column_letter(2 + j)].width = 11
    wp.freeze_panes = "B5"

    # ── 시트 5: 지표·설명 (엔진 metrics 전체 + 전략 정의 + 방법론) ───────────────
    info = wb.create_sheet("지표·설명")
    info.cell(1, 1, f"{disp} — 엔진 지표 · 전략 정의 · 방법론").font = title_font
    info.cell(3, 1, "엔진 지표 (정본)").font = accent_font
    rr = 4
    for k, v in metrics.items():
        info.cell(rr, 1, _METRIC_LABELS.get(k, k)).font = bold
        val = _num(v)
        cell = info.cell(rr, 2, val)
        if isinstance(val, float):
            cell.number_format = "0.0000"
        rr += 1

    rr += 1
    info.cell(rr, 1, "전략 정의 (IR)").font = accent_font
    rr += 1
    ird = _ir_dict(ir)
    for key in ("name", "query", "universe", "signal", "position", "simulation", "study"):
        if key not in ird or ird[key] in (None, {}, []):
            continue
        info.cell(rr, 1, key).font = bold
        v = ird[key]
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        if len(text) > 900:
            text = text[:900] + " …"
        info.cell(rr, 2, text).alignment = Alignment(wrap_text=True, vertical="top")
        rr += 1

    rr += 1
    info.cell(rr, 1, "방법론 · 한계").font = accent_font
    rr += 1
    notes = [
        ("라이브 수식", "엔진 정본 자산곡선(B열) 위에서 일수익·낙폭·CAGR·샤프·MDD를 정의식으로 재계산 — bps=0이면 엔진 지표와 일치(증빙)."),
        ("일일 추가비용(bps)", "백테스트 시트 B3. 일별 수익률에서 차감하는 사후 비용 민감도(라이브). 엔진 비용모델 변경이 아님."),
        ("거래내역", "엔진이 산출한 확정 거래(정적 값). 진입가·청산가·수익률은 엔진과 byte 일치 — 검증 앵커."),
        ("엑셀이 복제 못 하는 것", "엔진은 정수주·현금·마진콜·지연체결의 이벤트 NAV 시뮬레이션 — 셀 수식으로 NAV 경로를 정확히 재현하지 않는다(자산곡선은 엔진 값 그대로 사용)."),
        ("전략 로직 변경", "임계값·기간·종목 등 전략 파라미터를 바꿔 다시 보려면 '변수 조정'(엔진 재실행) — 엑셀은 산술·비용 검증 도구."),
    ]
    for k, v in notes:
        info.cell(rr, 1, k).font = bold
        info.cell(rr, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
        rr += 1
    info.column_dimensions["A"].width = 22
    info.column_dimensions["B"].width = 90

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
