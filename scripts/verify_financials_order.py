"""전 종목 재무제표 구조 전수 점검 — 원문 보고서-정확/폴백 여부 + 이상치 플래그.

저장된 financials JSON을 스캔(오프라인)해 회사별 PL/BS 구조를 점검한다:
- PL 첫 계정이 매출/영업수익/이자수익류인가(아니면 폴백·이상)
- BS에 자산총계/유동자산이 있고 첫 계정이 자산류인가
- 연도수(5개년 기대)·행수(원문=상세 다수, OpenAPI 폴백=소수)
요약 카운트 + 플래그 종목 리스트 출력. 표본은 --sample 로 원문 직접 대조.
"""
import json
import os
import sys

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "app", "data", "financials")
PL_HEAD = {"매출액", "영업수익", "수익(매출액)", "수익", "매출", "이자수익", "영업수익(매출액)", "보험손익", "영업이익"}
BS_HEAD = {"유동자산", "자산", "현금및현금성자산", "현금및예치금", "현금및예치금등", "자산총계"}


def _ns(s):
    return (s or "").replace(" ", "")


def _accts(stmt):
    return [r.get("account", "") for r in (stmt or {}).get("rows", [])]


def main():
    codes = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    n = len(codes)
    flags = []
    pl_ok = bs_ok = pl_doc = 0
    for code in codes:
        try:
            d = json.load(open(os.path.join(DIR, f"{code}.json"), encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            flags.append((code, f"load-fail {e}")); continue
        ann = d.get("annual", {})
        pl, bs = ann.get("PL", {}), ann.get("BS", {})
        pla, bsa = _accts(pl), _accts(bs)
        # PL 첫 실계정(이익률·% 행 제외)
        pl1 = next((a for a in pla if "률" not in a and not a.endswith("%")), "")
        bs1 = bsa[0] if bsa else ""
        issues = []
        if not pla:
            issues.append("PL없음")
        elif _ns(pl1) not in {_ns(x) for x in PL_HEAD}:
            issues.append(f"PL첫계정='{pl1}'")
        else:
            pl_ok += 1
        if not bsa:
            issues.append("BS없음")
        elif _ns(bs1) not in {_ns(x) for x in BS_HEAD} and not any("자산총계" in a for a in bsa[:3]):
            issues.append(f"BS첫계정='{bs1}'")
        else:
            bs_ok += 1
        # 원문-정확 추정(상세 계정 다수) vs 폴백(소수)
        if len(bsa) >= 25:
            pl_doc += 1
        np_ = len((pl.get("periods") or []))
        if np_ and np_ < 4:
            issues.append(f"연도{np_}개")
        if issues:
            flags.append((code, ", ".join(issues), f"PL{len(pla)}/BS{len(bsa)}행"))
    print(f"총 {n}종목 | PL정상 {pl_ok} | BS정상 {bs_ok} | BS상세(원문추정) {pl_doc} | 플래그 {len(flags)}")
    for f in flags[:80]:
        print("  ", f)


if __name__ == "__main__":
    main()
