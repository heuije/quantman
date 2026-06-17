import { useState, useEffect, useMemo, useRef, type ReactNode } from "react";
import {
  ComposedChart, Line, Bar, Scatter, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, ReferenceArea, CartesianGrid, Legend, Cell,
} from "recharts";
import { api } from "../api";
import type {
  SymbolDetail, SymbolListing, SymbolPoint, CompareItem, KrExtras,
} from "../types";

// 주가차트 기간 버튼 — [라벨, 개월수]. summary 그래프와 동일(클라이언트 필터).
const PERIODS: [string, number][] = [
  ["1개월", 1], ["3개월", 3], ["6개월", 6], ["1년", 12], ["3년", 36], ["5년", 60], ["10년", 120],
];

// 지표 분류 — 유사 지표끼리 그룹핑(사용자 선택 판단 보조). key→그룹.
const IND_GROUP: Record<string, string> = {
  bb: "추세·이동평균", ema: "추세·이동평균", wma: "추세·이동평균", vwap: "추세·이동평균",
  envelope: "추세·이동평균", keltner: "추세·이동평균", donchian: "추세·이동평균", psar: "추세·이동평균",
  ichimoku: "추세·이동평균", supertrend: "추세·이동평균", dmi: "추세·이동평균", aroon: "추세·이동평균",
  vortex: "추세·이동평균", dpo: "추세·이동평균",
  rsi_14: "모멘텀·오실레이터", macd: "모멘텀·오실레이터", stoch: "모멘텀·오실레이터", cci: "모멘텀·오실레이터",
  williams_r: "모멘텀·오실레이터", roc: "모멘텀·오실레이터", momentum: "모멘텀·오실레이터", stochrsi: "모멘텀·오실레이터",
  trix: "모멘텀·오실레이터", uo: "모멘텀·오실레이터", ao: "모멘텀·오실레이터", ppo: "모멘텀·오실레이터",
  disparity: "모멘텀·오실레이터", psy: "모멘텀·오실레이터", kst: "모멘텀·오실레이터", coppock: "모멘텀·오실레이터",
  elder: "모멘텀·오실레이터",
  atr_14: "변동성", vol_20d: "변동성", bb_pctb: "변동성", bb_bw: "변동성", stddev: "변동성", mass_index: "변동성",
  volume: "거래량·자금흐름", obv: "거래량·자금흐름", mfi: "거래량·자금흐름", cmf: "거래량·자금흐름",
  chaikin_osc: "거래량·자금흐름", force_index: "거래량·자금흐름", eom: "거래량·자금흐름", vr: "거래량·자금흐름",
  ad_line: "거래량·자금흐름",
};
const IND_GROUP_ORDER = ["추세·이동평균", "모멘텀·오실레이터", "변동성", "거래량·자금흐름", "기타"];

// 한국식 시장 색 (상승=빨강 / 하락=파랑). 벤치마크=초록.
const UP = "#de3033", DOWN = "#1668c4", BENCH = "#15803d", ACCENT = "#c4982b";

// 이동평균 5종 — 모든(가격·거래량) 차트에 상시 표시.
const PRICE_MA: [keyof SymbolPoint, string, string][] = [
  ["ma5", "MA5", "#9aa0a6"], ["ma20", "MA20", "#c4982b"], ["ma60", "MA60", "#1668c4"],
  ["ma120", "MA120", "#7c3aed"], ["ma240", "MA240", "#b45309"],
];
const VOL_MA: [keyof SymbolPoint, string, string][] = [
  ["vma5", "MA5", "#9aa0a6"], ["vma20", "MA20", "#c4982b"], ["vma60", "MA60", "#1668c4"],
  ["vma120", "MA120", "#7c3aed"], ["vma240", "MA240", "#b45309"],
];

const DEFAULT_SEL = ["rsi_14", "macd", "volume", "stoch"];   // 최대 4개

// 다종목 비교 팔레트 (최대 10)
const CMP_COLORS = ["#de3033", "#1668c4", "#15803d", "#c4982b", "#7c3aed",
  "#b45309", "#0891b2", "#be185d", "#4d7c0f", "#9333ea"];

// 요약박스 — 현재가·베타는 고정, 나머지 2개는 아래 후보에서 선택.
const METRIC_OPTS: [string, string][] = [
  ["rsi", "RSI (14)"], ["range52", "52주 고/저"], ["volume", "거래량"],
  ["macd", "MACD"], ["stoch", "스토캐스틱"], ["atr", "ATR (14)"], ["vol20", "변동성 (20일)"],
];

// 서브차트 라인 색
const L1 = "#8a6a14", L2 = "#1668c4", L3 = "#7c3aed", L4 = "#22a06b", L5 = "#c4982b", GRAY = "#9aa0a6";

// 툴팁 글씨 크기 — 주가 차트(PriceTooltip)와 동일하게 12px로 통일.
const TIP_STYLE = { fontSize: 12, padding: "8px 11px", borderRadius: 8, lineHeight: 1.5 };
const TIP_LABEL = { fontSize: 12, fontWeight: 700, marginBottom: 5 };
const TIP_ITEM = { fontSize: 12, padding: "1px 0" };

// 지표 1개 = 렌더 설정. SubChart가 이 설정대로 일괄 렌더(라인/밴드/히스토 + 줌).
type RCSeries = { k: string; name: string; color: string; width?: number; dash?: string };
type RC = {
  priceOverlay?: boolean;                     // 종가선 + 가격 y축 (이동평균·밴드류)
  series?: RCSeries[];
  hist?: string;                              // 중립색 히스토그램 (macd_hist, ppo_hist)
  histSign?: string;                          // 부호색(빨강/파랑) 히스토그램 (ao)
  volume?: boolean;                           // 거래량 특수 렌더(전일비 색 + 거래량MA)
  domain?: [number, number] | ["auto", "auto"];
  ticks?: number[];
  refs?: { y: number; color: string }[];
};

