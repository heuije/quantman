# -*- coding: utf-8 -*-
"""장마감 후 자동 스냅샷 (헤드리스).

Streamlit 없이 실행되어 오늘자 마감 포트폴리오 성과를 performance_history.csv에 누적 기록.
Windows 작업 스케줄러가 매 거래일 장마감 후(예: 15:40) daily_snapshot.bat을 통해 호출.
"""
import os
import glob
import datetime as dt

import engine
import performance
import flows


def _kospi_close():
    try:
        import FinanceDataReader as fdr
        start = (dt.datetime.now() - dt.timedelta(days=10)).strftime("%Y-%m-%d")
        return float(fdr.DataReader("KS11", start)["Close"].dropna().iloc[-1])
    except Exception:
        return None


def main():
    # 당일 캐시 삭제 → 장마감 '종가'로 새로 받아 기록(장중 캐시 방지)
    for f in glob.glob(os.path.join(engine.CACHE, "*.csv")):
        try:
            os.remove(f)
        except Exception:
            pass

    df = engine.build_positions()
    tp = float(df["투자원금"].sum(skipna=True))
    te = float(df["평가금액"].sum(skipna=True))
    isa = engine.compute_isa_tax(df)
    pb = engine.compute_portfolio_beta(df)
    h = performance.record_today(tp, te, isa.get("세후수익률"), pb.get("beta_all"), _kospi_close())

    # 일별 투자자 수급 적립(보유 KRX 종목 + 코스피/코스닥 지수 프록시)
    krx = df[~df["티커"].isin(["CASH", "GOLD"])]
    pairs = [("[지수] 코스피", "069500"), ("[지수] 코스닥", "229200")]
    pairs += [(r["종목명"], str(r["티커"])) for _, r in krx.iterrows()]
    fh = flows.record_flows_today(pairs)

    ret = (te - tp) / tp * 100 if tp else 0.0
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] 스냅샷 기록 완료 · "
          f"성과 누적 {len(h)}일 · 수급 누적 {len(fh)}행 · "
          f"평가금액 {te:,.0f}원 · 수익률 {ret:+.2f}%")


if __name__ == "__main__":
    main()
