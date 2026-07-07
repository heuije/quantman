import { useEffect, useState } from "react";

// 테마(다크/라이트) — html[data-theme] + qp_theme(localStorage) 영속.
// 초기 적용은 index.html의 인라인 스크립트가 렌더 전에 수행(FOUC 방지). 이 훅은 토글·구독만.
export type Theme = "dark" | "light";
const KEY = "qp_theme";

function current(): Theme {
  const t = document.documentElement.getAttribute("data-theme");
  return t === "light" ? "light" : "dark";
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(current);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch { /* 무시 */ }
  }, [theme]);
  const toggle = () => setThemeState((t) => (t === "dark" ? "light" : "dark"));
  return { theme, toggle, setTheme: setThemeState };
}