const RENDER: Record<string, RC> = {
  // 기존 8
  rsi_14: { series: [{ k: "rsi_14", name: "RSI", color: L1, width: 1.5 }], domain: [0, 100], ticks: [30, 50, 70], refs: [{ y: 70, color: UP }, { y: 30, color: DOWN }] },
  macd: { series: [{ k: "macd", name: "MACD", color: L5, width: 1.5 }, { k: "macd_signal", name: "시그널", color: L2, width: 1 }], hist: "macd_hist", refs: [{ y: 0, color: GRAY }] },
  stoch: { series: [{ k: "stoch_k", name: "%K", color: L1, width: 1.5 }, { k: "stoch_d", name: "%D", color: L2, width: 1 }], domain: [0, 100], ticks: [20, 50, 80], refs: [{ y: 80, color: UP }, { y: 20, color: DOWN }] },
  volume: { volume: true },
  atr_14: { series: [{ k: "atr_14", name: "ATR", color: L3, width: 1.5 }] },
  obv: { series: [{ k: "obv", name: "OBV", color: L4, width: 1.5 }] },
  vol_20d: { series: [{ k: "vol_20d", name: "변동성%", color: L5, width: 1.5 }] },
  bb: { priceOverlay: true, series: [{ k: "bb_upper", name: "상단", color: L4, width: 1 }, { k: "bb_mid", name: "중심", color: GRAY, width: 1, dash: "3 3" }, { k: "bb_lower", name: "하단", color: L4, width: 1 }] },
  // 가격 오버레이
  ema: { priceOverlay: true, series: [{ k: "ema20", name: "EMA20", color: L5, width: 1.2 }, { k: "ema60", name: "EMA60", color: L2, width: 1 }] },
  wma: { priceOverlay: true, series: [{ k: "wma20", name: "WMA20", color: L5, width: 1.2 }] },
  vwap: { priceOverlay: true, series: [{ k: "vwap", name: "VWAP", color: L3, width: 1.2 }] },
  envelope: { priceOverlay: true, series: [{ k: "env_upper", name: "상단", color: L4, width: 1 }, { k: "env_lower", name: "하단", color: L4, width: 1 }] },
  keltner: { priceOverlay: true, series: [{ k: "kc_upper", name: "상단", color: L4, width: 1 }, { k: "kc_mid", name: "중심", color: GRAY, width: 1, dash: "3 3" }, { k: "kc_lower", name: "하단", color: L4, width: 1 }] },
  donchian: { priceOverlay: true, series: [{ k: "dc_upper", name: "상단", color: L4, width: 1 }, { k: "dc_mid", name: "중심", color: GRAY, width: 1, dash: "3 3" }, { k: "dc_lower", name: "하단", color: L4, width: 1 }] },
  psar: { priceOverlay: true, series: [{ k: "psar", name: "SAR", color: L3, width: 1, dash: "1 4" }] },
  ichimoku: { priceOverlay: true, series: [{ k: "ichi_tenkan", name: "전환", color: L5, width: 1 }, { k: "ichi_kijun", name: "기준", color: L2, width: 1 }, { k: "ichi_spanA", name: "선행A", color: L4, width: 1 }, { k: "ichi_spanB", name: "선행B", color: "#be185d", width: 1 }] },
  supertrend: { priceOverlay: true, series: [{ k: "supertrend", name: "슈퍼트렌드", color: L3, width: 1.4 }] },
  // 모멘텀·오실레이터
  cci: { series: [{ k: "cci", name: "CCI", color: L1, width: 1.5 }], refs: [{ y: 100, color: UP }, { y: -100, color: DOWN }, { y: 0, color: GRAY }] },
  williams_r: { series: [{ k: "williams_r", name: "%R", color: L1, width: 1.5 }], domain: [-100, 0], ticks: [-20, -50, -80], refs: [{ y: -20, color: UP }, { y: -80, color: DOWN }] },
  roc: { series: [{ k: "roc", name: "ROC%", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  momentum: { series: [{ k: "momentum", name: "모멘텀", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  stochrsi: { series: [{ k: "stochrsi_k", name: "%K", color: L1, width: 1.5 }, { k: "stochrsi_d", name: "%D", color: L2, width: 1 }], domain: [0, 100], ticks: [20, 50, 80], refs: [{ y: 80, color: UP }, { y: 20, color: DOWN }] },
  trix: { series: [{ k: "trix", name: "TRIX", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  uo: { series: [{ k: "uo", name: "UO", color: L1, width: 1.5 }], domain: [0, 100], ticks: [30, 50, 70], refs: [{ y: 70, color: UP }, { y: 30, color: DOWN }] },
  ao: { histSign: "ao", refs: [{ y: 0, color: GRAY }] },
  dmi: { series: [{ k: "plus_di", name: "+DI", color: UP, width: 1.2 }, { k: "minus_di", name: "-DI", color: DOWN, width: 1.2 }, { k: "adx", name: "ADX", color: L3, width: 1.5 }], refs: [{ y: 25, color: GRAY }] },
  aroon: { series: [{ k: "aroon_up", name: "Up", color: UP, width: 1.2 }, { k: "aroon_down", name: "Down", color: DOWN, width: 1.2 }], domain: [0, 100], ticks: [30, 70] },
  vortex: { series: [{ k: "vi_plus", name: "VI+", color: UP, width: 1.2 }, { k: "vi_minus", name: "VI-", color: DOWN, width: 1.2 }] },
  dpo: { series: [{ k: "dpo", name: "DPO", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  ppo: { series: [{ k: "ppo", name: "PPO", color: L5, width: 1.5 }, { k: "ppo_signal", name: "시그널", color: L2, width: 1 }], hist: "ppo_hist", refs: [{ y: 0, color: GRAY }] },
  disparity: { series: [{ k: "disparity", name: "이격도", color: L1, width: 1.5 }], refs: [{ y: 100, color: GRAY }] },
  psy: { series: [{ k: "psy_line", name: "심리도", color: L1, width: 1.5 }], domain: [0, 100], ticks: [25, 50, 75], refs: [{ y: 75, color: UP }, { y: 25, color: DOWN }] },
  kst: { series: [{ k: "kst", name: "KST", color: L5, width: 1.5 }, { k: "kst_signal", name: "시그널", color: L2, width: 1 }], refs: [{ y: 0, color: GRAY }] },
  coppock: { series: [{ k: "coppock", name: "코폭", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  elder: { series: [{ k: "bull_power", name: "불파워", color: UP, width: 1.2 }, { k: "bear_power", name: "베어파워", color: DOWN, width: 1.2 }], refs: [{ y: 0, color: GRAY }] },
  // 거래량
  mfi: { series: [{ k: "mfi", name: "MFI", color: L1, width: 1.5 }], domain: [0, 100], ticks: [20, 50, 80], refs: [{ y: 80, color: UP }, { y: 20, color: DOWN }] },
  cmf: { series: [{ k: "cmf", name: "CMF", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  chaikin_osc: { series: [{ k: "chaikin_osc", name: "Chaikin", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  force_index: { series: [{ k: "force_index", name: "Force", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  eom: { series: [{ k: "eom", name: "EOM", color: L1, width: 1.5 }], refs: [{ y: 0, color: GRAY }] },
  vr: { series: [{ k: "vr", name: "VR%", color: L1, width: 1.5 }], refs: [{ y: 150, color: UP }, { y: 70, color: DOWN }, { y: 100, color: GRAY }] },
  ad_line: { series: [{ k: "ad_line", name: "A/D", color: L4, width: 1.5 }] },
  // 변동성
  bb_pctb: { series: [{ k: "bb_pct_b", name: "%B", color: L1, width: 1.5 }], refs: [{ y: 1, color: UP }, { y: 0, color: DOWN }, { y: 0.5, color: GRAY }] },
  bb_bw: { series: [{ k: "bb_bw", name: "밴드폭%", color: L1, width: 1.5 }] },
  stddev: { series: [{ k: "stddev_20", name: "표준편차", color: L3, width: 1.5 }] },
  mass_index: { series: [{ k: "mass_index", name: "Mass", color: L1, width: 1.5 }], refs: [{ y: 27, color: UP }, { y: 26.5, color: DOWN }] },
};

// 신규 지표 설명(정의) — interpret()의 규칙기반 해석과 함께 표시.
const DEF: Record<string, string> = {
  ema: "지수이동평균(EMA)은 최근 가격에 가중치를 더 준 이동평균으로, 단순이평보다 추세 변화에 빠르게 반응합니다.",
  wma: "가중이동평균(WMA)은 최근일일수록 큰 가중치를 준 이동평균입니다.",
  vwap: "거래량가중평균가(VWAP)는 거래량을 가중한 평균 단가로, 기관 평균 매매가 기준선으로 쓰입니다.",
  envelope: "엔벨로프는 이동평균 ±일정%(여기선 5%) 밴드로, 상단 접근=과열·하단 접근=과냉을 봅니다.",
  keltner: "켈트너 채널은 EMA ±2×ATR 밴드로 변동성 기반 추세·돌파를 봅니다.",
  donchian: "돈치안 채널은 최근 N일 최고가·최저가 밴드로, 상단 돌파를 추세 시작 신호로 씁니다.",
  psar: "파라볼릭 SAR는 추세 방향의 손절·반전점을 표시합니다(가격이 SAR를 교차하면 추세 전환).",
  ichimoku: "일목균형표는 전환선·기준선·선행스팬(구름)으로 추세·지지저항을 한 번에 봅니다.",
  supertrend: "슈퍼트렌드는 ATR 기반 추세 추종선으로, 가격이 선 위면 상승·아래면 하락 추세입니다.",
  cci: "CCI는 가격이 평균에서 벗어난 정도로, +100 위=과열·강세, -100 아래=과냉·약세.",
  williams_r: "Williams %R은 최근 범위 내 종가 위치(0~-100)로, -20 위=과매수·-80 아래=과매도.",
  roc: "ROC(가격변화율)은 N일 전 대비 등락률(%)로 모멘텀 강도를 봅니다(0 위=상승 모멘텀).",
  momentum: "모멘텀은 N일 전 대비 가격 차이로, 0 위면 상승 모멘텀입니다.",
  stochrsi: "스토캐스틱 RSI는 RSI에 스토캐스틱을 적용해 더 민감하게 과매수(80↑)·과매도(20↓)를 봅니다.",
  trix: "TRIX는 삼중 지수이평의 변화율로 노이즈를 걸러낸 추세 모멘텀입니다(0 상향 돌파=매수).",
  uo: "얼티밋 오실레이터는 단·중·장기를 합친 모멘텀(0~100)으로, 30↓ 과매도·70↑ 과매수.",
  ao: "어썸 오실레이터는 5·34 중간가격 이평 차이로 모멘텀을 봅니다(0 위=강세).",
  dmi: "DMI/ADX는 +DI/-DI로 방향, ADX로 추세 강도를 봅니다(ADX 25 위=추세 강함).",
  aroon: "아룬은 최근 고점·저점까지의 시간으로 추세 시작·강도를 봅니다(Up>Down=상승).",
  vortex: "볼텍스(VI)는 VI+와 VI-의 교차로 추세 전환을 포착합니다(VI+>VI-=상승).",
  dpo: "DPO(추세제거)는 추세를 뺀 가격 순환만 보여 주기적 고저점을 찾습니다.",
  ppo: "PPO는 MACD를 %로 표현한 것으로 종목 간 비교가 쉽습니다(시그널 상향 돌파=매수).",
  disparity: "이격도는 현재가÷이동평균×100으로, 100 위=이평 위(과열 가능)·아래=과냉 가능.",
  psy: "투자심리도는 최근 12일 중 상승일 비율(%)로, 75↑ 과열·25↓ 침체.",
  kst: "KST는 여러 기간 ROC를 합친 장기 모멘텀으로, 시그널 상향 돌파를 매수로 봅니다.",
  coppock: "코폭 곡선은 장기 모멘텀 지표로, 0 상향 돌파를 중장기 매수 신호로 봅니다.",
  elder: "엘더레이는 불파워(고가-EMA)·베어파워(저가-EMA)로 매수·매도 세력을 봅니다.",
  mfi: "MFI(자금흐름)는 거래량을 반영한 RSI로, 80↑ 과매수·20↓ 과매도.",
  cmf: "CMF(차이킨 자금흐름)는 거래량 가중 매집/분산으로, 0 위=매집 우위.",
  chaikin_osc: "차이킨 오실레이터는 A/D선의 단·장기 이평 차이로 자금 흐름 변화를 봅니다.",
  force_index: "강도지수는 가격 변화×거래량으로 추세의 힘을 측정합니다(0 위=상승 압력).",
  eom: "이동편의도(EOM)는 적은 거래량으로 가격이 쉽게 오르내리는 정도로, 0 위=상승 용이.",
  vr: "거래량비율(VR)은 상승일/하락일 거래량 비(%)로, 보통 150 위 과열·70 아래 침체.",
  ad_line: "A/D 누적분배선은 종가 위치×거래량 누적으로, 가격과 같이 오르면 매집(추세 신뢰).",
  bb_pctb: "볼린저 %B는 밴드 내 종가 위치로, 1 위=상단 돌파(과열)·0 아래=하단 이탈(과냉).",
  bb_bw: "볼린저 밴드폭은 밴드 폭(변동성)으로, 좁아지면(스퀴즈) 큰 변동 임박 신호.",
  stddev: "표준편차는 최근 N일 가격 변동성의 크기입니다(클수록 변동성↑).",
  mass_index: "매스 인덱스는 고저 범위 확장으로 추세 반전(반전 벌지, 27 위→26.5 아래)을 예고합니다.",
};

// 급등락 세모 마커 (위=급등 빨강 / 아래=급락 파랑)
function TriUp({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy - 13} L ${cx - 6} ${cy - 5} L ${cx + 6} ${cy - 5} Z`}
    fill={UP} stroke="#fff" strokeWidth={0.6} />;
}
function TriDown({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy + 13} L ${cx - 6} ${cy + 5} L ${cx + 6} ${cy + 5} Z`}
    fill={DOWN} stroke="#fff" strokeWidth={0.6} />;
}

// 캔들(틱) — recharts엔 캔들이 없어 [low,high] 플로팅 바의 픽셀 좌표(y/height)에서
// open/close 픽셀을 선형 보간해 직접 그린다. 상승(종가≥시가)=빨강 / 하락=파랑.
function Candle(props: {
  x?: number; y?: number; width?: number; height?: number;
  payload?: SymbolPoint;
}) {
  const { x, y, width, height, payload } = props;
  if (x == null || y == null || width == null || height == null || !payload) return null;
  const { open, high, low, close } = payload;
  if (open == null || high == null || low == null || close == null) return null;
  const color = close >= open ? UP : DOWN;
  const cx = x + width / 2;
  const span = high - low;
  const pxPer = span ? height / span : 0;
  const yOpen = y + (high - open) * pxPer;
  const yClose = y + (high - close) * pxPer;
  const bodyTop = Math.min(yOpen, yClose);
  const bodyH = Math.max(Math.abs(yClose - yOpen), 1);
  const bw = Math.max(Math.min(width * 0.62, 10), 2);
  return (
    <g>
      <line x1={cx} y1={y} x2={cx} y2={y + height} stroke={color} strokeWidth={1} />
      <rect x={cx - bw / 2} y={bodyTop} width={bw} height={bodyH} fill={color} />
    </g>
  );
}

// 커서 드래그로 구간 확대 — 카테고리(날짜) X축을 가진 차트에 재사용.
// 마우스 다운→무브로 두 날짜를 잡고, 업 시 그 구간으로 데이터를 필터(ISO 날짜라 문자열 비교=시간순).
type DragArea = { x1: string; x2: string } | null;
type ChartEvt = { activeLabel?: string | number } | null;
function useDragZoom<T extends { date: string }>(data: T[]) {
  const [range, setRange] = useState<[string, string] | null>(null);
  const [refL, setRefL] = useState<string | null>(null);
  const [refR, setRefR] = useState<string | null>(null);
  const view = range ? data.filter((d) => d.date >= range[0] && d.date <= range[1]) : data;
  const dragProps = {
    onMouseDown: (e: ChartEvt) => { if (e?.activeLabel != null) { const l = String(e.activeLabel); setRefL(l); setRefR(l); } },
    onMouseMove: (e: ChartEvt) => { if (refL != null && e?.activeLabel != null) setRefR(String(e.activeLabel)); },
    onMouseUp: () => {
      if (refL != null && refR != null && refL !== refR) setRange(refL < refR ? [refL, refR] : [refR, refL]);
      setRefL(null); setRefR(null);
    },
  };
  const area: DragArea = refL != null && refR != null && refL !== refR ? { x1: refL, x2: refR } : null;
  // 휠(스크롤) 줌 — 차트 위 휠 ↑=확대 / ↓=축소(커서 위치 기준). 드래그 줌과 같은 range 상태 공유.
  // recharts는 휠 줌 미지원 → 컨테이너 div에 네이티브 wheel 리스너(passive:false)로 직접 구현.
  const dataRef = useRef(data); dataRef.current = data;
  const wheelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = wheelRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const d = dataRef.current;
      if (d.length < 4) return;
      e.preventDefault();                       // 페이지 스크롤 대신 차트 줌
      const rect = el.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      setRange((cur) => {
        let i0 = 0, i1 = d.length - 1;
        if (cur) {
          const f = d.findIndex((x) => x.date >= cur[0]);
          i0 = f < 0 ? 0 : f;
          for (let j = d.length - 1; j >= 0; j--) { if (d[j].date <= cur[1]) { i1 = j; break; } }
        }
        const w = i1 - i0;
        const anchor = i0 + frac * w;            // 커서가 가리키는 데이터 위치 고정
        const nw = Math.min(d.length - 1, Math.max(8, Math.round(w * (e.deltaY < 0 ? 0.8 : 1.25))));
        let ns = Math.round(anchor - frac * nw);
        ns = Math.min(d.length - 1 - nw, Math.max(0, ns));
        const ne = ns + nw;
        if (ns <= 0 && ne >= d.length - 1) return null;   // 전체 범위 → 줌 해제
        return [d[ns].date, d[ne].date];
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  return { view, dragProps, area, wheelRef, range,
    setWindow: (a: string, b: string) => setRange([a, b]),   // 기간 버튼·날짜 입력으로 구간 지정
    reset: () => setRange(null), zoomed: range != null };
}

export default function StockDashboard({ symbol, hideSearch }: { symbol?: string; hideSearch?: boolean } = {}) {
  const [symbols, setSymbols] = useState<string[]>(symbol ? [symbol] : ["005930"]);
  // HOME 임베드 — 외부 선택 종목을 따라감. symbol이 바뀌면 단일 종목 모드로 차트까지 재로딩.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => {
    if (!symbol) return;
    setSymbols([symbol]);
    loadFor([symbol], range);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);
  const [input, setInput] = useState("");
  const [range, setRange] = useState("10y");   // 넉넉히 받아 차트 기간/날짜는 클라이언트 필터(summary식)
  const [chartPeriod, setChartPeriod] = useState<number | "all" | "custom">(12);  // 차트 기간 버튼 활성표시
  const [data, setData] = useState<SymbolDetail | null>(null);
  const [cmp, setCmp] = useState<CompareItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [listings, setListings] = useState<SymbolListing[]>([]);
  const [selected, setSelected] = useState<string[]>(DEFAULT_SEL);
  const [focused, setFocused] = useState(false);
  const [recent, setRecent] = useState<string[]>(() => {   // 과거 검색 기록(localStorage)
    try { return JSON.parse(localStorage.getItem("ca_recent") || "[]"); } catch { return []; }
  });
  const [box3, setBox3] = useState("rsi");
  const [box4, setBox4] = useState("range52");
  const [hidden, setHidden] = useState<Record<string, boolean>>({});  // 범례 클릭 토글
  const [kr, setKr] = useState<KrExtras | null>(null);   // 한국 종목 부가 데이터
  const [subchartsOpen, setSubchartsOpen] = useState(true);  // 보조지표 4분할 전체 접기

  const nameMap = new Map(listings.map((l) => [l.symbol, l.name]));
  const nameOf = (s: string) => nameMap.get(s) || s;
  const compareMode = symbols.length >= 2;

  async function loadFor(syms: string[], rng: string) {
    if (syms.length === 0) return;
    setBusy(true); setErr("");
    try {
      if (syms.length === 1) {
        const d = await api.symbolDetail(syms[0], rng);
        setData(d); setCmp(null);
      } else {
        const r = await api.marketCompare(syms, rng);
        setCmp(r.items); setData(null);
      }
      setRange(rng);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    if (!symbol) loadFor(["005930"], "10y");   // 독립 사용 시 기본 종목(임베드 시엔 symbol effect가 로딩)
    api.marketListings().then((r) => setListings(r.listings)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 한국 종목(6자리) 단일 선택 시 부가 데이터 — 기간과 무관하므로 종목만 의존.
  // 실패해도 차트엔 영향 없음(부가 패널만 비표시). 서버가 일자별 캐시.
  const singleSym = symbols.length === 1 ? symbols[0] : null;
  useEffect(() => {
    setKr(null);
    if (!singleSym || !/^\d{6}$/.test(singleSym)) return;
    let alive = true;
    api.krExtras(singleSym)
      .then((x) => { if (alive) setKr(x); })
      .catch(() => { if (alive) setKr(null); });
    return () => { alive = false; };
  }, [singleSym]);

  // 종목 추가/제거 (최대 10) — 부수효과는 updater 밖에서(StrictMode 이중호출 방지)
  function addSymbol(sym: string) {
    const s = sym.trim().toUpperCase();
    if (!s || symbols.includes(s) || symbols.length >= 10) { setInput(""); return; }
    setInput(""); setFocused(false);
    setRecent((r) => {                       // 검색 기록 갱신(최신순, 최대 8, 영속)
      const nxt = [s, ...r.filter((x) => x !== s)].slice(0, 8);
      try { localStorage.setItem("ca_recent", JSON.stringify(nxt)); } catch { /* noop */ }
      return nxt;
    });
    const next = [...symbols, s];
    setSymbols(next);
    loadFor(next, range);
  }
  function removeSymbol(sym: string) {
    const next = symbols.filter((s) => s !== sym);
    if (!next.length) return;
    setSymbols(next);
    loadFor(next, range);
  }

  // 검색 드롭다운 — 입력 있으면 코드/이름 필터, 없으면 가나다순 전체 목록
  const sortedListings = useMemo(
    () => [...listings].sort((a, b) => a.name.localeCompare(b.name, "ko")),
    [listings]);
  const q = input.trim().toUpperCase();
  const matches = (q
    ? sortedListings.filter((l) => l.symbol.includes(q) || l.name.toUpperCase().includes(q))
    : sortedListings
  ).slice(0, 60);
  // 과거 검색 기록 → 목록 항목 (입력 없을 때 상단 노출)
  const recentItems = recent
    .map((sym) => listings.find((l) => l.symbol === sym))
    .filter((l): l is SymbolListing => !!l);
  const searchRow = (l: SymbolListing, kp = "") => (
    <li key={kp + l.symbol} onMouseDown={() => addSymbol(l.symbol)}
      style={{ padding: "7px 12px", cursor: "pointer", fontSize: 13,
        display: "flex", justifyContent: "space-between", gap: 8 }}
      onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(79,143,245,0.12)")}
      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
      <span>{l.name}</span>
      <span style={{ color: "var(--muted)" }}>{l.symbol} · {l.market}</span>
    </li>
  );

  const indicators = data?.indicators || [];
  function toggle(key: string) {
    setSelected((s) => s.includes(key) ? s.filter((k) => k !== key)
      : s.length < 4 ? [...s, key] : s);
  }

  const isKR = data?.currency === "KRW";
  const fmtP = (v: number | null | undefined) =>
    v == null ? "—" : isKR ? Math.round(v).toLocaleString()
      : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const won = isKR ? "원" : "", dol = isKR ? "" : "$";
  const last = data?.last;
  const chgColor = last?.change_pct != null ? (last.change_pct >= 0 ? UP : DOWN) : "var(--muted)";

  // 단일 종목 차트 데이터 + 캔들/급등락 컬럼
  const chartData = (data?.series || []).map((p, i, arr) => ({
    ...p,
    hl: p.low != null && p.high != null ? [p.low, p.high] : null,
    up_spike: p.chg_pct != null && p.chg_pct >= 10 ? p.high : null,
    down_spike: p.chg_pct != null && p.chg_pct <= -10 ? p.low : null,
    vol_chg: i > 0 && arr[i - 1].volume && p.volume != null
      ? (p.volume - (arr[i - 1].volume as number)) / (arr[i - 1].volume as number) * 100 : null,
  }));

  // 커서 드래그 줌 — 주가차트·보조지표가 같은 X축(날짜)을 공유하므로 하나의 줌 상태로 묶는다.
  const pz = useDragZoom(chartData);
  // 차트 기간 — summary식: 10년 받아 기간 버튼/직접 날짜로 구간 지정(클라이언트 필터).
  const cdMin = chartData[0]?.date || "", cdMax = chartData[chartData.length - 1]?.date || "";
  const monthsBack = (m: number) => {
    if (!cdMax) return cdMin;
    const d = new Date(cdMax); d.setMonth(d.getMonth() - m);
    const s = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return s < cdMin ? cdMin : s;
  };
  // 데이터 로드/종목 변경 시 기본 보기 = 최근 1년
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (cdMax) { setChartPeriod(12); pz.setWindow(monthsBack(12), cdMax); } }, [cdMax]);

  // 가격축 도메인 — 줌된 구간(pz.view) 기준으로 타이트하게(0 강제 방지).
  // 긴 구간(수천 일)에서 spread 인자 한계를 피하려고 reduce로 min/max 계산.
  let pmin = Infinity, pmax = -Infinity;
  pz.view.forEach((d) => {
    ([d.low, d.high, d.bench, d.ma5, d.ma20, d.ma60, d.ma120, d.ma240] as (number | null)[])
      .forEach((v) => { if (v != null) { if (v < pmin) pmin = v; if (v > pmax) pmax = v; } });
  });
  const priceDomain: [number, number] = pmin <= pmax
    ? [Math.floor(pmin * 0.985), Math.ceil(pmax * 1.015)] : [0, 1];

  return (
    <div className="dashboard-fullwidth">
      {!hideSearch && (
      <p style={{ color: "var(--muted)", marginTop: 0, fontSize: 13 }}>
        한국·미국 개별 종목·ETF·ETN을 캔들·지표로 분석합니다. <b>종목명 또는 코드</b>로 검색하고,
        벤치마크(코스피/코스닥/나스닥) 초록 점선과 비교하거나 <b>최대 10종목까지 수익률 비교</b>가 가능합니다.
      </p>
      )}

      {/* 검색 + 선택 종목 칩 (임베드 시 숨김 — 기간 선택은 주가차트 헤더로 이동) */}
      {!hideSearch && (
      <div className="panel" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", width: 320 }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setTimeout(() => setFocused(false), 150)}
            onKeyDown={(e) => { if (e.key === "Enter" && matches[0]) addSymbol(matches[0].symbol); }}
            placeholder="종목명·코드 검색 후 추가 (삼성전자 / 005930 / AAPL)"
            aria-label="종목 검색"
            style={{ width: "100%" }}
          />
          {focused && (matches.length > 0 || (!q && recentItems.length > 0)) && (
            <ul style={{
              position: "absolute", top: "100%", left: 0, right: 0, zIndex: 20,
              margin: 0, padding: 0, listStyle: "none", maxHeight: 320, overflowY: "auto",
              background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 8,
              boxShadow: "0 6px 20px rgba(0,0,0,0.35)",
            }}>
              {!q && recentItems.length > 0 && (
                <>
                  <li style={{ padding: "6px 12px", fontSize: 11, fontWeight: 700,
                    color: "var(--muted)", letterSpacing: "0.02em" }}>최근 검색</li>
                  {recentItems.map((l) => searchRow(l, "r-"))}
                  <li style={{ borderTop: "1px solid var(--border,#e3e8ef)" }} />
                  <li style={{ padding: "6px 12px", fontSize: 11, fontWeight: 700,
                    color: "var(--muted)", letterSpacing: "0.02em" }}>전체 종목 (가나다순)</li>
                </>
              )}
              {matches.map((l) => searchRow(l))}
            </ul>
          )}
        </div>
        {/* 선택 종목 칩 */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", width: "100%" }}>
          {symbols.map((s, i) => (
            <span key={s} className="ca-symchip">
              <span style={{ width: 9, height: 9, borderRadius: 2, flexShrink: 0,
                background: compareMode ? CMP_COLORS[i % CMP_COLORS.length] : "#4f8ff5" }} />
              {nameOf(s)} <span style={{ color: "var(--muted)" }}>{s}</span>
              {symbols.length > 1 && (
                <button className="x-btn" aria-label={`${nameOf(s)} 제거`}
                  onClick={() => removeSymbol(s)}>✕</button>
              )}
            </span>
          ))}
          <span style={{ fontSize: 12, color: "var(--muted)", alignSelf: "center" }}>
            {busy ? "조회 중…" : compareMode ? `${symbols.length}/10 비교 중 — 검색해서 추가`
              : "검색해서 종목을 추가하면 비교 모드"}
          </span>
        </div>
      </div>
      )}

      {err && <div className="error" style={{ marginTop: 12 }}>{err}</div>}

      {/* ── 다종목 비교 모드 ── */}
      {compareMode && cmp && (
        <CompareChart items={cmp} />
      )}

      {/* ── 단일 종목 모드 ── */}
      {!compareMode && data && last && (
        <>
          {/* 요약 박스 — 현재가·베타 고정 + 사용자 선택 2개 */}
          <div className="ca-summary">
            <Card title={`${nameOf(data.symbol)} 현재가`}>
              <div style={{ fontSize: 22, fontWeight: 700 }}>{dol}{fmtP(last.close)}{won}</div>
              <div style={{ color: chgColor, fontWeight: 600 }}>
                {last.change_pct != null && last.change_pct >= 0 ? "▲" : "▼"} {last.change_pct?.toFixed(2)}%
              </div>
            </Card>
            <Card title={`베타 (β) vs ${last.benchmark}`}>
              <div style={{ fontSize: 22, fontWeight: 700,
                color: last.beta == null ? "var(--muted)" : last.beta > 1 ? UP : last.beta < 1 ? DOWN : "inherit" }}>
                {last.beta ?? "—"}
              </div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>
                {last.benchmark} 1.00 · {last.beta == null ? "" : last.beta > 1 ? "변동 더 큼" : "변동 더 작음"}
              </div>
            </Card>
            <MetricCard sel={box3} onSel={setBox3} last={last} fmtP={fmtP} dol={dol} won={won} />
            <MetricCard sel={box4} onSel={setBox4} last={last} fmtP={fmtP} dol={dol} won={won} />
          </div>

          {/* 지표 선택 (최대 4) — 유사 지표끼리 그룹핑 + 본문 12pt */}
          <div className="panel" style={{ marginTop: 16 }}>
            <details open>
              <summary style={{ cursor: "pointer", fontWeight: 700, fontSize: "12pt" }}>
                지표 선택 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}>(최대 4개 · 유사 지표끼리 분류)</span>
              </summary>
              {IND_GROUP_ORDER.map((g) => {
                const items = indicators.filter((i) => (IND_GROUP[i.key] || "기타") === g);
                if (!items.length) return null;
                return (
                  <div key={g} style={{ marginTop: 12 }}>
                    <div style={{ fontSize: "12pt", fontWeight: 700, color: "var(--accent)", margin: "2px 0 6px" }}>{g}</div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))", gap: 6 }}>
                      {items.map((i) => {
                        const on = selected.includes(i.key);
                        const dis = !on && selected.length >= 4;
                        return (
                          <label key={i.key} style={{ fontSize: "12pt", opacity: dis ? 0.4 : 1,
                            cursor: dis ? "not-allowed" : "pointer", display: "flex", alignItems: "center", gap: 6 }}>
                            <input type="checkbox" checked={on} disabled={dis} onChange={() => toggle(i.key)} />
                            {i.label}
                          </label>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </details>
          </div>

          {/* 주가 캔들차트 (전체폭) — 캔들 + MA5종 + 벤치마크 점선 + 급등락 · 커서 드래그 줌 */}
          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0, display: "flex", justifyContent: "space-between",
              alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>주가 추이 (캔들) · 이동평균 · 벤치마크({last.benchmark}) · 급등락(±10%)</span>
              <span style={{ fontSize: 11, fontWeight: 400, color: "var(--muted)", whiteSpace: "nowrap" }}>
                {pz.zoomed
                  ? <button type="button" className="ghost sm" onClick={pz.reset}
                      style={{ fontSize: 11, padding: "2px 8px" }}>↺ 줌 초기화</button>
                  : "그래프를 드래그하면 확대"}
              </span>
            </h3>
            {/* 기간 버튼 + 직접 날짜 설정 — summary 그래프와 동일(클라이언트 필터) */}
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 10, alignItems: "center" }}>
              {PERIODS.map(([lbl, m]) => {
                const on = chartPeriod === m;
                return (
                  <button key={lbl} type="button" className="ghost sm"
                    onClick={() => { setChartPeriod(m); pz.setWindow(monthsBack(m), cdMax); }}
                    style={{ fontSize: 12, padding: "3px 11px",
                      background: on ? "rgba(79,143,245,0.16)" : undefined,
                      fontWeight: on ? 700 : 400, color: on ? "#1668c4" : undefined }}>{lbl}</button>
                );
              })}
              <button type="button" className="ghost sm" onClick={() => { setChartPeriod("all"); pz.reset(); }}
                style={{ fontSize: 12, padding: "3px 11px",
                  background: chartPeriod === "all" ? "rgba(79,143,245,0.16)" : undefined,
                  fontWeight: chartPeriod === "all" ? 700 : 400, color: chartPeriod === "all" ? "#1668c4" : undefined }}>전체</button>
              <span style={{ color: "var(--muted)", fontSize: 12, margin: "0 2px" }}>|</span>
              <input type="date" value={pz.range?.[0] || cdMin} min={cdMin} max={cdMax}
                onChange={(e) => { setChartPeriod("custom"); pz.setWindow(e.target.value, pz.range?.[1] || cdMax); }}
                style={{ fontSize: 12, padding: "2px 6px" }} aria-label="시작일" />
              <span style={{ color: "var(--muted)", fontSize: 12 }}>~</span>
              <input type="date" value={pz.range?.[1] || cdMax} min={cdMin} max={cdMax}
                onChange={(e) => { setChartPeriod("custom"); pz.setWindow(pz.range?.[0] || cdMin, e.target.value); }}
                style={{ fontSize: 12, padding: "2px 6px" }} aria-label="종료일" />
            </div>
            <div ref={pz.wheelRef}>

            <ResponsiveContainer width="100%" height={420}>
              <ComposedChart data={pz.view} margin={{ top: 5, right: 16, bottom: 5, left: 8 }} {...pz.dragProps}>
                <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis domain={priceDomain} tick={{ fontSize: 11 }} width={56}
                  tickFormatter={(v) => isKR ? `${Math.round(v / 1000)}k` : String(v)} />
                <Tooltip content={({ active, payload }) => (
                  <PriceTooltip active={active}
                    payload={payload as readonly PriceTipEntry[] | undefined}
                    isKR={isKR} dol={dol} won={won} benchName={last.benchmark} />
                )} />
                <Legend
                  verticalAlign="bottom"
                  wrapperStyle={{ fontSize: 11, paddingTop: 10 }}
                  onClick={(e) => { const k = (e as { dataKey?: string }).dataKey; if (k) setHidden((h) => ({ ...h, [k]: !h[k] })); }}
                  formatter={(value, entry) => {
                    const k = (entry as { dataKey?: string })?.dataKey;
                    const off = !!(k && hidden[k]);
                    return <span style={{ cursor: "pointer", color: off ? "var(--muted)" : "inherit",
                      textDecoration: off ? "line-through" : "none" }}>{value}</span>;
                  }} />
                <Bar dataKey="hl" isAnimationActive={false} legendType="none" shape={Candle} />
                {PRICE_MA.map(([k, lbl, col]) => (
                  <Line key={k} type="monotone" dataKey={k} stroke={col} strokeWidth={1} dot={false} name={lbl} hide={!!hidden[k]} />
                ))}
                {/* 볼린저밴드 — 주가에 오버레이. Legend에서 클릭으로 켜고 끔 */}
                <Line type="monotone" dataKey="bb_upper" stroke="#22a06b" strokeWidth={1} dot={false} name="볼린저 상단" hide={!!hidden["bb_upper"]} connectNulls />
                <Line type="monotone" dataKey="bb_mid" stroke="#9aa0a6" strokeWidth={1} strokeDasharray="3 3" dot={false} name="볼린저 중심" hide={!!hidden["bb_mid"]} connectNulls />
                <Line type="monotone" dataKey="bb_lower" stroke="#22a06b" strokeWidth={1} dot={false} name="볼린저 하단" hide={!!hidden["bb_lower"]} connectNulls />
                <Line type="monotone" dataKey="bench" stroke={BENCH} strokeWidth={1.4}
                  strokeDasharray="6 4" dot={false} name={`${last.benchmark}(벤치마크)`} connectNulls hide={!!hidden["bench"]} />
                <Scatter dataKey="up_spike" shape={<TriUp />} name="급등 +10%" legendType="triangle" fill={UP} hide={!!hidden["up_spike"]} />
                <Scatter dataKey="down_spike" shape={<TriDown />} name="급락 −10%" legendType="triangle" fill={DOWN} hide={!!hidden["down_spike"]} />
                {pz.area && <ReferenceArea x1={pz.area.x1} x2={pz.area.x2} fill="#4f8ff5" fillOpacity={0.15} strokeOpacity={0.3} />}
              </ComposedChart>
            </ResponsiveContainer>
            </div>
            <ExplToggle ikey="price" series={data.series} isKR={isKR} dol={dol} won={won} benchName={last.benchmark} />
          </div>

          {/* 보조지표 4분할 — 최상단 버튼 하나로 전체 접기/펼치기 */}
          {selected.length > 0 ? (
            <div style={{ marginTop: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                marginBottom: subchartsOpen ? 12 : 0 }}>
                <h3 style={{ margin: 0 }}>보조지표 ({selected.length})</h3>
                <button type="button" className="ghost sm"
                  onClick={() => setSubchartsOpen((o) => !o)} aria-expanded={subchartsOpen}
                  style={{ fontSize: 12, padding: "3px 12px" }}>
                  {subchartsOpen ? "▾ 지표 접기" : "▸ 지표 펼치기"}
                </button>
              </div>
              {subchartsOpen && (
                <div className="ca-grid">
                  {selected.map((k) => (
                    <div key={k} className="panel" style={{ marginBottom: 0 }}>
                      <SubChart ikey={k} label={indicators.find((i) => i.key === k)?.label || k}
                        data={pz.view} isKR={isKR} dragProps={pz.dragProps} area={pz.area} />
                      <ExplToggle ikey={k} series={data.series} isKR={isKR} dol={dol} won={won} benchName={last.benchmark} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <p style={{ color: "var(--muted)", marginTop: 12, fontSize: 13 }}>
              위 “지표 선택”에서 RSI·MACD·거래량 등을 고르면 여기에 4분할로 표시됩니다.
            </p>
          )}

          {/* ── 한국 종목 부가 데이터 (네이버·FnGuide·DART) ── */}
          {isKR && kr && <KrSections kr={kr} close={last.close} />}
        </>
      )}
    </div>
  );
}

// ── 한국 종목 부가 데이터 — 투자자별·공매도·컨센서스 ──────────
// 추정실적·애널리스트 리포트·최근 공시(DART)는 Valuation/Consensus 등 다른 탭과 중복이라 제거.
function KrSections({ kr, close }: { kr: KrExtras; close: number | null }) {
  const { investor, consensus, shorting } = kr;
  const [invWin, setInvWin] = useState(20);   // 투자자별 집계 창 (1/5/20/60/120일)
  // 투자자별 — 차트는 오래된→최근(좌→우)이라 역순. investor 원본은 최신우선.
  const invData = [...investor].reverse().map((p) => ({
    date: p.date.slice(5), 기관: p.inst, 외국인: p.foreign, 개인: p.indiv,
  }));

  return (
    <>
      {/* 투자자별 순매매 — 1/5/20/60/120일 창 전환 */}
      {investor.length > 0 && (() => {
        const wins = [1, 5, 20, 60, 120].filter((w) => w <= investor.length);
        const win = Math.min(invWin, investor.length);
        const recent = investor.slice(0, win);   // 최신우선 → 최근 win일
        const sums = {
          기관: recent.reduce((s, p) => s + p.inst, 0),
          외국인: recent.reduce((s, p) => s + p.foreign, 0),
          개인: recent.reduce((s, p) => s + p.indiv, 0),
        };
        return (
          <div className="panel" style={{ marginTop: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
              <h3 style={{ margin: 0 }}>투자자별 순매매 (단위 주)</h3>
              <div style={{ display: "flex", gap: 4 }}>
                {wins.map((w) => (
                  <button key={w} type="button" className="ghost sm" onClick={() => setInvWin(w)}
                    style={{ fontSize: 12, padding: "3px 10px",
                      background: invWin === w ? "rgba(79,143,245,0.16)" : undefined,
                      fontWeight: invWin === w ? 700 : 400,
                      color: invWin === w ? "#1668c4" : undefined }}>
                    {w}일
                  </button>
                ))}
              </div>
            </div>
            <div style={{ display: "flex", gap: 20, margin: "12px 0 4px", flexWrap: "wrap" }}>
              {(["기관", "외국인", "개인"] as const).map((lbl) => {
                const v = sums[lbl];
                return (
                  <div key={lbl}>
                    <div style={{ fontSize: 12, color: "var(--muted)" }}>{lbl} · 최근 {win}일 누적</div>
                    <div style={{ fontSize: 14, fontWeight: 700, color: v >= 0 ? UP : DOWN }}>
                      {v >= 0 ? "+" : ""}{v.toLocaleString()}주
                    </div>
                  </div>
                );
              })}
            </div>
            <ResponsiveContainer width="100%" height={260}>
              <ComposedChart data={invData.slice(-win)} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
                <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={win > 60 ? 24 : 12} />
                <YAxis tick={{ fontSize: 11 }} width={56}
                  tickFormatter={(v) => `${Math.round(Number(v) / 10000)}만`} />
                <Tooltip formatter={(v) => Number(v).toLocaleString() + "주"}
                  contentStyle={TIP_STYLE} labelStyle={TIP_LABEL} itemStyle={TIP_ITEM} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <ReferenceLine y={0} stroke="#94a3b8" />
                <Bar dataKey="기관" fill={ACCENT} />
                <Bar dataKey="외국인" fill={DOWN} />
                <Bar dataKey="개인" fill={GRAY} />
              </ComposedChart>
            </ResponsiveContainer>
            <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
              양(+)은 순매수, 음(−)은 순매도. 위 버튼으로 1·5·20·60·120일 누적을 전환합니다.
              개인은 기관·외국인 합의 반대값으로 근사. 출처: 네이버 금융
            </p>
          </div>
        );
      })()}

      {/* 공매도 잔고 (KRX) — 배포 환경에서 활성 */}
      {shorting.length > 0 && (() => {
        const sData = [...shorting].reverse().map((p) => ({
          date: p.date.slice(5), 잔고비중: p.bal_ratio,
        }));
        const latest = shorting[0];
        return (
          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>공매도 잔고</h3>
            <div style={{ display: "flex", gap: 20, margin: "4px 0 8px", flexWrap: "wrap" }}>
              <div>
                <div style={{ fontSize: 12, color: "var(--muted)" }}>최근 잔고수량</div>
                <div style={{ fontSize: 18, fontWeight: 700 }}>
                  {latest.bal_qty != null ? latest.bal_qty.toLocaleString() + "주" : "—"}
                </div>
              </div>
              <div>
                <div style={{ fontSize: 12, color: "var(--muted)" }}>잔고비중</div>
                <div style={{ fontSize: 18, fontWeight: 700, color: DOWN }}>
                  {latest.bal_ratio != null ? latest.bal_ratio.toFixed(2) + "%" : "—"}
                </div>
              </div>
            </div>
            <ResponsiveContainer width="100%" height={240}>
              <ComposedChart data={sData} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
                <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis tick={{ fontSize: 11 }} width={44} tickFormatter={(v) => `${v}%`} />
                <Tooltip formatter={(v) => `${Number(v).toFixed(2)}%`}
                  contentStyle={TIP_STYLE} labelStyle={TIP_LABEL} itemStyle={TIP_ITEM} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line type="monotone" dataKey="잔고비중" stroke={DOWN} strokeWidth={1.6} dot={false} name="잔고비중(%)" connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
            <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
              잔고비중 = 공매도 잔고수량 / 상장주식수. 출처: KRX
            </p>
          </div>
        );
      })()}

      {/* 컨센서스 — 증권사별 목표주가·투자의견 */}
      {consensus.length > 0 && (
        <div className="panel" style={{ marginTop: 16, overflowX: "auto" }}>
          <h3 style={{ marginTop: 0 }}>애널리스트 컨센서스 (목표주가·투자의견)</h3>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "2px solid #e3e8ef", textAlign: "right" }}>
                <th style={{ textAlign: "left", padding: "7px 6px" }}>증권사</th>
                <th style={{ padding: "7px 6px" }}>투자의견</th>
                <th style={{ padding: "7px 6px" }}>목표주가</th>
                <th style={{ padding: "7px 6px" }}>현재가 대비</th>
                <th style={{ padding: "7px 6px" }}>최종일</th>
              </tr>
            </thead>
            <tbody>
              {consensus.map((c, i) => {
                const up = close && c.target ? (c.target - close) / close * 100 : null;
                return (
                  <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "6px", fontWeight: 600 }}>{c.broker}</td>
                    <td style={{ padding: "6px", textAlign: "right" }}>{c.opinion || "—"}</td>
                    <td style={{ padding: "6px", textAlign: "right" }}>
                      {c.target != null ? c.target.toLocaleString() + "원" : "—"}
                    </td>
                    <td style={{ padding: "6px", textAlign: "right",
                      color: up == null ? "var(--muted)" : up >= 0 ? UP : DOWN }}>
                      {up == null ? "—" : `${up >= 0 ? "+" : ""}${up.toFixed(1)}%`}
                    </td>
                    <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{c.date}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
            “현재가 대비”는 (목표주가 − 현재가)/현재가. 출처: 네이버 금융 컨센서스
          </p>
        </div>
      )}

    </>
  );
}

// ── 다종목 비교 — 종목별 소형 캔들차트(small-multiples) ──────────────────────────
function CompareChart({ items }: { items: CompareItem[] }) {
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h3 style={{ marginTop: 0 }}>
        다종목 캔들 비교
        <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 400 }}>
          {"  "}· 종목별 개별 캔들차트(MA 5·20·60) · 비교 모드에선 지표는 표시하지 않습니다
        </span>
      </h3>
      <div className="ca-grid">
        {items.map((it) => <CompareCell key={it.symbol} item={it} />)}
      </div>
    </div>
  );
}

function CompareCell({ item }: { item: CompareItem }) {
  const isKR = item.currency === "KRW";
  const data = item.series.map((p) => ({
    ...p, hl: p.low != null && p.high != null ? [p.low, p.high] : null,
  }));
  const cz = useDragZoom(data);
  let pmin = Infinity, pmax = -Infinity;
  cz.view.forEach((d) => {
    ([d.low, d.high, d.ma20, d.ma60] as (number | null)[]).forEach((v) => {
      if (v != null) { if (v < pmin) pmin = v; if (v > pmax) pmax = v; }
    });
  });
  const domain: [number, number] = pmin <= pmax
    ? [Math.floor(pmin * 0.985), Math.ceil(pmax * 1.015)] : [0, 1];
  return (
    <div className="panel" style={{ marginBottom: 0 }}>
      <h3 style={{ marginTop: 0, fontSize: 14, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
        <span>{item.name} <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>{item.symbol}</span></span>
        {cz.zoomed && <button type="button" className="ghost sm" onClick={cz.reset}
          style={{ fontSize: 11, padding: "2px 8px", fontWeight: 400 }}>↺ 줌 초기화</button>}
      </h3>
      <div ref={cz.wheelRef}>
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart data={cz.view} margin={{ top: 5, right: 12, bottom: 5, left: 8 }} {...cz.dragProps}>
          <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
          <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
          <YAxis domain={domain} tick={{ fontSize: 10 }} width={52}
            tickFormatter={(v) => isKR ? `${Math.round(v / 1000)}k` : String(v)} />
          <Tooltip contentStyle={TIP_STYLE} labelStyle={TIP_LABEL} itemStyle={TIP_ITEM} /><Legend />
          <Bar dataKey="hl" isAnimationActive={false} legendType="none" shape={Candle} />
          <Line type="monotone" dataKey="ma5" stroke="#9aa0a6" strokeWidth={1} dot={false} name="MA5" connectNulls />
          <Line type="monotone" dataKey="ma20" stroke="#c4982b" strokeWidth={1} dot={false} name="MA20" connectNulls />
          <Line type="monotone" dataKey="ma60" stroke="#1668c4" strokeWidth={1} dot={false} name="MA60" connectNulls />
          {cz.area && <ReferenceArea x1={cz.area.x1} x2={cz.area.x2} fill="#4f8ff5" fillOpacity={0.15} strokeOpacity={0.3} />}
        </ComposedChart>
      </ResponsiveContainer>
      </div>
    </div>
  );
}

// ── 서브 지표 차트 (4분할 그리드 1칸) ──────────────────────────────────────────
function SubChart({ ikey, label, data, isKR, dragProps, area }:
  { ikey: string; label: string; data: Record<string, unknown>[]; isKR: boolean;
    dragProps?: object; area?: DragArea }) {
  const rc: RC = RENDER[ikey] || { series: [{ k: ikey, name: label, color: ACCENT }] };
  const M = { top: 5, right: 16, bottom: 5, left: 8 };  // 주가 차트와 동일 margin → X축 정렬
  const zoomArea = area ? <ReferenceArea x1={area.x1} x2={area.x2} fill="#4f8ff5" fillOpacity={0.15} strokeOpacity={0.3} /> : null;
  const kfmt = (v: number) => isKR ? `${Math.round(v / 1000)}k` : String(v);

  // 거래량 — 전일비 색 막대 + 거래량 이동평균
  if (rc.volume) {
    return (
      <>
        <h3 style={{ marginTop: 0 }}>
          {label}
          <span style={{ fontSize: 11, color: "var(--muted)", fontWeight: 400 }}>
            {"  "}· 전일比 ▲+5% 빨강 / ▼−5% 파랑 · MA 5종
          </span>
        </h3>
        <ResponsiveContainer width="100%" height={210}>
          <ComposedChart data={data} margin={M} {...dragProps}>
            <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={48} />
            <YAxis tick={{ fontSize: 10 }} width={56}
              tickFormatter={(v) => v >= 1e6 ? `${(v / 1e6).toFixed(0)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v)} />
            <Tooltip formatter={(v) => Number(v).toLocaleString()}
              contentStyle={TIP_STYLE} labelStyle={TIP_LABEL} itemStyle={TIP_ITEM} />
            <Bar dataKey="volume" name="거래량" isAnimationActive={false}>
              {data.map((d, i) => {
                const vc = d.vol_chg as number | null;
                const fill = vc != null && vc >= 5 ? UP : vc != null && vc <= -5 ? DOWN : "#d7cfc4";
                return <Cell key={i} fill={fill} />;
              })}
            </Bar>
            {VOL_MA.map(([k, lbl, col]) => (
              <Line key={k} type="monotone" dataKey={k} stroke={col} strokeWidth={1} dot={false} name={lbl} />
            ))}
            {zoomArea}
          </ComposedChart>
        </ResponsiveContainer>
      </>
    );
  }

  // 일반 — 설정대로 라인/밴드/히스토그램 + 기준선 + 줌
  const priceY = !!rc.priceOverlay;
  return (
    <>
      <h3 style={{ marginTop: 0 }}>{label}</h3>
      <ResponsiveContainer width="100%" height={210}>
        <ComposedChart data={data} margin={M} {...dragProps}>
          <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
          <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={48} />
          <YAxis domain={rc.domain || ["auto", "auto"]} ticks={rc.ticks} tick={{ fontSize: 10 }}
            width={56} tickFormatter={priceY ? kfmt : undefined} />
          <Tooltip contentStyle={TIP_STYLE} labelStyle={TIP_LABEL} itemStyle={TIP_ITEM} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {(rc.refs || []).map((r, i) => (
            <ReferenceLine key={i} y={r.y} stroke={r.color} strokeDasharray="4 4" />
          ))}
          {priceY && (
            <Line type="monotone" dataKey="close" stroke={ACCENT} strokeWidth={1.4} dot={false} name="종가" connectNulls />
          )}
          {rc.hist && <Bar dataKey={rc.hist} fill="#cbb9ac" name="히스토그램" isAnimationActive={false} />}
          {rc.histSign && (
            <Bar dataKey={rc.histSign} name={label} isAnimationActive={false}>
              {data.map((d, i) => {
                const v = d[rc.histSign as string] as number | null;
                return <Cell key={i} fill={v != null && v >= 0 ? UP : DOWN} />;
              })}
            </Bar>
          )}
          {(rc.series || []).map((s) => (
            <Line key={s.k} type="monotone" dataKey={s.k} stroke={s.color}
              strokeWidth={s.width || 1.4} strokeDasharray={s.dash} dot={false} name={s.name} connectNulls />
          ))}
          {zoomArea}
        </ComposedChart>
      </ResponsiveContainer>
    </>
  );
}

// ── 지표 설명·해석 (버튼 토글) ────────────────────────────────────────────────
function ExplToggle(props: {
  ikey: string; series: SymbolPoint[]; isKR: boolean; dol: string; won: string; benchName: string;
}) {
  const [open, setOpen] = useState(false);
  const { def, reading } = interpret(props.ikey, props.series, props);
  return (
    <div style={{ marginTop: 8 }}>
      <button className="ghost sm" onClick={() => setOpen((o) => !o)}>
        {open ? "설명·해석 닫기 ▴" : "설명·해석 보기 ▾"}
      </button>
      {open && (
        <div className="ca-expl">
          <div><b>설명</b> — {def}</div>
          <div style={{ marginTop: 6 }}><b>최근 1개월 해석</b> — {reading}</div>
        </div>
      )}
    </div>
  );
}

// 규칙기반 해석 — 최근 약 1개월(21 거래일) 수치로 문구 생성.
function interpret(
  ikey: string, series: SymbolPoint[],
  ctx: { isKR: boolean; dol: string; won: string; benchName: string },
): { def: string; reading: string } {
  const recent = series.slice(-21);
  const vals = (k: keyof SymbolPoint) =>
    recent.map((p) => p[k]).filter((v) => v != null) as number[];
  const mean = (a: number[]) => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const lastN = (k: keyof SymbolPoint) => { const a = vals(k); return a.length ? a[a.length - 1] : null; };
  const trend = (k: keyof SymbolPoint) => {
    const a = vals(k); if (a.length < 2) return "데이터 부족";
    const d = a[a.length - 1] - a[0];
    return d > 0 ? "상승 추세" : d < 0 ? "하락 추세" : "보합";
  };
  const f1 = (v: number | null) => v == null ? "—" : v.toFixed(1);
  const money = (v: number | null) => v == null ? "—"
    : ctx.isKR ? `${ctx.dol}${Math.round(v).toLocaleString()}${ctx.won}`
      : `${ctx.dol}${v.toFixed(2)}`;

  switch (ikey) {
    case "price": {
      const close = lastN("close"); const m20 = lastN("ma20"); const m60 = lastN("ma60"); const m240 = lastN("ma240");
      const ma5 = vals("ma5"); const ma20a = vals("ma20");
      let cross = "";
      if (ma5.length >= 2 && ma20a.length >= 2) {
        const p5 = ma5[ma5.length - 2], c5 = ma5[ma5.length - 1];
        const p20 = ma20a[ma20a.length - 2], c20 = ma20a[ma20a.length - 1];
        if (p5 <= p20 && c5 > c20) cross = " 최근 단기선(MA5)이 MA20을 상향 돌파(골든크로스).";
        else if (p5 >= p20 && c5 < c20) cross = " 최근 단기선(MA5)이 MA20을 하향 돌파(데드크로스).";
      }
      const pos = close != null && m20 != null
        ? close >= m20 ? "MA20 위(단기 상승 우위)" : "MA20 아래(단기 약세)" : "—";
      return {
        def: "캔들은 일별 시·고·저·종가(상승=빨강/하락=파랑), 이동평균(MA)은 추세, 초록 점선은 "
          + `벤치마크(${ctx.benchName}) 대비 상대 흐름입니다.`,
        reading: `현재가 ${money(close)}, ${pos}. MA60 ${money(m60)} · MA240 ${money(m240)} 기준 `
          + `장기 추세는 ${close != null && m240 != null ? (close >= m240 ? "상승 국면" : "하락 국면") : "—"}.${cross}`,
      };
    }
    case "rsi_14": {
      const m = mean(vals("rsi_14")); const l = lastN("rsi_14");
      const zone = l == null ? "—" : l >= 70 ? "과매수 — 단기 과열·조정 주의" : l <= 30 ? "과매도 — 반등 가능성" : "중립";
      return {
        def: "RSI(14)는 0~100으로 상승/하락 압력 균형을 나타냅니다. 70↑ 과매수, 30↓ 과매도.",
        reading: `최근 1개월 평균 ${f1(m)}, 현재 ${f1(l)} (${zone}). ${trend("rsi_14")}.`,
      };
    }
    case "macd": {
      const macd = lastN("macd"); const sig = lastN("macd_signal"); const hist = lastN("macd_hist");
      const rel = macd != null && sig != null
        ? macd > sig ? "MACD가 시그널 위 — 상승 모멘텀" : "MACD가 시그널 아래 — 하락 모멘텀" : "—";
      return {
        def: "MACD는 단기(12)−장기(26) 지수이평 차이. 시그널선(9) 상향 돌파=매수 신호, 히스토그램=둘의 격차.",
        reading: `현재 MACD ${macd == null ? "—" : macd.toFixed(3)}, 시그널 ${sig == null ? "—" : sig.toFixed(3)} (${rel}). `
          + `히스토그램 ${hist == null ? "—" : hist.toFixed(3)}(${hist != null && hist >= 0 ? "양(+)" : "음(−)"}).`,
      };
    }
    case "stoch": {
      const k = lastN("stoch_k"); const d = lastN("stoch_d");
      const zone = k == null ? "—" : k >= 80 ? "과매수권" : k <= 20 ? "과매도권" : "중립권";
      return {
        def: "스토캐스틱(14,3)은 최근 범위 내 종가 위치(%K)와 그 이동평균(%D). 80↑ 과매수, 20↓ 과매도.",
        reading: `현재 %K ${f1(k)} / %D ${f1(d)} (${zone}). ${trend("stoch_k")}.`,
      };
    }
    case "volume": {
      const recAvg = mean(vals("volume"));
      const prior = series.slice(-42, -21).map((p) => p.volume).filter((v) => v != null) as number[];
      const priAvg = mean(prior);
      const cmp = recAvg != null && priAvg != null
        ? recAvg > priAvg * 1.1 ? "직전 1개월 대비 증가(관심↑)" : recAvg < priAvg * 0.9 ? "직전 1개월 대비 감소(관심↓)" : "비슷"
        : "—";
      return {
        def: "거래량은 매매 활발도. 가격 변동과 함께 보면 추세 신뢰도를 가늠합니다. MA 5종으로 추세 확인.",
        reading: `최근 1개월 평균 거래량 ${recAvg == null ? "—" : Math.round(recAvg).toLocaleString()}주, ${cmp}.`,
      };
    }
    case "atr_14": {
      const a = lastN("atr_14"); const close = lastN("close");
      const pct = a != null && close ? (a / close * 100) : null;
      return {
        def: "ATR(14)은 일중 변동폭의 평균(절대값). 변동성·손절폭 설정의 참고치입니다.",
        reading: `현재 ATR ${money(a)} (종가의 ${pct == null ? "—" : pct.toFixed(1)}%). ${trend("atr_14")} — `
          + `${pct != null && pct >= 3 ? "변동성 큰 편" : "변동성 보통~낮음"}.`,
      };
    }
    case "obv": {
      return {
        def: "OBV는 상승일 거래량을 더하고 하락일은 빼 누적한 지표. 가격과 같이 오르면 매집(추세 신뢰).",
        reading: `최근 1개월 OBV는 ${trend("obv")} — ${trend("obv") === "상승 추세" ? "매집 우위" : trend("obv") === "하락 추세" ? "분산 우위" : "중립"}.`,
      };
    }
    case "vol_20d": {
      const l = lastN("vol_20d"); const m = mean(vals("vol_20d"));
      return {
        def: "변동성(20일)은 최근 20일 수익률의 표준편차(연율화). 위험 수준을 나타냅니다.",
        reading: `현재 ${f1(l)}%, 최근 1개월 평균 ${f1(m)}%. ${trend("vol_20d")}.`,
      };
    }
    case "bb": {
      const close = lastN("close"); const up = lastN("bb_upper"); const low = lastN("bb_lower"); const mid = lastN("bb_mid");
      const pctB = close != null && up != null && low != null && up !== low
        ? ((close - low) / (up - low) * 100) : null;
      const zone = pctB == null ? "—" : pctB >= 100 ? "상단 돌파(과열)" : pctB >= 80 ? "상단 근접"
        : pctB <= 0 ? "하단 이탈(과냉)" : pctB <= 20 ? "하단 근접" : "밴드 중앙권";
      return {
        def: "볼린저밴드는 20일 이동평균 ±2표준편차. 밴드 폭은 변동성, 상단 접근=과열, 하단=과냉 신호.",
        reading: `현재가 ${money(close)}, 밴드 내 위치 %B ${pctB == null ? "—" : pctB.toFixed(0)}% (${zone}). 중심선 ${money(mid)}.`,
      };
    }
    default:
      return genericInterpret(ikey, series);
  }
}

// 신규 38종 — RENDER 설정 + 최근 1개월 수치로 규칙기반 해석 자동 생성.
function genericInterpret(ikey: string, series: SymbolPoint[]): { def: string; reading: string } {
  const rc = RENDER[ikey];
  const def = DEF[ikey] || "기술적 지표입니다.";
  const recent = series.slice(-21);
  const fld = (rc?.priceOverlay ? rc.series?.[0]?.k
    : rc?.histSign || rc?.series?.[0]?.k || ikey) as string;
  const vals = recent.map((p) => (p as unknown as Record<string, number | null>)[fld])
    .filter((v) => v != null) as number[];
  const last = vals.length ? vals[vals.length - 1] : null;
  const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  const tr = vals.length >= 2
    ? (vals[vals.length - 1] > vals[0] ? "상승" : vals[vals.length - 1] < vals[0] ? "하락" : "보합")
    : "데이터 부족";
  const f = (v: number | null) => v == null ? "—"
    : Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(2);

  let reading: string;
  if (rc?.priceOverlay) {
    const close = recent.length ? recent[recent.length - 1].close : null;
    const rel = close != null && last != null ? (close >= last ? "위(강세)" : "아래(약세)") : "—";
    reading = `현재가가 ${rc.series?.[0]?.name ?? "기준선"} ${rel}. 기준선 1개월 추세 ${tr}.`;
  } else if (rc?.refs && rc.refs.length >= 2) {
    const ys = rc.refs.map((r) => r.y);
    const hi = Math.max(...ys), lo = Math.min(...ys);
    const zone = last == null || hi === lo ? "중립"
      : last >= hi ? "상단(과열/강세)" : last <= lo ? "하단(과냉/약세)" : "중립";
    reading = `현재 ${f(last)}, 최근 1개월 평균 ${f(avg)} (${zone}). 추세 ${tr}.`;
  } else {
    reading = `현재 ${f(last)}, 최근 1개월 평균 ${f(avg)}. 추세 ${tr}.`;
  }
  return { def, reading };
}

// ── 가격 캔들 차트 커스텀 툴팁 (OHLC + MA + 벤치마크) ──────────────────────────
type PriceTipEntry = { payload: SymbolPoint & { date: string } };
function PriceTooltip(props: {
  active?: boolean; payload?: readonly PriceTipEntry[];
  isKR: boolean; dol: string; won: string; benchName: string;
}) {
  if (!props.active || !props.payload || !props.payload.length) return null;
  const p = props.payload[0].payload;
  const m = (v: number | null) => v == null ? "—"
    : props.isKR ? `${props.dol}${Math.round(v).toLocaleString()}${props.won}`
      : `${props.dol}${v.toFixed(2)}`;
  const row = (label: string, v: number | null, color?: string) => (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 16, color }}>
      <span>{label}</span><span>{m(v)}</span>
    </div>
  );
  return (
    <div style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 8,
      padding: "8px 11px", fontSize: 12, boxShadow: "0 4px 14px rgba(0,0,0,0.35)", minWidth: 160 }}>
      <div style={{ fontWeight: 700, marginBottom: 5 }}>{p.date}</div>
      {row("시가", p.open)}{row("고가", p.high)}{row("저가", p.low)}
      {row("종가", p.close, p.close != null && p.open != null ? (p.close >= p.open ? UP : DOWN) : undefined)}
      <div style={{ borderTop: "1px solid var(--border,#e3e8ef)", margin: "5px 0" }} />
      {row("MA20", p.ma20)}{row("MA60", p.ma60)}{row("MA240", p.ma240)}
      {row(props.benchName, p.bench, BENCH)}
    </div>
  );
}

// ── 요약 박스 (선택형) ────────────────────────────────────────────────────────
function MetricCard(props: {
  sel: string; onSel: (v: string) => void; last: SymbolDetail["last"];
  fmtP: (v: number | null | undefined) => string; dol: string; won: string;
}) {
  const { sel, onSel, last, fmtP, dol, won } = props;
  const head = (
    <div className="ca-card-head">
      <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600 }}>
        {METRIC_OPTS.find(([k]) => k === sel)?.[1]}
      </span>
      <select value={sel} onChange={(e) => onSel(e.target.value)} aria-label="요약 지표 선택">
        {METRIC_OPTS.map(([k, lbl]) => <option key={k} value={k}>{lbl}</option>)}
      </select>
    </div>
  );
  let body: ReactNode;
  switch (sel) {
    case "rsi": {
      const r = last.rsi_14;
      const label = r == null ? "" : r >= 70 ? "과매수" : r <= 30 ? "과매도" : "중립";
      const color = r == null ? "var(--muted)" : r >= 70 ? UP : r <= 30 ? DOWN : "var(--muted)";
      body = <><div style={{ fontSize: 22, fontWeight: 700 }}>{r == null ? "—" : r.toFixed(1)}</div>
        <div style={{ color, fontWeight: 600, fontSize: 13 }}>{label}</div></>;
      break;
    }
    case "range52":
      body = <><div style={{ fontSize: 14 }}>고 {dol}{fmtP(last.high_52w)}{won}</div>
        <div style={{ fontSize: 14 }}>저 {dol}{fmtP(last.low_52w)}{won}</div></>;
      break;
    case "volume":
      body = <div style={{ fontSize: 20, fontWeight: 700 }}>{last.volume == null ? "—" : last.volume.toLocaleString()}</div>;
      break;
    case "macd":
      body = <div style={{ fontSize: 20, fontWeight: 700, color: last.macd == null ? "var(--muted)" : last.macd >= 0 ? UP : DOWN }}>
        {last.macd == null ? "—" : last.macd.toFixed(3)}</div>;
      break;
    case "stoch":
      body = <div style={{ fontSize: 22, fontWeight: 700 }}>{last.stoch_k == null ? "—" : last.stoch_k.toFixed(1)}<span style={{ fontSize: 12, color: "var(--muted)" }}> %K</span></div>;
      break;
    case "atr":
      body = <div style={{ fontSize: 20, fontWeight: 700 }}>{dol}{fmtP(last.atr_14)}{won}</div>;
      break;
    case "vol20":
      body = <div style={{ fontSize: 22, fontWeight: 700 }}>{last.vol_20d == null ? "—" : `${last.vol_20d.toFixed(1)}%`}</div>;
      break;
    default:
      body = <div style={{ fontSize: 22, fontWeight: 700 }}>—</div>;
  }
  return <div className="panel" style={{ padding: 14, marginBottom: 0 }}>{head}<div style={{ marginTop: 6 }}>{body}</div></div>;
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="panel" style={{ padding: 14, marginBottom: 0 }}>
      <div style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}
