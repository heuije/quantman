/* 탭 + 검색 가능한 종목 리스트.
 * 카테고리를 탭으로, 종목을 탭 안에서 검색·선택.
 * SymbolPicker (매수 대상)와 OperandEditor (매수 조건)에서 공유.
 */

import { useMemo, useState } from "react";

export interface TabItem {
  key: string;          // 고유 ID (예: symbol code)
  label: string;        // 표시명 (예: "005930 삼성전자")
  cat: string;          // 탭 (카테고리) 이름
  badge?: string;       // 우측 배지
}

interface Props {
  items: TabItem[];
  order: string[];               // 탭 표시 순서
  selected?: string;
  onPick: (key: string) => void;
  emptyMessage?: string;         // 탭에 항목 없을 때 표시
  placeholder?: string;
}

const PER_PAGE = 60;

export default function TabbedSymbolList({
  items, order, selected, onPick, emptyMessage, placeholder,
}: Props) {
  // 카테고리별 집계
  const grouped = useMemo(() => {
    const m: Record<string, TabItem[]> = {};
    for (const it of items) (m[it.cat] ??= []).push(it);
    return m;
  }, [items]);

  // 탭 순서: order에 정의된 것 먼저, 그 외는 알파벳
  const tabs = useMemo(() => {
    const ordered = order.filter((c) => grouped[c]?.length);
    const rest = Object.keys(grouped)
      .filter((c) => !order.includes(c))
      .sort();
    return [...ordered, ...rest];
  }, [grouped, order]);

  const [activeTab, setActiveTab] = useState<string>(tabs[0] ?? "");
  const [search, setSearch] = useState("");

  // 선택된 종목이 어느 탭에 있나 — 진입 시 그 탭으로 점프
  const selTab = items.find((it) => it.key === selected)?.cat;
  if (selTab && !activeTab) setActiveTab(selTab);

  const currentTab = activeTab || tabs[0] || "";
  const currentItems = grouped[currentTab] ?? [];

  const q = search.trim().toLowerCase();
  const filtered = q
    ? currentItems.filter((i) => i.label.toLowerCase().includes(q)
                              || i.key.toLowerCase().includes(q))
    : currentItems;
  const shown = filtered.slice(0, PER_PAGE);
  const hidden = filtered.length - shown.length;

  if (tabs.length === 0) {
    return <div className="cat-empty" style={{ padding: 16 }}>
      {emptyMessage ?? "표시할 종목이 없습니다."}
    </div>;
  }

  return (
    <div className="tabbed-list">
      <div className="tabbed-tabs">
        {tabs.map((t) => (
          <button
            key={t} type="button"
            className={"tabbed-tab" + (t === currentTab ? " active" : "")}
            onClick={() => { setActiveTab(t); setSearch(""); }}
          >
            <span>{t}</span>
            <span className="tabbed-tab-n">{grouped[t].length}</span>
          </button>
        ))}
      </div>
      <input
        className="pop-search" placeholder={placeholder ?? "검색…"}
        value={search} onChange={(e) => setSearch(e.target.value)}
      />
      <div className="tabbed-items">
        {shown.length === 0 && <div className="cat-empty">결과 없음</div>}
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
    </div>
  );
}
