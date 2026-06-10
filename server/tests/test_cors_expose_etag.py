"""CORS expose_headers — 교차출처 브라우저가 ETag를 읽을 수 있어야 한다.

웹(quantman.vercel.app)과 API(railway.app)는 다른 origin이라, 서버가
`Access-Control-Expose-Headers: ETag`를 보내지 않으면 브라우저 JS는
응답의 ETag 헤더를 읽지 못한다(CORS-safelisted 헤더가 아님).

실측 발견(2026-06-10, Neon egress 인시던트 라이브 검증 중): expose_headers
미설정으로 web/src/api.ts의 etagCache가 ETag를 저장하지 못해 If-None-Match를
한 번도 보내지 않았고, P0-1 ETag/304 최적화 전체가 프로덕션 웹에서 0회 동작
— 모든 폴이 full payload 200으로 떨어져 egress 폭증의 공범이었다.
(테스트는 same-origin TestClient라 통과했었음 — 이 테스트가 그 갭을 닫는다.)
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import main as appmain  # noqa: E402


def test_cors_exposes_etag_to_cross_origin_browser():
    """Origin 동반 요청 응답에 Access-Control-Expose-Headers: ETag 포함."""
    client = TestClient(appmain.app)
    r = client.get("/health", headers={"Origin": "https://quantman.vercel.app"})
    assert r.status_code == 200
    exposed = r.headers.get("access-control-expose-headers", "")
    assert "etag" in exposed.lower(), (
        f"expose_headers에 ETag 없음 (현재: {exposed!r}) — "
        "교차출처 브라우저가 ETag를 못 읽어 304 캐시 전체가 불발됨")
