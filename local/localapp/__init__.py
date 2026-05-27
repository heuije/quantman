import os as _os

from . import corepath  # noqa: F401  (import 시 core 경로 등록)
from .config import APP_DIR as _APP_DIR

# 로컬앱 버전 — GUI 표시·릴리스 zip 명명·release notes 와 같은 출처.
__version__ = "0.8.11-beta"

# core(quant_core) 데이터 저장 위치를 사용자 디렉터리로 — 번들 데이터를 쓰지 않고
# 로컬앱이 직접 최신 시세를 수집해 보관한다.
_os.environ.setdefault("QP_CORE_DATA_DIR", str(_APP_DIR / "data"))
