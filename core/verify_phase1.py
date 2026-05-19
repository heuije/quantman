"""Phase 1 검증 - core 패키지로 백테스트가 끝까지 도는지 확인."""

import sys

sys.stdout.reconfigure(encoding="utf-8")

import quant_core as qc


def main():
    print("1) 데이터셋 로딩...")
    data = qc.load_dataset(with_indicators=True)
    print(f"   심볼 {len(data)}개 로드")
    assert data, "데이터가 비어 있음"

    # OHLC를 갖춘 첫 심볼을 매수 대상으로 선택
    trade_symbol = next(
        (s for s, df in data.items()
         if {"Open", "Close"}.issubset(df.columns) and len(df) > 300),
        None,
    )
    assert trade_symbol, "OHLC 데이터를 갖춘 심볼이 없음"
    df = data[trade_symbol]
    print(f"2) 매수 대상: {trade_symbol} ({len(df)}행, {df.index[0].date()}~{df.index[-1].date()})")

    # 그 심볼에 존재하는 지표 컬럼 하나로 조건 구성
    indic = next((c for c in qc.get_indicator_columns() if c in df.columns), None)
    assert indic, "사용할 지표 컬럼이 없음"
    print(f"3) 시그널 지표: {indic} ({qc.get_indicator_label(indic)})")

    strategy = qc.Strategy(
        name="검증용 전략",
        trade_symbol=trade_symbol,
        buy=qc.ConditionGroup(
            conditions=[qc.Condition(symbol=trade_symbol, indicator=indic,
                                     op="<", value=0.0)],
            logic="AND",
        ),
        exit_rules=qc.ExitRules(hold_days=5, stop_loss=-5.0),
        amount_pct=100.0,
    )
    print("4) 통합 Strategy 객체 생성 OK")
    print("   JSON 직렬화:", strategy.model_dump_json()[:120], "...")

    print("5) 백테스트 실행...")
    result = qc.run_strategy_backtest(strategy, data)
    assert result["success"], f"백테스트 실패: {result.get('error')}"

    m = result["metrics"]
    print("   ─ 결과 ─────────────────────────")
    print(f"   거래 수      : {m['n_trades']}")
    print(f"   총수익률(%)  : {m['total_return']:.2f}")
    print(f"   승률(%)      : {m['win_rate']:.1f}")
    print(f"   MDD(%)       : {m['mdd']:.2f}")
    print(f"   샤프         : {m['sharpe']:.2f}")
    print("\n[OK] Phase 1 검증 통과 - core 패키지 백테스트 정상 동작")


if __name__ == "__main__":
    main()
