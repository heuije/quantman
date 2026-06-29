/** 전략 심볼 목록 → 자동매매 자산군 집합(중복·null 제거). AccountPicker capability 필터용. */
export function dedupeAssetClasses(syms: string[], map: Map<string, string | null>): string[] {
  const acs = syms.map((s) => map.get(s) ?? null).filter((ac): ac is string => ac != null);
  return [...new Set(acs)];
}
