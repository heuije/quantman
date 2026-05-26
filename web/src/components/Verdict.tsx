/** 백테스트 지표를 객관적으로 풀어주는 카드.
 *  주관적 평가("우수/추천" 등)는 투자자문으로 해석될 수 있어 배제.
 *  지표가 무엇을 뜻하는지 평이한 한국어로만 해설한다.
 */

import type { ReactNode } from "react";
import { fmt2 } from "../format";

const num = (v: number | null | undefined): number | null =>
  v == null || Number.isNaN(v) ? null : v;

/** 전문용어 옆 hover info-tip. 라벨 텍스트 직후에 ⓘ. */
function Term({ children, tip }: { children: ReactNode; tip: string }) {
  return (
    <>
      {children}
      <span className="metric-hint" data-tip={tip}>ⓘ</span>
    </>
  );
}

export default function Verdict({ metrics }: {
  metrics: Record<string, number | null>;
}) {
  const ret = num(metrics.total_return);
  const cagr = num(metrics.cagr);
  const mdd = num(metrics.mdd);
  const sharpe = num(metrics.sharpe);
  const nTrades = num(metrics.n_trades);
  const winRate = num(metrics.win_rate);
  const excess = num(metrics.excess_return);

  // 손익이 있는 객관적 사실(누적 수익률·초과 수익)만 색을 입힌다.
  const signTone = (v: number) => v > 0 ? "pt-good" : v < 0 ? "pt-bad" : "pt-neutral";
  const lines: { node: ReactNode; tone: string }[] = [];

  if (ret != null && cagr != null) {
    lines.push({
      node: (
        <>
          백테스트 기간 누적 수익률은 {fmt2(ret)}%, 연평균 환산
          (<Term tip="CAGR (Compound Annual Growth Rate). 여러 해에 걸친 수익을 매년 동일한 비율로 복리 환산한 값. 예: 3년간 +33% → 약 +10%/년.">CAGR</Term>)
          은 {fmt2(cagr)}%입니다.
        </>
      ),
      tone: signTone(ret),
    });
  }
  if (mdd != null) {
    lines.push({
      node: (
        <>
          보유 중 한때 자산이 최대 {fmt2(mdd)}%까지 감소했습니다
          (<Term tip="MDD (Maximum Drawdown). 자본 고점에서 저점까지의 최대 하락폭(%). 값이 클수록 보유 중 손실 폭이 컸음. 변동성·심리적 부담의 척도.">MDD</Term>).
          {" "}값이 클수록 변동성이 크다는 뜻입니다.
        </>
      ),
      tone: "pt-neutral",
    });
  }
  if (sharpe != null) {
    lines.push({
      node: (
        <>
          <Term tip="Sharpe Ratio. 위험(변동성) 한 단위당 초과 수익. 일반적으로 1↑=양호, 2↑=우수. 음수면 무위험 수익보다 낮음.">샤프 비율</Term>
          {" "}{fmt2(sharpe)} — 변동성 한 단위당 거둔 수익을 보여주는 위험조정 수익 지표입니다.
        </>
      ),
      tone: "pt-neutral",
    });
  }
  if (winRate != null && nTrades != null) {
    lines.push({
      node: <>총 {nTrades}건의 매매 중 {fmt2(winRate)}%가 이익으로 마감되었습니다.</>,
      tone: "pt-neutral",
    });
  }
  if (excess != null) {
    lines.push({
      node: (
        <>
          같은 종목을 단순 매수·보유했을 때 대비 {fmt2(excess)}%p 차이입니다
          (<Term tip="Excess Return. 전략 수익률 − 단순 매수보유 수익률. 양수면 전략이 가치를 더했다는 객관적 사실(추천 아님).">초과 수익</Term>).
        </>
      ),
      tone: signTone(excess),
    });
  }
  if (nTrades != null && nTrades < 10) {
    lines.push({
      node: (
        <>
          매매 횟수가 {nTrades}건으로 적은 편입니다.
          {" "}<Term tip="통계의 안정성은 표본 크기(매매 횟수)에 비례. 표본이 30건 미만이면 한 번의 운/불운이 결과를 크게 좌우할 수 있음.">표본이 작을수록</Term>
          {" "}통계의 안정성이 낮아집니다.
        </>
      ),
      tone: "pt-warn",
    });
  }

  if (lines.length === 0) return null;

  const cardTone = ret != null && ret < 0 ? "warn" : "ok";

  return (
    <div className={"verdict " + cardTone}>
      <div className="verdict-head">
        <span className="verdict-tagline">결과 해설</span>
      </div>
      <ul className="verdict-points">
        {lines.map((t, i) => (
          <li key={i} className={t.tone}>• {t.node}</li>
        ))}
      </ul>
      <p className="verdict-summary muted" style={{ fontSize: 12, marginTop: 12 }}>
        과거 데이터에 기반한 시뮬레이션 결과로, 미래 수익을 보장하지 않습니다.
        본 화면은 정보 제공 목적이며 투자 자문이 아닙니다.
      </p>
    </div>
  );
}
