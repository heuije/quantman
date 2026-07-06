"""HTTP 서빙 캐시의 영속 경로 — 재배포에도 살아남게 한다.

Railway 컨테이너 파일시스템은 에페메랄이라, 앱 폴더(server/app/data/)에 쓴 캐시는
**재배포마다 증발**한다. 그 결과 배포 직후 첫 방문자가 재무제표(DART XBRL 리포트당 ~8초×7)·
추정실적·기업개요를 매번 다시 크롤해 HOME이 30~120초까지 걸렸다(측정).

프로덕션은 영속 볼륨(`QP_CORE_DATA_DIR`=/srv/data)을 마운트하므로, HTTP 서빙 캐시를 그 볼륨
하위(`serving_cache/`)에 두어 재배포에도 살아남게 한다. 종목당 한 번 크롤하면 이후 모든 요청·
재배포에 즉시 서빙된다. 볼륨 미설정(로컬 dev)은 기존 앱 폴더로 폴백 — 동작 동일, 위치만 이동.

⚠ 코어 데이터 볼륨(parquet 피드)과 같은 마운트를 공유하되 `serving_cache/` 네임스페이스로 분리해
코어 피드(flow·consensus·fundamentals 등)와 섞이지 않게 한다.
"""
import os

_APP_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def serving_cache_path(*parts: str) -> str:
    """`{볼륨 또는 앱폴더}/serving_cache/<parts...>`. 볼륨(prod)이면 재배포 영속."""
    base = os.environ.get("QP_CORE_DATA_DIR") or _APP_DATA
    return os.path.join(base, "serving_cache", *parts)
