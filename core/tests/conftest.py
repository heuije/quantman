"""core 테스트가 *이 저장소*의 quant_core를 임포트하도록 보장.

server/tests/conftest.py와 같은 부류 방어의 core판 — 듀얼 워크트리·editable install 환경에서
core/tests를 단독 실행하면(예: `pytest core/tests/test_x.py`) 설치된 다른 워크트리(다른 브랜치)의
quant_core가 먼저 잡혀 **다른 코드를 테스트**한다(실측: pre-계약 브랜치가 잡혀 red/green 반전).
결합 실행(server 포함) 때만 server conftest의 전역 sys.path 부작용으로 우연히 맞던 것을,
core 단독 실행에서도 항상 같은 트리로 고정한다(단일 체크아웃 CI에선 무해한 no-op).
"""
import sys
from pathlib import Path

_CORE = str(Path(__file__).resolve().parents[1])   # <repo>/core
if _CORE in sys.path:
    sys.path.remove(_CORE)
sys.path.insert(0, _CORE)

# 이미 다른 트리의 quant_core가 임포트된 상태로 진입했다면(플러그인 등) 제거해 재해석.
for _m in [m for m in list(sys.modules) if m == "quant_core" or m.startswith("quant_core.")]:
    _f = getattr(sys.modules[_m], "__file__", "") or ""
    if _CORE not in _f:
        del sys.modules[_m]
