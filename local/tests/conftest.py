"""local 테스트가 *이 저장소*의 quant_core·localapp을 임포트하도록 보장.

모노레포 레이아웃(core·server·web·local 형제). 듀얼 워크트리나 editable install 환경에서
다른 워크트리(다른 브랜치)의 quant_core가 먼저 잡히는 것을 방지 — in-repo core·local을 sys.path
최우선에 둔다(단일 체크아웃 CI에선 무해).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]   # platform/ (local/tests/ 의 조부모)
for _p in (str(_ROOT / "core"), str(_ROOT / "local")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
