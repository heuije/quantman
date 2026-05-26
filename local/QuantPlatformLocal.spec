# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 빌드 스펙 — 로컬앱 데스크탑 GUI(onedir).

로컬앱이 실제로 쓰는 것만 번들한다: pandas·numpy·pyarrow·yfinance·
FinanceDataReader·requests·keyring·apscheduler·pystray·tkinter.
torch·cv2·transformers·scipy 등 미사용 대형 패키지는 제외해 용량을 줄인다.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath("../core"))

from PyInstaller.utils.hooks import collect_all, collect_data_files

# localapp 버전을 __init__.py에서 직접 파싱 — onedir 출력 폴더명에 사용해
# 사용자가 압축 풀었을 때 어느 버전인지 폴더명만 봐도 알도록 한다.
# (zip 파일명은 패키징 단계에서 동일 명규로 부여.)
_INIT_PY = Path(__file__).parent / "localapp" / "__init__.py"
_m = re.search(r'__version__\s*=\s*"([^"]+)"', _INIT_PY.read_text(encoding="utf-8"))
if not _m:
    raise RuntimeError(f"__version__ 파싱 실패: {_INIT_PY}")
_VERSION = _m.group(1)
_BUNDLE_NAME = f"QuantPlatformLocal-v{_VERSION}"

datas, binaries, hiddenimports = [], [], []

# 동적 import·백엔드가 있는 (작은) 패키지만 통째로 수집
for pkg in ("keyring", "pystray", "apscheduler", "FinanceDataReader", "zstandard"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# quant_core 데이터 파일(시장 캘린더·유니버스 JSON)을 번들에 포함.
# onedir 패키지엔 .py만 들어가므로 이걸 빠뜨리면 calendars/us_sessions.json이 없어
# 미국 스케줄러(_plan_us_session → market_calendar)가 CalendarError로 동작 불가.
# 포함 대상: quant_core/calendars/*.json, quant_core/universe/*.json
datas += collect_data_files("quant_core")

hiddenimports += [
    "pystray._win32",
    "keyring.backends.Windows",
    "win32timezone",
    "quant_core",
]

# 로컬앱이 쓰지 않는 대형/불필요 패키지 — 번들에서 제외
EXCLUDES = [
    "torch", "torchvision", "torchaudio", "functorch",
    "cv2", "transformers", "tokenizers", "onnx", "onnxruntime",
    "sklearn", "scipy", "numba", "llvmlite", "statsmodels", "jieba",
    "tensorflow", "keras",
    "matplotlib", "streamlit", "IPython", "pytest", "_pytest",
    "notebook", "jupyter", "jupyterlab", "jupyter_server",
    "psycopg", "psycopg2", "psycopg_binary",
    "sqlalchemy", "sqlmodel", "fastapi", "starlette", "uvicorn",
    "pythonwin",
]

a = Analysis(
    ["desktop.py"],
    pathex=[".", "../core"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="QuantPlatformLocal",
    console=False,            # GUI 앱 — 콘솔 창 없음
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    name=_BUNDLE_NAME,   # dist/QuantPlatformLocal-v{version}/ — 폴더명만 봐도 버전 식별.
)
