"""server 테스트가 *이 저장소*의 quant_core·app을 임포트하도록 보장.

모노레포 레이아웃(core·server·web·local 형제). 듀얼 워크트리나 editable install 환경에서
다른 워크트리(다른 브랜치)의 quant_core가 먼저 잡히면 같은 저장소의 server↔core 짝이 어긋난다.
conftest는 pytest 수집 시점(테스트 모듈 임포트 전)에 실행되므로, in-repo core·server를 sys.path
최우선에 둬 항상 동일 트리 소스로 테스트한다(단일 체크아웃 CI에선 무해한 no-op).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]   # platform/ (server/tests/ 의 조부모)
for _p in (str(_ROOT / "core"), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
