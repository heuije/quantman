"""온보딩 상태 결정 — 위젯 무의존 순수 로직(테스트 가능).

GUI(_render_setup_area)는 이 결정의 렌더러일 뿐. 분리로 조합 매트릭스를 전수 테스트한다
(기존엔 위젯과 결합돼 무검증이라 '선물 단독 stuck' 버그가 ship됨)."""
from __future__ import annotations


def decide_setup_mode(broker: str, ready: bool, dev_ok: bool, collapsed: bool) -> str:
    """온보딩 화면 모드 결정. 반환: normal | wizard_kis | wizard_ls | wizard_pair.

    ready = 그 브로커 자산군 슬롯 ≥1(secrets_store.broker_ready). 기존 _render_setup_area
    로직과 동치이되 ready를 '주식 슬롯'이 아니라 '자산군 슬롯 집합'으로 받는다."""
    wizard = "wizard_ls" if broker == "ls" else "wizard_kis"
    if not ready:
        return wizard               # 자격증명 미등록 → 해당 브로커 입력 폼
    if not dev_ok:
        return "wizard_pair"        # 자격증명 OK, 페어링 필요
    if collapsed:
        return "normal"             # 둘 다 완료 + 접힘 → 정상 운영
    return wizard                   # 둘 다 OK인데 ⚙ 펼침 → 자격증명 변경
