/**
 * 수동 다중 종목 선택기 — 1개부터 N개까지.
 *
 * 선택된 종목을 칩 그리드로 표시하고 X로 제거. 새 종목 추가 버튼 클릭 시
 * 시장별 탭 + 검색 팝오버가 열린다.
 *
 * value는 콤마 구분 문자열 ("005930,000660,035420") — backend Strategy 스키마와 동일.
 */

import { useState } from "react";
import type { SymbolInfo } from "../types";
import { usePopoverDismiss } from "./SymbolPicker";
import TabbedSymbolList from "./TabbedSymbolList";

const TRADABLE_TAB_ORDER = [
  "KOSPI", "KOSDAQ",
  "미국 NASDAQ", "미국 NYSE", "미국 AMEX",
  "일본", "홍콩",
];

function categoryFor(cat: string): string {
  if (cat.includes("KOSPI")) return "KOSPI";
  if (cat.includes("KOSDAQ")) return "KOSDAQ";
  if (cat.includes("NASDAQ")) return "미국 NASDAQ";
  if (cat.includes("NYSE")) return "미국 NYSE";
  if (cat.includes("AMEX")) return "미국 AMEX";
  if (cat.startsWith("일본")) return "일본";
  if (cat.startsWith("홍콩")) return "홍콩";
  return cat;
}

export default function MultiSymbolPicker({ symbols, value, onChange }: {
  symbols: SymbolInfo[];
  value: string;                   // 콤마 분리 — "005930,000660"
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLDivElement>(open, setOpen);

  const selected = value.split(",").map((s) => s.trim()).filter(Boolean);
  const list = symbols.filter((s) => s.tradable);

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

  // 선택된 종목도 리스트에 유지 — 체크 해제로 빼낼 수 있게.
  const items = list.map((s) => ({
    key: s.symbol,
    label: s.name ? `${s.symbol} ${s.name}` : s.symbol,
    cat: categoryFor(s.category),
    badge: s.has_backtest_data === false ? "백테스트 불가" : undefined,
  }));

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
            order={TRADABLE_TAB_ORDER}
            placeholder="종목명 또는 코드 검색…"
            multiSelect
            selectedKeys={selected}
            onPick={toggle}
          />
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
