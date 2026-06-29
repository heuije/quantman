"""자동매매 가능 여부 단일 진실원천(SSOT) — (브로커 × 모드 × 자산군) → Capability.

순수함수·네트워크 없음. 서버 게이트(strategies._assert_live_tradable)·웹 라벨·
로컬 런타임 가드가 모두 이 한 표를 읽어 "빌더 노출 ⊋ 게이트 통과 ⊋ 실제 체결"의
불일치를 없앤다(설계 §3.1).

상태:
  ok          — 완전 지원. 적용 가능.
  needs_setup — 적용 가능하나 유저 행동 필요(예: KIS 해외선물 실전 HTS 시세신청).
                비차단 — 시세 미신청이어도 전일종가 사이징+시장가로 발주는 동작.
  blocked     — 하드 차단. 적용 시 422 + reason.
verified: 라이브 검증 완료 셀(실전 무경고). 미검증은 실전 적용 시 경고-확인(게이트가 처리).

⚠ Phase 1: us_futures(CME 해외선물)는 배선 전이라 전 셀 blocked("준비 중"). Phase 2가 연다.
"""
from __future__ import annotations

from dataclasses import dataclass

BROKERS = ("kis", "ls")
MODES = ("paper", "live")
ASSET_CLASSES = ("kr_equity", "kr_futures", "us_equity", "us_futures")

# 실사용으로 검증된 셀(실전 무경고). 나머지는 모의 라운드트립 검증 후 승격.
_VERIFIED = frozenset({
    ("kis", "paper", "kr_equity"), ("kis", "live", "kr_equity"),
    ("kis", "paper", "us_equity"), ("kis", "live", "us_equity"),
})


@dataclass(frozen=True)
class Capability:
    status: str                 # "ok" | "needs_setup" | "blocked"
    verified: bool = False
    reason: str = ""            # blocked/needs_setup 사유(유저 메시지)
    setup_hint: str | None = None  # Phase 2: needs_setup 경로에서 채워질 예정


def autotrade_capability(broker: str, mode: str, asset_class: str) -> Capability:
    if broker not in BROKERS or mode not in MODES or asset_class not in ASSET_CLASSES:
        return Capability("blocked", reason="자동매매 미지원 대상입니다")

    verified = (broker, mode, asset_class) in _VERIFIED

    # 구조적 차단 셀
    if asset_class == "us_equity" and broker == "ls" and mode == "paper":
        return Capability("blocked", reason="LS 미국주식은 모의투자 미제공 — KIS 모의를 사용하세요")

    if asset_class == "us_futures":
        # Phase 1: 해외선물 배선 전 — 전 셀 보류. (Phase 2에서 LS ok / KIS live needs_setup)
        return Capability("blocked", reason="해외선물 자동매매는 준비 중입니다(곧 지원)")

    # kr_equity·kr_futures(전 셀) + us_equity(ls/paper 제외) → ok
    return Capability("ok", verified)
