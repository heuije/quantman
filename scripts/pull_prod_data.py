"""프로덕션 dataset 번들을 로컬로 pull — API 비용 0 로컬 시각/데이터 검증 테스트 환경(Part A).

프로덕션 `GET /dataset/bundle?scope=full`을 로컬앱과 동일 경로
(`localapp.sync_client.fetch_dataset_bundle`, scope="full")로 내려받아 지정 DATA_DIR에 푼다.
인증은 로컬앱 디바이스 토큰(OS 키링) 재사용 — 새 자격증명 불요.

**scope="full"** 을 쓰는 이유: 자동매매 로컬앱은 price+펀더멘털만 소비하므로 기본 번들
(scope="trading")엔 서버 챗봇 전용 피드(flow·시총·공매도·13F)가 없다. dev 테스트환경의
localhost 챗봇은 이 피드까지 소비하므로, full 스코프로 받아야 **프로덕션 볼륨과 동일한
전 모달리티** 데이터가 된다(외국인 순매수 등 챗봇 쿼리가 로컬에서도 프로덕션급으로 동작).

로컬 실행 경로(`load_dataset_for`)가 그 parquet을 읽어 프로덕션과 동일 데이터를 쓴다.
즉 로컬 하니스·서버·엔진이 프로덕션과 같은 데이터로 동작.

사용:
    python scripts/pull_prod_data.py                       # 기본 ~/.quant-platform/dev-data
    python scripts/pull_prod_data.py --data-dir D:/qp-data # 임의 경로

그 후 그 경로를 QP_CORE_DATA_DIR로 지정해 실행:
    QP_CORE_DATA_DIR=~/.quant-platform/dev-data python -m app.main   (서버)
    QP_CORE_DATA_DIR=~/.quant-platform/dev-data python scripts/dump_results.py   (Part B1)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "local"))


def main() -> int:
    ap = argparse.ArgumentParser(description="프로덕션 dataset 번들 로컬 pull(테스트 환경)")
    ap.add_argument("--data-dir", default=str(Path.home() / ".quant-platform" / "dev-data"),
                    help="parquet을 풀 로컬 경로(기본 ~/.quant-platform/dev-data)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    from localapp.config import PLATFORM_URL
    from localapp.secrets_store import load_device_token
    if not load_device_token():
        print("✗ 디바이스 토큰 없음 — 로컬앱을 1회 실행·로그인해 디바이스를 등록하세요.", file=sys.stderr)
        return 2

    print(f"프로덕션 {PLATFORM_URL}/dataset/bundle?scope=full → {data_dir}")
    from localapp.sync_client import fetch_dataset_bundle
    result = fetch_dataset_bundle(data_dir, scope="full")
    if result.get("skipped"):
        print("✓ ETag 일치 — 이미 최신(다운로드 skip)")
    else:
        print(f"✓ 다운로드·추출 완료: {result}")

    pq = list(data_dir.glob("*.parquet"))
    total_mb = sum(p.stat().st_size for p in pq) / 1e6
    print(f"✓ 로컬 parquet {len(pq)}개 · {total_mb:.1f} MB")
    for s in ("005930", "S&P500", "코스피200선물"):
        exists = (data_dir / f"{s}.parquet").exists()
        print(f"    {s}.parquet: {'있음' if exists else '없음'}")
    # full 스코프 서버 전용 피드 도착 확인 — 챗봇이 로컬에서 프로덕션급으로 동작하는지의 핵심.
    for sub in ("fundamentals", "flow", "marketcap", "short_volume", "institutional"):
        n = len(list((data_dir / sub).glob("*.parquet"))) if (data_dir / sub).exists() else 0
        print(f"    {sub}/: {n}개")
    print(f"\n다음: QP_CORE_DATA_DIR={data_dir} 로 지정해 서버·하니스 실행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
