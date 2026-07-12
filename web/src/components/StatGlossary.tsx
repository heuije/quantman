// 통계·성과 지표 용어집(SSOT) — 챗 시각화에 항상 붙는 한 줄 설명.
// 정의는 엔진 구현과 동기해야 한다: p값(1표본)=core/quant_core/ir_engine/compare.py
// one_sample_test의 ttest_1samp(arr, 0.0) = 귀무가설 '평균=0'·**양측** 검정,
// 2표본=two_sample_test(Welch·등분산 가정 없음). 문구를 바꾸면 compare.py와 대조할 것.
// 배경: 사용자 실측 피드백 — p값의 귀무가설·MFE 뜻이 화면 어디에도 없어 오독 여지.

export const STAT_GLOSSARY: Record<string, string> = {
  p_value:
    "p값 = 양측 t-검정(귀무가설: 평균 0 — '효과 없음'일 때 이런 평균이 우연히 나올 확률). " +
    "p<0.05면 평균이 0과 유의하게 다름 — 방향은 평균의 부호로 판단('유의'≠상승)",
  p_value_2s: "p값 = Welch 2표본 t-검정(귀무가설: 두 구간의 평균이 같다·등분산 가정 없음)",
  t_stat: "t = 평균이 0에서 벗어난 정도(표준오차 단위·|t|≥2면 대략 유의)",
  mean_mae: "MAE = 평균 최대 불리 폭(진입 후 최악 시점까지의 평균 하락% — 경로 위험)",
  mean_mfe: "MFE = 평균 최대 유리 폭(보유 중 최고 시점까지의 평균 상승% — 잠재 이익)",
  prob_positive: "양(+)확률 = 수익이 양수였던 표본 비율(단순 비율·통계 검정 아님)",
  payoff_ratio: "손익비 = 평균 이익 ÷ |평균 손실|",
  ic: "IC = 신호값과 forward 수익의 순위상관(-1~+1·클수록 예측력)",
  ic_ir: "IR = 평균 IC ÷ IC 변동성(예측의 일관성)",
  sharpe: "샤프 = 수익 ÷ 변동성(연환산·위험 대비 수익)",
  sortino: "소르티노 = 하락 변동성만 벌점으로 하는 샤프 변형",
  mdd: "MDD = 최대 낙폭(고점 대비 최대 하락%)",
  cagr: "CAGR = 연평균 복리 수익률",
  win_rate: "승률 = 이익 거래의 비율",
  quantiles: "하위/상위 5% = 분포의 5%·95% 분위수(꼬리 수준)",
  oos: "OOS = 표본외 검증(최적화에 안 쓴 구간에서도 성과가 유지되는지 — 과최적화 점검)",
  coef: "계수 = 해당 팩터 1단위당 forward 수익 기여(Fama-MacBeth 평균·CI가 0을 안 걸치면 유의)",
};

/** 시각화 하단 지표 설명 각주 — keys 순서대로 용어집 문구를 이어 붙인다(없는 키는 무시). */
export function StatNote({ keys }: { keys: string[] }) {
  const items = keys.map((k) => STAT_GLOSSARY[k]).filter(Boolean);
  if (!items.length) return null;
  return (
    <div className="muted" style={{ fontSize: 11, marginTop: 6, lineHeight: 1.6 }}>
      {items.map((s, i) => (
        <span key={i}>
          {i > 0 && " · "}
          {s}
        </span>
      ))}
    </div>
  );
}
