#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""로컬 데이터셋 Parquet 서버 동기화 도구 (네이버 차단 완벽 우회).

사용법:
  python local/sync_uploader.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# local 디렉터리를 sys.path에 추가하여 localapp 패키지를 로드할 수 있게 함
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

try:
    import pandas as pd
    import requests
    from localapp.config import PLATFORM_URL
    from localapp.secrets_store import load_device_token
    from localapp.sync_client import push_local_dataset
    from quant_core import data_fetcher
except ImportError as e:
    print(f"필수 패키지 로드 실패: {e}")
    print("의존성을 먼저 설치하세요: pip install pandas requests keyring")
    sys.exit(1)


def main():
    import logging
    # 로컬 콘솔 화면에 업로드 상황이 실시간 출력되도록 로깅 초기화
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 60)
    print(" [QuantPlatform Parquet 벌크 동기화 시스템] ")
    print("=" * 60)
    print(f"서버 주소: {PLATFORM_URL}")
    
    # 0. 로컬 데이터 경로 확인 및 자동 교정 (환경변수 덮어쓰기 대비)
    target_data_dir = data_fetcher.DATA_DIR
    repo_data_dir = Path(__file__).parent.parent.resolve() / "core" / "data"
    
    def count_parquet(d: Path) -> int:
        if not d.exists():
            return 0
        return len(list(d.glob("*.parquet")))
        
    n_target = count_parquet(target_data_dir)
    n_repo = count_parquet(repo_data_dir)
    
    if n_repo > n_target:
        print(f"💡 환경변수 경로({target_data_dir})보다 프로젝트 데이터 폴더({repo_data_dir})에 더 많은 Parquet 파일이 존재합니다.")
        print(f"   ({n_repo}개 vs {n_target}개) -> 더 많은 데이터가 있는 폴더로 자동 전환하여 동기화합니다.")
        target_data_dir = repo_data_dir
        
    print(f"로컬 데이터 경로: {target_data_dir} (총 {count_parquet(target_data_dir)}개 파일)")
    print("-" * 60)

    # 1. 기기 페어링 확인
    token = load_device_token()
    if not token:
        print("❌ 에러: 기기 페어링이 되어 있지 않습니다.")
        print("로컬앱 GUI를 실행해 기기 페어링을 먼저 진행하시거나,")
        print("secrets_store에 토큰을 설정해야 합니다.")
        sys.exit(1)

    print("✅ 페어링 토큰 확인 완료.")
    print("동기화를 시작합니다. 파일 수가 많으면 수 분 정도 소요될 수 있습니다...")
    print("-" * 60)

    start_time = time.time()
    try:
        # 동기화 실행
        result = push_local_dataset(target_data_dir)
        
        elapsed = time.time() - start_time
        print("-" * 60)
        print("🎉 동기화 프로세스 완료!")
        print(f"소요 시간: {elapsed:.2f}초")
        print(f"총 분석 파일: {result['total']}개")
        print(f"성공 업로드: {result['uploaded']}개")
        print(f"최신 유지(생략): {result['skipped']}개")
        print(f"실패: {result['failed']}개")
        print("=" * 60)
        
        if result['failed'] > 0:
            print("⚠️ 일부 파일의 업로드가 실패했습니다. 다시 실행하면 실패한 파일만 골라 재전송합니다.")
        else:
            print("✨ 모든 데이터셋이 서버에 완벽하고 영구적으로 동기화되었습니다! 즉시 백테스트가 가능합니다.")
            
    except KeyboardInterrupt:
        print("\n🛑 사용자에 의해 강제 종료되었습니다.")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 치명적 오류 발생: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
