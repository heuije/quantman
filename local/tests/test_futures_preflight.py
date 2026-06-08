"""futures_preflight 진단 스크립트 스모크 — import 정상 + 자격증명 없을 때 graceful exit.

preflight는 네트워크·KIS 자격증명·장중에 의존하는 수동 진단 도구라 전체 경로는 라이브에서만
검증된다. 다만 *구문/임포트 붕괴*나 *no-creds 가드 회귀*는 희소한 라이브 세션을 낭비시키므로
사전에 막는다(이 스모크가 그 역할).

    cd platform/local && python -m pytest tests/test_futures_preflight.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent   # tests → local
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest


def test_preflight_imports_clean():
    """구문/임포트 정상 — 라이브 세션 전 깨짐 방지."""
    import futures_preflight  # noqa: F401
    assert hasattr(futures_preflight, "main")
    assert hasattr(futures_preflight, "overseas_readonly")


def test_domestic_no_creds_exits(monkeypatch):
    """국내 자격증명 없으면 명시 메시지 후 sys.exit(1) (조용한 진행 금지)."""
    import futures_preflight as pf
    monkeypatch.setattr(pf, "load_kis_futures", lambda: None)
    monkeypatch.setattr(sys, "argv", ["futures_preflight.py"])
    with pytest.raises(SystemExit):
        pf.main()


def test_overseas_no_creds_exits(monkeypatch):
    """해외 자격증명 없으면 명시 메시지 후 sys.exit(1)."""
    import futures_preflight as pf
    monkeypatch.setattr(pf, "load_kis_overseas_futures", lambda: None)
    with pytest.raises(SystemExit):
        pf.overseas_readonly()
