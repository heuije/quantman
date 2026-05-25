import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { parseScreenerKey } from "../types";
import type { ScreenerPreset, ScreenerSpecIO, SymbolInfo } from "../types";
import ScreenerPanel from "./ScreenerPanel";
import TabbedSymbolList from "./TabbedSymbolList";

export const SYMBOL_CAT_ORDER = [
  "국내주식 (KOSPI)", "국내주식 (KOSDAQ)",
  "국내ETF/ETN (KOSPI)", "국내ETF/ETN (KOSDAQ)",
  "국내REITs (KOSPI)",
  "미국 NASDAQ 주식", "미국 NYSE 주식", "미국 AMEX 주식",
  "미국 NASDAQ ETF/ETN", "미국 NYSE ETF/ETN", "미국 AMEX ETF/ETN",
  "일본 주식", "일본 ETF/ETN",
  "홍콩 주식", "홍콩 ETF/ETN",
  "자산", "변동성", "금리·환율", "신용", "거시지표", "심리", "개별종목",
];

/** 팝오버를 외부 클릭·Esc로 닫는 훅. 트리거+패널을 감싸는 ref를 반환. */
export function usePopoverDismiss<T extends HTMLElement>(
  open: boolean, setOpen: (v: boolean) => void,
) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, setOpen]);
  return ref;
}

