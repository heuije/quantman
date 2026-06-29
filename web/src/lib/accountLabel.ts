/**
 * accountLabel — 전략의 account_ref를 비민감 핸들 목록에서 룩업 (P5-4).
 *
 * 카드·상세에서 "이 전략은 어느 계좌로 실행되나"를 표시하기 위한 공용 헬퍼.
 * account_ref가 null이거나 핸들 목록에 없는(레거시·해제된 계좌) 경우 null을 돌려
 * 호출자가 "계좌 미선택"으로 표시하게 한다.
 */

import type { AccountHandle } from "../types";

export function accountLabel(
  accountRef: string | null | undefined,
  handles: AccountHandle[],
): { nickname: string; mode: "paper" | "live" } | null {
  if (!accountRef) return null;
  const h = handles.find((x) => x.account_id === accountRef);
  if (!h) return null;
  return { nickname: h.nickname, mode: h.mode };
}
