"""데이터 수급·무결성 계층 (atomic_block_spec_v2 기반 전략에 정합).

이 패키지는 백테스트 데이터의 **요구 정의(DataSpec)**·실측 메타(DataManifest)·
전략요소→데이터 의존성을 담는다. quant_core 순수성을 지키기 위해 서버 DB에
의존하지 않는다(무결성 머신은 사이드카 manifest 파일만 읽는다).

  spec.py      — DataSpec: 피드 단위 요구 정의(유형·깊이·메타·가공·출처). 단일 출처.
"""

from .spec import (  # noqa: F401
    REGISTRY, Adjustment, DataTypeSpec, Derivation, Frequency, PClass,
    data_spec, get, register,
)
from .manifest import (  # noqa: F401
    DataManifest, FeedManifest, SymbolManifest, build_manifest, coverage_report,
    default_manifest_path, load_manifest, save_manifest,
)
from .deps import required_data  # noqa: F401
from .gate import evaluate_data_soundness  # noqa: F401

__all__ = [
    "DataTypeSpec", "PClass", "Frequency", "Adjustment", "Derivation",
    "REGISTRY", "register", "get", "data_spec",
    "DataManifest", "FeedManifest", "SymbolManifest", "build_manifest",
    "load_manifest", "save_manifest", "default_manifest_path", "coverage_report",
    "required_data", "evaluate_data_soundness",
]
