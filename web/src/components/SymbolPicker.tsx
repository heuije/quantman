import { useEffect, useRef, useState } from "react";
import type { SymbolInfo } from "../types";

export const SYMBOL_CAT_ORDER =
  ["자산", "변동성", "금리·환율", "신용", "거시지표", "심리", "개별종목"];

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
  items: { key: string; label: string; cat: string }[];
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

  return (
    <div className="cat-list">
      {cats.length === 0 && <div className="cat-empty">결과 없음</div>}
      {cats.map((cat) => (
        <div key={cat}>
          <div className="cat-head">{cat}</div>
          {byCat[cat].map((it) => (
            <button
              key={it.key} type="button"
              className={"cat-item" + (it.key === selected ? " sel" : "")}
              onClick={() => onPick(it.key)}
            >
              {it.label}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

/** 종목 선택 칩 — 클릭 시 검색·카테고리 팝오버가 열린다. */
export default function SymbolPicker({ symbols, value, tradableOnly, onChange }: {
  symbols: SymbolInfo[];
  value: string;
  tradableOnly?: boolean;
  onChange: (sym: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);

  const list = symbols.filter((s) => s.indicators.length > 0
    && (!tradableOnly || s.tradable));

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {value || "종목 선택"}
        <span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover">
          <input
            className="pop-search" placeholder="종목 검색…" autoFocus
            value={search} onChange={(e) => setSearch(e.target.value)}
          />
          <div className="op-label">종목</div>
          <CategoryList
            items={list.map((s) => ({ key: s.symbol, label: s.symbol, cat: s.category }))}
            order={SYMBOL_CAT_ORDER}
            selected={value}
            search={search}
            onPick={(k) => { onChange(k); setOpen(false); }}
          />
        </div>
      )}
    </span>
  );
}
