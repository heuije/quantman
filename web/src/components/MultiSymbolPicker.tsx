/**
 * 수동 다중 종목 선택기 — 1개부터 N개까지.
 *
 * 선택된 종목을 칩 그리드로 표시하고 X로 제거. 새 종목 추가 버튼 클릭 시
 * 시장별 탭 + 검색 팝오버가 열린다.
 *
 * value는 콤마 구분 문자열 ("005930,000660,035420") — backend Strategy 스키마와 동일.
 */

import { useState } from "react";
import type { IndicatorInfo, IrNode, SymbolInfo } from "../types";
import { usePopoverDismiss } from "./SymbolPicker";
import SentenceTree, { type Catalog } from "./SentenceTree";
import TabbedSymbolList from "./TabbedSymbolList";

const TRADABLE_TAB_ORDER = [
  "KOSPI", "KOSDAQ", "선물",
  "미국 NASDAQ", "미국 NYSE", "미국 AMEX",
];

function categoryFor(cat: string): string {
  if (cat.includes("KOSPI")) return "KOSPI";
  if (cat.includes("KOSDAQ")) return "KOSDAQ";
  if (cat.includes("NASDAQ")) return "미국 NASDAQ";
  if (cat.includes("NYSE")) return "미국 NYSE";
  if (cat.includes("AMEX")) return "미국 AMEX";
  return cat;
}

