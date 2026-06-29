/**
 * AccountPicker — 전략 "적용" 시 실행할 계좌 핸들을 명시적으로 고르는 모달 (P5-4).
 *
 * 비민감 핸들(account_id·별명·broker·mode·자산군)만 다룬다(INV-SEC: 계좌번호·자격증명 없음).
 * 선택한 핸들의 mode가 전략 run_mode, account_id가 account_ref가 된다(호출자가 처리).
 *
 * 구조·토큰은 DESIGN.md §7 따름: 표준 .modal(센터 모달 + backdrop) + 행은 .account-menu button
 * 패턴 재사용. mode 배지는 .sc-badge.paper/.live. 인라인 hex 없음 — var(--*) 토큰만.
 */

import type { AccountHandle } from "../types";

// 자산군 코드 → 한글 라벨(표시 전용). 미정의 코드는 원문 노출(은폐보다 정직).
const ASSET_CLASS_LABEL: Record<string, string> = {
  kr_equity: "국내주식",
  kr_futures: "국내선물",
  us_futures: "해외선물",
};

const BROKER_LABEL: Record<string, string> = { kis: "KIS", ls: "LS" };

function assetClassesLabel(classes: string[]): string {
  return classes.map((c) => ASSET_CLASS_LABEL[c] ?? c).join(", ");
}

interface Props {
  handles: AccountHandle[];
  activeIds: string[];
  currentRef?: string | null;
  onSelect: (h: AccountHandle) => void;
  onClose: () => void;
}

export default function AccountPicker({
  handles,
  activeIds,
  currentRef,
  onSelect,
  onClose,
}: Props) {
  function pick(h: AccountHandle) {
    if (h.mode === "live") {
      const ok = window.confirm(
        `실전 계좌 '${h.nickname}'로 적용합니다 — 다음 사이클부터 실제 자금으로 거래됩니다. 계속할까요?`,
      );
      if (!ok) return;
    }
    onSelect(h);
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal account-picker"
        role="dialog"
        aria-modal="true"
        aria-label="실행할 계좌 선택"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2>실행할 계좌 선택</h2>
          <button className="x-btn" aria-label="닫기" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          {handles.length === 0 ? (
            <div className="account-picker-empty">
              로컬앱에서 계좌를 등록·페어링하면 여기에 표시됩니다.
            </div>
          ) : (
            <div className="account-menu account-picker-list">
              {handles.map((h) => {
                const isActive = activeIds.includes(h.account_id);
                const isBound = currentRef != null && h.account_id === currentRef;
                return (
                  <button
                    key={h.account_id}
                    className={"account-picker-row" + (isBound ? " bound" : "")}
                    onClick={() => pick(h)}
                  >
                    <span
                      className={"account-picker-active " + (isActive ? "on" : "off")}
                      aria-hidden="true"
                    >
                      {isActive ? "●" : "○"}
                    </span>
                    <span className="account-picker-info">
                      <span className="account-picker-name">
                        {h.nickname}
                        {isBound && (
                          <span className="account-picker-bound-tag">현재 바인딩</span>
                        )}
                      </span>
                      <span className="account-picker-sub">
                        {BROKER_LABEL[h.broker] ?? h.broker}
                        {h.asset_classes.length > 0 &&
                          ` · ${assetClassesLabel(h.asset_classes)}`}
                        {isActive && " · 현재 활성"}
                      </span>
                    </span>
                    <span className={"sc-badge " + h.mode}>
                      {h.mode === "live" ? "실전" : "모의"}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="modal-foot">
          <button className="ghost" onClick={onClose}>
            닫기
          </button>
        </div>
      </div>
    </div>
  );
}
