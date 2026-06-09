"""local 테스트가 *이 저장소*의 quant_core·localapp을 임포트하도록 보장 + 상태 격리.

모노레포 레이아웃(core·server·web·local 형제). 듀얼 워크트리나 editable install 환경에서
다른 워크트리(다른 브랜치)의 quant_core가 먼저 잡히는 것을 방지 — in-repo core·local을 sys.path
최우선에 둔다(단일 체크아웃 CI에선 무해).

⚠️ 상태 격리(필수): config.APP_DIR은 QP_LOCAL_DIR env가 없으면 ~/.quant-platform(실제
로컬앱 상태)로 기본설정된다. 격리 없이 테스트를 돌리면 order_log·trader가 사용자의 실제
ledger·orders·trades 파일에 써서 라이브 로컬앱을 오염시킨다. localapp.config import 전에
QP_LOCAL_DIR을 임시 디렉터리로 고정한다(이미 설정돼 있으면 존중 — CI override 허용).
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QP_LOCAL_DIR", tempfile.mkdtemp(prefix="qp-localtest-"))

_ROOT = Path(__file__).resolve().parents[2]   # platform/ (local/tests/ 의 조부모)
for _p in (str(_ROOT / "core"), str(_ROOT / "local")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
