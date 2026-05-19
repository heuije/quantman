"""quant_core 패키지를 import 가능하게 sys.path에 추가한다 (개발 모드)."""

import pathlib
import sys

_CORE = pathlib.Path(__file__).resolve().parents[2] / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
