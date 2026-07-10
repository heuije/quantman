"""대체데이터(컨센서스·기관수급) DataSpec 등록 계약 — P0.

spec.py 레지스트리가 인사이트 엔진이 소비할 **컬럼 계약(provides)**과 PIT 메타를
선언하는지 고정한다. 실제 데이터 흐름(feed·cron·indicators 배선)은 후속 P1~P3 —
이 단계에서는 진실원천 레지스트리의 *선언*만 검증한다(데이터 적재 전이라 absent).

    cd core && pytest tests/test_altdata_spec.py -v
"""
from quant_core.data import spec


def test_pclass_flow_exists():
    """수급은 PRICE/FUNDAMENTAL/MACRO 어디에도 안 맞는 독립 분류 → P7 신설."""
    assert spec.PClass.FLOW.value == "P7"


def test_flow_kr_investor_registered():
    s = spec.get("flow.kr_investor")
    assert s is not None, "flow.kr_investor 미등록"
    assert s.pclass == spec.PClass.FLOW
    assert s.frequency == "daily"
    assert s.point_in_time is True, "as_of ffill·look-ahead 차단 위해 PIT 필수"
    # 원시 일별 컬럼만 — 연속/누적은 시계열 연산자 조합(직교 프리미티브)
    assert "inst_net_buy" in s.provides and "foreign_net_buy" in s.provides
    assert "as_of" in s.required_meta


def test_consensus_contract_columns():
    """estimate.consensus가 네이버(reports_kr) 연동으로 일별 컨센서스 컬럼 계약을 선언."""
    s = spec.get("estimate.consensus")
    assert s is not None
    for col in ("consensus_target", "consensus_target_median", "analyst_count",
                "consensus_opinion", "target_dispersion", "target_revision_pct",
                "days_since_report"):
        assert col in s.provides, f"컨센서스 컬럼 계약 누락: {col}"
    assert s.point_in_time is True
    assert "as_of" in s.required_meta
    assert s.source and "미연동" not in s.source, "source가 네이버로 갱신돼야"


def test_data_spec_serializes_new_feeds():
    """data_spec() 직렬화(프론트·문서·cron 소비)에 신규 피드가 포함되고 무예외."""
    rows = spec.data_spec()
    keys = {row["key"] for row in rows}
    assert {"flow.kr_investor", "estimate.consensus"} <= keys
    for row in rows:
        assert isinstance(row["provides"], list)
        assert isinstance(row["required_meta"], list)
