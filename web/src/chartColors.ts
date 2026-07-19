/**
 * 차트 색 팔레트 — recharts SVG는 CSS 변수(var())를 못 읽어(색을 속성값으로 직접 받음) 토큰을
 * JS로 읽어 인라인한다. 테마(data-theme) 전환 시 재계산(useSyncExternalStore 구독) → 다크·라이트·
 * PDF 화이트 인쇄를 모두 한 소스로 대응한다. 토큰 값의 SSOT는 web/src/index.css :root/[data-theme].
 */
import { useSyncExternalStore } from "react";

export interface ChartColors {
  accent: string; strong: string; muted: string; grid: string; text: string;
  up: string; down: string; upSoft: string; downSoft: string; panel: string;
}

// 다크 기본값 — CSS var 미해석(초기/비브라우저) 시 폴백. index.css :root와 동기화.
const FALLBACK: ChartColors = {
  accent: "#d4a738", strong: "#e0b958", muted: "#8b94a3", grid: "#2b323e", text: "#d2d8e0",
  up: "#de3033", down: "#1668c4", upSoft: "rgba(222,48,51,0.18)", downSoft: "rgba(22,104,196,0.22)",
  panel: "#1c212b",
};

function rd(cs: CSSStyleDeclaration, name: string, fb: string): string {
  const v = cs.getPropertyValue(name).trim();
  return v || fb;
}

export function readChartColors(): ChartColors {
  if (typeof document === "undefined") return FALLBACK;
  const cs = getComputedStyle(document.documentElement);
  return {
    accent: rd(cs, "--accent", FALLBACK.accent),
    strong: rd(cs, "--accent-strong", FALLBACK.strong),
    muted: rd(cs, "--muted", FALLBACK.muted),
    grid: rd(cs, "--border", FALLBACK.grid),
    text: rd(cs, "--text", FALLBACK.text),
    up: rd(cs, "--up", FALLBACK.up),
    down: rd(cs, "--down", FALLBACK.down),
    upSoft: rd(cs, "--up-soft", FALLBACK.upSoft),
    downSoft: rd(cs, "--down-soft", FALLBACK.downSoft),
    panel: rd(cs, "--panel", FALLBACK.panel),
  };
}

// useSyncExternalStore 스냅샷 — 값이 안 바뀌면 같은 참조를 돌려줘 불필요 재렌더/루프를 막는다.
let snap: ChartColors = FALLBACK;
let sig = "";
function getSnapshot(): ChartColors {
  const c = readChartColors();
  const s = c.accent + c.grid + c.text + c.panel + c.up + c.down + c.muted;
  if (s !== sig) { sig = s; snap = c; }
  return snap;
}
function subscribe(cb: () => void): () => void {
  if (typeof document === "undefined") return () => {};
  const obs = new MutationObserver(cb);
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => obs.disconnect();
}

/** 현재 테마의 차트 팔레트. data-theme 전환 시 자동 재계산(recharts 재렌더). */
export function useChartColors(): ChartColors {
  return useSyncExternalStore(subscribe, getSnapshot, () => FALLBACK);
}

// ── 훅 없이 렌더 시점 색을 읽는 경로 (호출부 110곳을 안 바꾸는 라이브 게터 Proxy용) ─────
// getComputedStyle 반복 비용을 피하려 팔레트를 캐시하고 data-theme 변경 시에만 무효화한다.
let _now: ChartColors | null = null;
function chartColorsNow(): ChartColors {
  if (!_now) _now = readChartColors();
  return _now;
}
if (typeof document !== "undefined") {
  new MutationObserver(() => { _now = null; }).observe(
    document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}

/** 접근 시마다 현재 테마 토큰을 돌려주는 팔레트 — recharts는 렌더 시점 색을 쓰므로 그대로 인라인.
 *  테마 전환 시 차트 재렌더(remount)는 결과 래퍼의 key={useThemeName()}가 담당한다. */
export const chartC: ChartColors = new Proxy({} as ChartColors, {
  get: (_t, p: string) => chartColorsNow()[p as keyof ChartColors],
}) as ChartColors;

/** 현재 data-theme 이름("light"/"dark"). 전환 시 구독 재렌더 → 결과 래퍼 key로 써서 차트 remount. */
export function useThemeName(): string {
  return useSyncExternalStore(
    subscribe,
    () => (typeof document !== "undefined"
      ? document.documentElement.getAttribute("data-theme") ?? "dark" : "dark"),
    () => "dark",
  );
}
