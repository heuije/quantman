/** 공통 수치 포맷 — 모든 수치는 소수점 2자리까지, 정수면 .00 생략.
 *
 * fmt2(123.456) → "123.46"
 * fmt2(10)      → "10"
 * fmt2(0.5)     → "0.5"
 * fmt2(null)    → "-"
 */
export function fmt2(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "-";
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** 원 금액 — 만원/억원 단위로 사람이 읽기 쉽게. */
export function wonReadable(n: number): string {
  if (n >= 1e8) return `${fmt2(n / 1e8)}억원`;
  if (n >= 1e4) return `${fmt2(n / 1e4)}만원`;
  return `${fmt2(n)}원`;
}

/** 라틴 문자 → 한글 글자읽기 (S→에스, K→케이 …). 회사별 하드코딩 없이 26자로 일반화. */
const _LATIN_KO: Record<string, string> = {
  a: "에이", b: "비", c: "씨", d: "디", e: "이", f: "에프", g: "지", h: "에이치",
  i: "아이", j: "제이", k: "케이", l: "엘", m: "엠", n: "엔", o: "오", p: "피",
  q: "큐", r: "알", s: "에스", t: "티", u: "유", v: "브이", w: "더블유", x: "엑스",
  y: "와이", z: "지",
};

/** 검색 정규화 — 영문↔국문 양방향 매칭용 정규형.
 * KIS 종목명은 영문 약칭을 한글음차한다(예: SK하이닉스→"에스케이하이닉스"). 검색어·종목명을
 * 모두 라틴문자→한글 글자읽기로 변환해 한 정규형으로 맞추면 "sk하이닉스"와 "에스케이하이닉스"가
 * 같은 값이 되어 양방향 검색된다(공백·기호 제거, 소문자화). */
export function searchNorm(s: string | null | undefined): string {
  return (s || "").toLowerCase().replace(/[^a-z0-9가-힣]/g, "")
    .replace(/[a-z]/g, (c) => _LATIN_KO[c] || c);
}

/** 종목명/코드가 검색어에 매칭되는지 — 영문·국문 양방향. 원문 부분일치(기존 동작) +
 * 글자읽기 정규화 매칭을 함께 본다. query 대소문자/공백은 내부에서 처리. */
export function stockSearchMatch(name: string, code: string, query: string): boolean {
  const ql = (query || "").trim().toLowerCase();
  if (!ql) return true;
  return code.toLowerCase().includes(ql)
    || name.toLowerCase().includes(ql)
    || searchNorm(name).includes(searchNorm(query));
}