export default function MultiSymbolPicker({ symbols, value, onChange, inline, scope = "tradable",
  strategies, screener, onScreenerChange, screenerRefresh, onScreenerRefreshChange,
  catalog, selfIndicators }: {
  symbols: SymbolInfo[];
  value: string;                   // 콤마 분리 — "005930,000660" (전략 조합 시 "strat:5" 포함)
  onChange: (v: string) => void;
  /** inline=true: "+ 종목 추가" 버튼 없이 종목 리스트 항상 노출 (모달 등 큰 영역 전용). */
  inline?: boolean;
  /** 노출 범위 — tradable: 실거래 가능 종목(매수 대상용) · backtest: 백테스트 데이터 보유 종목(연구소용). */
  scope?: "tradable" | "backtest";
  /** "내 전략" 탭 — 저장 전략을 strat:<id> 합성 자산으로 선택(전략 조합 G3). 미지정 시 탭 숨김. */
  strategies?: { id: number; name: string; run_mode?: string }[];
  /** 세부조건(선택 종목 2차 필터). onScreenerChange가 주어질 때만 렌더. */
  screener?: IrNode | null;
  onScreenerChange?: (n: IrNode | null) => void;
  screenerRefresh?: "each_rebalance" | "once_at_start";
  onScreenerRefreshChange?: (r: "each_rebalance" | "once_at_start") => void;
  catalog?: Catalog;
  selfIndicators?: IndicatorInfo[];
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLDivElement>(open, setOpen);

  const selected = value.split(",").map((s) => s.trim()).filter(Boolean);
  // 선물은 주식 마스터에 없어 tradable=false지만 백테스트 선택은 가능해야 함(자동매매는 별도 게이트가 차단).
  const list = symbols.filter((s) => (scope === "backtest"
    ? s.has_backtest_data
    : (s.tradable || s.asset_class === "futures")));

  // "내 전략" 합성 자산(strat:<id>) — 저장 전략을 유니버스 자산으로(조합 폐쇄성). 시장 탭과 별도.
  const stratItems = (strategies ?? []).map((s) => ({
    key: `strat:${s.id}`,
    label: s.name,
    cat: "내 전략",
    badge: s.run_mode === "live" ? "실전" : s.run_mode === "paper" ? "모의" : "초안",
  }));
  const stratLabel = new Map(stratItems.map((it) => [it.key, it.label]));
  const tabOrder = stratItems.length ? ["내 전략", ...TRADABLE_TAB_ORDER] : TRADABLE_TAB_ORDER;
  const labelFor = (sym: string): string => {
    if (stratLabel.has(sym)) return stratLabel.get(sym)!;
    const info = list.find((s) => s.symbol === sym);
    return info?.name ? `${sym} ${info.name}` : sym;
  };

  // 다중선택 — 팝오버를 닫지 않고 토글. 연속으로 여러 종목 체크 가능.
  function toggle(sym: string) {
    if (selected.includes(sym)) {
      onChange(selected.filter((s) => s !== sym).join(","));
    } else {
      onChange([...selected, sym].join(","));
    }
  }
  function remove(sym: string) {
    onChange(selected.filter((s) => s !== sym).join(","));
  }

  // 선택된 종목도 리스트에 유지 — 체크 해제로 빼낼 수 있게. "내 전략"은 맨 앞 탭.
  const items = [
    ...stratItems,
    ...list.map((s) => ({
      key: s.symbol,
      label: s.name ? `${s.symbol} ${s.name}` : s.symbol,
      cat: s.asset_class === "futures" ? "선물" : categoryFor(s.category),
      badge: s.autotrade_hint === "backtest_only"
        ? "백테스트 전용"                              // 자동매매 불가(지수·매크로·해외선물 등)
        : (scope === "tradable" && s.has_backtest_data === false)
          ? "백테스트 불가"                            // 라이브만 가능·백테스트 데이터 없음
          : undefined,                                 // 자동매매 가능 — 뱃지 없음
    })),
  ];

  // 세부조건(2차 필터) — onScreenerChange가 주어질 때만(유니버스 선택기 한정) 렌더.
  const screenerBlock = onScreenerChange ? (
    <ScreenerSection
      disabled={selected.length === 0}
      condition={screener ?? null}
      onCondition={onScreenerChange}
      refresh={screenerRefresh ?? "each_rebalance"}
      onRefresh={onScreenerRefreshChange ?? (() => {})}
      catalog={catalog} symbols={symbols} selfIndicators={selfIndicators ?? []}
      count={selected.length}
    />
  ) : null;

  // Inline 모드 — chips 영역 + 종목 리스트 항상 노출. "+ 종목 추가" 버튼 없음.
  if (inline) {
    return (
      <div className="multi-picker multi-picker-inline">
        <div className="multi-chips">
          {selected.length === 0 && (
            <span className="muted small">아직 선택된 종목이 없습니다.</span>
          )}
          {selected.map((sym) => {
            const label = labelFor(sym);
            return (
              <span key={sym} className="multi-chip">
                {label}
                <button type="button" className="multi-chip-x"
                        aria-label={`${sym} 제거`}
                        onClick={() => remove(sym)}>×</button>
              </span>
            );
          })}
        </div>
        <div className="multi-inline-list">
          <TabbedSymbolList
            items={items}
            order={tabOrder}
            placeholder="종목명 또는 코드 검색…"
            multiSelect
            selectedKeys={selected}
            onPick={toggle}
          />
        </div>
        <div className="multi-inline-foot muted small">
          {selected.length}개 선택됨 — 종목명을 클릭해 추가/제거
        </div>
        {screenerBlock}
      </div>
    );
  }

  return (
    <div className="multi-picker" ref={ref}>
      <div className="multi-chips">
        {selected.length === 0 && (
          <span className="muted small">아직 선택된 종목이 없습니다.</span>
        )}
        {selected.map((sym) => {
          const info = list.find((s) => s.symbol === sym);
          const label = info?.name ? `${sym} ${info.name}` : sym;
          return (
            <span key={sym} className="multi-chip">
              {label}
              <button type="button" className="multi-chip-x"
                      aria-label={`${sym} 제거`}
                      onClick={() => remove(sym)}>×</button>
            </span>
          );
        })}
        <button type="button" className="multi-add"
                onClick={() => setOpen((v) => !v)}>
          + 종목 추가
        </button>
      </div>

      {open && (
        <div className="popover popover-wide multi-popover">
          <TabbedSymbolList
            items={items}
            order={tabOrder}
            placeholder="종목명 또는 코드 검색…"
            multiSelect
            selectedKeys={selected}
            onPick={toggle}
          />
          {screenerBlock}
          <div className="multi-popover-foot">
            <span className="muted small">{selected.length}개 선택됨</span>
            <button type="button" className="sm" onClick={() => setOpen(false)}>
              완료
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * 단일 종목 참조 선택기 — 데이터 블록의 ref(예: "005930.Close")용.
 * 유니버스 선택기와 동일한 시장별 탭 + 검색 팝오버. "이 종목"(__SELF__, 전략 대상)이 기본.
 * 콤마 자유입력 select 대신 탭 분류 제공 — 종목명·코드 검색으로 빠르게 찾는다.
 */
export function SymbolRefPicker({ symbols, value, onChange }: {
  symbols: { symbol: string; name?: string; category?: string; has_backtest_data?: boolean }[];
  value: string;                   // "__SELF__" 또는 종목코드
  onChange: (sym: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);

  const list = symbols.filter((s) => s.has_backtest_data);
  const sel = value === "__SELF__" ? null : list.find((s) => s.symbol === value);
  const chipLabel = value === "__SELF__" ? "이 종목"
    : sel?.name ? `${value} ${sel.name}` : value;

  const items = [
    { key: "__SELF__", label: "이 종목 (전략 대상)", cat: "기본" },
    ...list.map((s) => ({
      key: s.symbol,
      label: s.name ? `${s.symbol} ${s.name}` : s.symbol,
      cat: categoryFor(s.category ?? ""),
    })),
  ];

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {chipLabel}
        <span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover popover-wide">
          <TabbedSymbolList
            items={items}
            order={["기본", ...TRADABLE_TAB_ORDER]}
            selected={value}
            placeholder="종목명 또는 코드 검색…"
            onPick={(k) => { onChange(k); setOpen(false); }}
          />
        </div>
      )}
    </span>
  );
}

/**
 * 세부조건 설정 — 선택한 종목에 대한 2차 필터(condition) + 재선별 시점.
 * 종목이 선택돼야 활성. 조건은 SentenceTree(문장형)로 조립, refresh로 동적/정적 선택.
 */
function ScreenerSection({ disabled, condition, onCondition, refresh, onRefresh,
                           catalog, symbols, selfIndicators, count }: {
  disabled: boolean; condition: IrNode | null; onCondition: (n: IrNode | null) => void;
  refresh: "each_rebalance" | "once_at_start";
  onRefresh: (r: "each_rebalance" | "once_at_start") => void;
  catalog?: Catalog; symbols: SymbolInfo[]; selfIndicators: IndicatorInfo[]; count: number;
}) {
  const [open, setOpen] = useState(false);
  if (disabled) {
    return <div className="muted small" style={{ marginTop: 8 }}>
      세부조건을 쓰려면 먼저 종목을 선택하세요.</div>;
  }
  return (
    <div className="screener-section" style={{ marginTop: 8, borderTop: "1px solid var(--line)", paddingTop: 8 }}>
      <button type="button" className="multi-add" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} 세부조건 설정{condition ? " · 적용 중" : " (선택)"}
      </button>
      {open && catalog && (
        <div style={{ marginTop: 6 }}>
          <SentenceTree node={condition} catalog={catalog} symbols={symbols}
                        selfIndicators={selfIndicators} requiredType="condition"
                        onChange={onCondition} />
          <div style={{ marginTop: 6, fontSize: 13 }}>
            <label style={{ display: "block" }}>
              <input type="radio" checked={refresh === "each_rebalance"}
                     onChange={() => onRefresh("each_rebalance")} /> 매 리밸런싱마다 재선별 (동적)
            </label>
            <label style={{ display: "block" }}>
              <input type="radio" checked={refresh === "once_at_start"}
                     onChange={() => onRefresh("once_at_start")} /> 시작 시점에 한 번만 선별 (정적·바스켓 유지)
            </label>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            {refresh === "each_rebalance"
              ? `선택한 ${count}개 종목 중 매 리밸런싱일에 조건을 만족하는 종목만 후보가 됩니다.`
              : "모의/실전을 시작하는 시점의 데이터로 종목을 한 번 확정해 유지합니다. 백테스트는 백테스트 시작일 기준이라 실제 운용 종목과 다를 수 있습니다."}
          </p>
        </div>
      )}
    </div>
  );
}
