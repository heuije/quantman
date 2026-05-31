"""WTI 원유선물 분석 CLI.

사용 예:
    # 1) 특정 임계값의 신호 목록
    python -m quant_core.oil_futures.cli signals --threshold 80 --type short

    # 2) 한 번에 여러 임계값 백테스트
    python -m quant_core.oil_futures.cli backtest \\
        --shorts 80,90,100 --longs 30,40,50 --horizons 20,60,120

    # 3) 전체 그리드 → CSV (대시보드용)
    python -m quant_core.oil_futures.cli grid --output grid_results.csv

    # 4) walk-forward (in-sample/out-of-sample 비교)
    python -m quant_core.oil_futures.cli walkforward --split 2020-01-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from .backtest import CostModel, run_backtest
from .data import load_wti
from .metrics import summarize
from .optimizer import grid_search, grid_to_dataframe, walk_forward
from .signals import generate_signals

console = Console()

# 엑셀 원본과 동일한 기본 임계값/horizon (대조 검증용 디폴트)
DEFAULT_SHORTS = [80, 90, 100, 110, 120, 130, 140, 150]
DEFAULT_LONGS = [10, 20, 30, 40, 50, 60]
DEFAULT_HORIZONS = [20, 40, 60, 120]


def _parse_list(s: str, t=float) -> list:
    return [t(x.strip()) for x in s.split(",") if x.strip()]


def _cost_from_args(args) -> CostModel:
    return CostModel(
        commission_per_contract=args.commission,
        slippage_ticks=args.slippage_ticks,
    )


# ───── subcommands ──────────────────────────────────────────────────────────

def cmd_signals(args) -> None:
    df = load_wti(args.csv) if args.csv else load_wti()
    short_th = [args.threshold] if args.type == "short" else []
    long_th = [args.threshold] if args.type == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th)
    if args.since:
        cut = pd.Timestamp(args.since)
        sigs = [s for s in sigs if s.date >= cut]

    table = Table(title=f"{args.type.upper()} 신호 (임계값 {args.threshold:g})")
    table.add_column("Date")
    table.add_column("종가", justify="right")
    for s in sigs:
        table.add_row(s.date.strftime("%Y-%m-%d"), f"{s.entry_ref_close:.2f}")
    console.print(table)
    console.print(f"\n총 [bold]{len(sigs)}[/]건")


def cmd_backtest(args) -> None:
    df = load_wti(args.csv) if args.csv else load_wti()
    shorts = _parse_list(args.shorts) if args.shorts else []
    longs = _parse_list(args.longs) if args.longs else []
    horizons = _parse_list(args.horizons, int)
    cost = _cost_from_args(args)

    if not shorts and not longs:
        console.print("[red]--shorts 또는 --longs 중 최소 하나는 지정해야 합니다.[/]")
        sys.exit(2)

    table = Table(title="백테스트 결과")
    for h, just in [
        ("Side", "left"), ("임계값", "right"), ("Horizon", "right"),
        ("n", "right"), ("승률", "right"), ("평균수익", "right"),
        ("PF", "right"), ("Sharpe", "right"), ("MDD($)", "right"),
        ("Net PnL($)", "right"), ("⚠", "center"),
    ]:
        table.add_column(h, justify=just)

    def _row(side, th, h, s):
        pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
        table.add_row(
            side, f"{th:g}", str(h), str(s.n_trades),
            f"{s.win_rate:.1%}", f"{s.avg_return:+.2%}",
            pf, f"{s.sharpe_annualized:.2f}",
            f"{s.max_drawdown_usd:,.0f}", f"{s.total_net_pnl_usd:,.0f}",
            "⚠️" if s.low_sample else "",
        )

    for th in shorts:
        sigs = generate_signals(df, short_thresholds=[th])
        for h in horizons:
            bt = run_backtest(df, sigs, horizon_days=h, cost=cost)
            _row("short", th, h, summarize(bt))
    for th in longs:
        sigs = generate_signals(df, long_thresholds=[th])
        for h in horizons:
            bt = run_backtest(df, sigs, horizon_days=h, cost=cost)
            _row("long", th, h, summarize(bt))

    console.print(table)
    console.print(
        "\n[dim]⚠ = 거래 수 30 미만 (low_sample) — 통계 신뢰도 낮음[/]"
    )


def cmd_grid(args) -> None:
    df = load_wti(args.csv) if args.csv else load_wti()
    shorts = _parse_list(args.shorts) if args.shorts else DEFAULT_SHORTS
    longs = _parse_list(args.longs) if args.longs else DEFAULT_LONGS
    horizons = _parse_list(args.horizons, int) if args.horizons else DEFAULT_HORIZONS
    cost = _cost_from_args(args)

    cells = grid_search(df, shorts, longs, horizons, cost)
    df_out = grid_to_dataframe(cells)
    df_out_sorted = df_out.sort_values(
        "total_net_pnl_usd", ascending=False
    ).reset_index(drop=True)

    if args.output:
        out_path = Path(args.output)
        df_out_sorted.to_csv(out_path, index=False)
        console.print(f"저장: [green]{out_path}[/] ({len(df_out_sorted)}행)")

    top_n = min(args.top, len(df_out_sorted))
    table = Table(title=f"전체 그리드 TOP {top_n} (Net PnL 기준)")
    for h, just in [
        ("Side", "left"), ("임계", "right"), ("H", "right"),
        ("n", "right"), ("승률", "right"), ("평균수익", "right"),
        ("Sharpe", "right"), ("Net PnL", "right"), ("⚠", "center"),
    ]:
        table.add_column(h, justify=just)
    for _, r in df_out_sorted.head(top_n).iterrows():
        table.add_row(
            r["side"], f"{r['threshold']:g}", str(int(r["horizon"])),
            str(int(r["n_trades"])), f"{r['win_rate']:.1%}",
            f"{r['avg_return']:+.2%}", f"{r['sharpe_annualized']:.2f}",
            f"{r['total_net_pnl_usd']:,.0f}",
            "⚠️" if r["low_sample"] else "",
        )
    console.print(table)


def cmd_walkforward(args) -> None:
    df = load_wti(args.csv) if args.csv else load_wti()
    shorts = _parse_list(args.shorts) if args.shorts else DEFAULT_SHORTS
    longs = _parse_list(args.longs) if args.longs else DEFAULT_LONGS
    horizons = _parse_list(args.horizons, int) if args.horizons else DEFAULT_HORIZONS
    cost = _cost_from_args(args)
    split = pd.Timestamp(args.split)

    res = walk_forward(df, shorts, longs, horizons, split, cost)

    console.print(
        f"[bold]Train[/]: {res.train_period[0].date()} ~ {res.train_period[1].date()}"
    )
    console.print(
        f"[bold]Test [/]: {res.test_period[0].date()} ~ {res.test_period[1].date()}"
    )

    b = res.best_in_sample
    console.print(
        f"\n[green]Best in-sample[/]: {b.side.value} "
        f"th={b.threshold:g} h={b.horizon_days}"
    )
    bs = b.summary
    console.print(
        f"  n={bs.n_trades}, 승률={bs.win_rate:.1%}, "
        f"평균수익={bs.avg_return:+.2%}, Sharpe={bs.sharpe_annualized:.2f}, "
        f"Net PnL=${bs.total_net_pnl_usd:,.0f}"
    )

    oos = res.best_out_of_sample
    color = "yellow" if oos.total_net_pnl_usd > 0 else "red"
    console.print(f"\n[{color}]Out-of-sample (동일 파라미터)[/]:")
    console.print(
        f"  n={oos.n_trades}, 승률={oos.win_rate:.1%}, "
        f"평균수익={oos.avg_return:+.2%}, Sharpe={oos.sharpe_annualized:.2f}, "
        f"Net PnL=${oos.total_net_pnl_usd:,.0f}"
        + (" ⚠️ low sample" if oos.low_sample else "")
    )

    # 격차 진단
    if bs.n_trades > 0 and oos.n_trades > 0:
        ratio = oos.avg_return / bs.avg_return if bs.avg_return else 0
        if ratio < 0:
            console.print(
                "\n[red]⚠️  OOS 수익이 음수 — overfit 강하게 의심[/]"
            )
        elif ratio < 0.3:
            console.print(
                "\n[yellow]⚠️  OOS 수익이 IS의 30% 미만 — overfit 가능성[/]"
            )


# ───── main ─────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m quant_core.oil_futures.cli",
        description="WTI 원유선물 분석 (Phase 1)",
    )
    p.add_argument("--csv", help="WTI 일봉 CSV 경로 (기본: core/data/wti_daily.csv)")
    p.add_argument("--commission", type=float, default=2.5,
                   help="계약당 한방향 수수료 USD (기본 2.5)")
    p.add_argument("--slippage-ticks", type=int, default=1,
                   help="진입/청산 각각 N틱 슬리피지 (기본 1, $0.01/틱)")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_sig = sub.add_parser("signals", help="특정 임계값의 신호 목록")
    p_sig.add_argument("--threshold", type=float, required=True)
    p_sig.add_argument("--type", choices=["short", "long"], required=True)
    p_sig.add_argument("--since", help="이 날짜 이후 신호만 (YYYY-MM-DD)")
    p_sig.set_defaults(func=cmd_signals)

    p_bt = sub.add_parser("backtest", help="여러 임계값×horizon 백테스트")
    p_bt.add_argument("--shorts", default="", help="쉼표구분 (예: 80,90,100)")
    p_bt.add_argument("--longs", default="", help="쉼표구분 (예: 30,40,50)")
    p_bt.add_argument("--horizons", default="20,40,60,120")
    p_bt.set_defaults(func=cmd_backtest)

    p_gr = sub.add_parser("grid", help="전체 그리드 (기본 엑셀 임계값 사용)")
    p_gr.add_argument("--shorts", default="")
    p_gr.add_argument("--longs", default="")
    p_gr.add_argument("--horizons", default="")
    p_gr.add_argument("--output", help="CSV 저장 경로")
    p_gr.add_argument("--top", type=int, default=15, help="상위 N개 콘솔 출력")
    p_gr.set_defaults(func=cmd_grid)

    p_wf = sub.add_parser("walkforward", help="train/test 분할 walk-forward")
    p_wf.add_argument("--split", required=True, help="YYYY-MM-DD")
    p_wf.add_argument("--shorts", default="")
    p_wf.add_argument("--longs", default="")
    p_wf.add_argument("--horizons", default="")
    p_wf.set_defaults(func=cmd_walkforward)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
