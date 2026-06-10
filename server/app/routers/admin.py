"""운영(admin) 엔드포인트 — 수동 데이터 변경 후 캐시를 강제 동기화.

데이터셋 캐시는 세대 마커로 cron·manage·백필(별도 프로세스) 변경을 자가 감지하지만,
마커 경로를 우회한 변경(예: parquet 파일 직접 삭제·편집)은 감지하지 못한다.
그런 수동 작업 후 즉시 반영하려면 이 엔드포인트로 강제 무효화한다.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from quant_core import data_fetcher
from sqlmodel import Session, select

from .. import data_cache
from ..db import get_session
from ..deps import get_current_user
from ..models import CompileLog, User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/invalidate")
def invalidate_dataset_cache(user: User = Depends(get_current_user)):
    """데이터셋 캐시 강제 무효화 + 세대 마커 bump.

    수동 데이터 변경(파일 직접 삭제·편집) 후 호출하면 다음 읽기에 parquet에서 재로드한다.
    마커 bump로 다른 프로세스/레플리카의 캐시도 자가 무효화된다."""
    data_cache.invalidate()
    return {"ok": True, "generation": data_fetcher.data_generation()}


@router.get("/compile-stats")
def compile_stats(user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """NL→IR 컴파일 정확도 집계 — 개선 flywheel의 신호.

    실패가 몰리는 위치(top_fail_paths)·규칙(top_fail_rules)을 보고 프롬프트·few-shot·스키마를
    어디에 보강할지 데이터로 결정한다. 베타 규모(수십~수백 건)라 전건 로드 후 Python 집계로 충분
    (행이 크게 늘면 SQL 집계로 전환). edited_rate는 검증 통과 IR의 *의미* 정확도(수정 없이 실행=정확)."""
    rows = session.exec(select(CompileLog)).all()
    total = len(rows)
    ok = [r for r in rows if r.ok]
    ran = [r for r in rows if r.ran]
    edited = [r for r in ran if r.edited]
    paths, rules = Counter(), Counter()
    for r in rows:
        if r.ok:
            continue
        for i in (r.issues or []):
            if isinstance(i, dict) and i.get("is_error"):
                paths[i.get("path", "?")] += 1
                rules[i.get("rule", "?")] += 1
    return {
        "total": total,
        "success_rate": round(len(ok) / total, 3) if total else None,
        "mean_repairs_all": round(sum(r.repair_count for r in rows) / total, 2) if total else None,
        "mean_repairs_ok": round(sum(r.repair_count for r in ok) / len(ok), 2) if ok else None,
        "ran": len(ran),
        "edited_after_run": len(edited),
        "edited_rate": round(len(edited) / len(ran), 3) if ran else None,
        "top_fail_paths": paths.most_common(10),
        "top_fail_rules": rules.most_common(10),
    }