/** 카테고리 헤더로 그룹화된 검색 가능한 선택 목록. */
export function CategoryList({ items, order, selected, search, onPick }: {
  items: { key: string; label: string; cat: string; badge?: string }[];
  order: string[];
  selected?: string;
  search?: string;
  onPick: (key: string) => void;
}) {
  const q = (search ?? "").trim().toLowerCase();
  const filtered = q
    ? items.filter((i) => i.label.toLowerCase().includes(q)
                       || i.key.toLowerCase().includes(q))
    : items;

  const byCat: Record<string, typeof items> = {};
  for (const it of filtered) (byCat[it.cat] ??= []).push(it);
  const cats = order.filter((c) => byCat[c]?.length)
    .concat(Object.keys(byCat).filter((c) => !order.includes(c)));

  // 큰 리스트는 잘라서 표시 (검색이 좁히기 전 4000+개 그대로 렌더 방지)
  const LIMIT_PER_CAT = q ? 200 : 50;

  return (
    <div className="cat-list">
      {cats.length === 0 && <div className="cat-empty">결과 없음</div>}
      {cats.map((cat) => {
        const items = byCat[cat];
        const shown = items.slice(0, LIMIT_PER_CAT);
        const hidden = items.length - shown.length;
        return (
          <div key={cat}>
            <div className="cat-head">{cat} <span className="cat-head-n">{items.length}</span></div>
            {shown.map((it) => (
              <button
                key={it.key} type="button"
                className={"cat-item" + (it.key === selected ? " sel" : "")}
                onClick={() => onPick(it.key)}
              >
                <span>{it.label}</span>
                {it.badge && <span className="cat-item-badge">{it.badge}</span>}
              </button>
            ))}
            {hidden > 0 && (
              <div className="cat-empty" style={{ fontSize: 11 }}>
                +{hidden}개 더 — 검색으로 좁혀주세요
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 종목 선택 칩 — 클릭 시 탭 + 검색 팝오버가 열린다.
 *
 * tradableOnly=true (매수 대상): KIS 매수 가능 종목 + 자동 선택 프리셋
 * tradableOnly=false (매수 조건): 백테스트 데이터 있는 종목 (분류별 탭)
 */
export default function SymbolPicker({
  symbols, value, tradableOnly, lockMode, onChange,
  screenerSpec, setScreenerSpec, setScreenerLimit, inline,
}: {
  symbols: SymbolInfo[];
  value: string;
  tradableOnly?: boolean;
  /** "screener" → 자동 선택 패널 고정, "manual" → 수동만. 미지정 시 내부 토글 표시. */
  lockMode?: "manual" | "screener";
  onChange: (sym: string) => void;
  /** 자동 선택 커스텀 spec (screener:custom). 매수 대상에서만 전달. */
  screenerSpec?: ScreenerSpecIO | null;
  setScreenerSpec?: (s: ScreenerSpecIO | null) => void;
  /** 세트 적용 시 최대 동시 보유 종목 수 = 세트의 상위 N개로 동기화. */
  setScreenerLimit?: (n: number) => void;
  /** inline=true: chip 버튼 없이 picker 본문 항상 노출 (모달 등 큰 영역 전용). */
  inline?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);

  // 자동 선택 모드는 매수 대상에서만 활성
  const screenerKey = parseScreenerKey(value);
  const [innerMode, setMode] = useState<"manual" | "screener">(
    screenerKey ? "screener" : "manual");
  const mode = lockMode ?? innerMode;
  const [presets, setPresets] = useState<ScreenerPreset[]>([]);
  const [asOf, setAsOf] = useState<string | null>(null);

  useEffect(() => {
    if (!tradableOnly || presets.length > 0) return;
    api.listScreenerPresets()
      .then((r) => { setPresets(r.presets); setAsOf(r.as_of); })
      .catch(() => {/* health 미공개도 무관 — UI는 manual 가능 */});
  }, [tradableOnly, presets.length]);

  const list = symbols.filter((s) =>
    tradableOnly ? s.tradable : s.indicators.length > 0);
  const empty = tradableOnly && list.length === 0 && symbols.length > 0;

  const tabOrder = tradableOnly ? TRADABLE_TAB_ORDER : OPERAND_TAB_ORDER;

  // chip 라벨 — 종목명·자동 선택 모두 표시
  const sel = value && !screenerKey
    ? symbols.find((s) => s.symbol === value) : undefined;
  const screenerPreset = screenerKey
    ? presets.find((p) => p.key === screenerKey) : null;
  const screenerLabel = screenerKey === "custom"
    ? (screenerSpec?.label || "맞춤 세트")
    : (screenerPreset?.title ?? screenerKey);
  const chipLabel = !value ? "종목 선택"
    : screenerKey
      ? `[자동] ${screenerLabel}`
      : sel?.name ? `${value} ${sel.name}` : value;

  // body — segmented (tradableOnly && !lockMode일 때만) + 본문 영역
  const body = (
    <>
      {tradableOnly && !lockMode && (
        <div className="seg" style={{ marginBottom: 10 }}>
          <button type="button"
                  className={mode === "manual" ? "on" : ""}
                  onClick={() => setMode("manual")}>
            수동 선택
          </button>
          <button type="button"
                  className={mode === "screener" ? "on" : ""}
                  onClick={() => setMode("screener")}>
            자동 선택
          </button>
        </div>
      )}

      {empty ? (
        <div className="cat-empty" style={{ padding: 16, lineHeight: 1.6 }}>
          매수 가능 종목 목록을 준비 중입니다.<br/>
          서버가 KIS 공식 마스터를 다운로드 중입니다. 잠시 후 다시 시도해주세요.
        </div>
      ) : mode === "screener" && tradableOnly ? (
        <ScreenerPanel
          presets={presets}
          asOf={asOf}
          tradeSymbol={value}
          setTradeSymbol={onChange}
          spec={screenerSpec ?? null}
          setSpec={setScreenerSpec ?? (() => {})}
          setScreenerLimit={setScreenerLimit ?? (() => {})}
          onClose={inline ? (() => {}) : (() => setOpen(false))}
        />
      ) : (
        <TabbedSymbolList
          items={list.map((s) => ({
            key: s.symbol,
            label: s.name ? `${s.symbol} ${s.name}` : s.symbol,
            cat: tabCategoryFor(s, tradableOnly),
            badge: tradableOnly && s.has_backtest_data === false
              ? "백테스트 불가" : undefined,
          }))}
          order={tabOrder}
          selected={value}
          placeholder={tradableOnly ? "종목명 또는 코드 검색…" : "종목 검색…"}
          onPick={(k) => { onChange(k); if (!inline) setOpen(false); }}
        />
      )}
    </>
  );

  // Inline 모드 — chip 트리거 없이 본문 항상 노출 (모달 등에서 사용).
  if (inline) return <div className="symbol-picker-inline">{body}</div>;

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {chipLabel}
        <span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover popover-wide">{body}</div>
      )}
    </span>
  );
}

// ── 탭 분류 헬퍼 ─────────────────────────────────────────────────────────────

const TRADABLE_TAB_ORDER = [
  "KOSPI", "KOSDAQ",
  "미국 NASDAQ", "미국 NYSE", "미국 AMEX",
  "일본", "홍콩",
];

const OPERAND_TAB_ORDER = [
  "자산", "변동성", "금리·환율", "신용", "거시지표", "심리",
];

function tabCategoryFor(s: SymbolInfo, tradable?: boolean): string {
  if (!tradable) return s.category;        // 매수 조건: 카테고리 그대로 (자산/변동성/...)
  // 매수 대상: 시장별 단일 탭 (주식 + ETF + REITs 통합)
  const cat = s.category;
  if (cat.includes("KOSPI")) return "KOSPI";
  if (cat.includes("KOSDAQ")) return "KOSDAQ";
  if (cat.includes("NASDAQ")) return "미국 NASDAQ";
  if (cat.includes("NYSE")) return "미국 NYSE";
  if (cat.includes("AMEX")) return "미국 AMEX";
  if (cat.startsWith("일본")) return "일본";
  if (cat.startsWith("홍콩")) return "홍콩";
  return cat;
}
