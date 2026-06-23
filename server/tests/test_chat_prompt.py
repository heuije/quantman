"""챗봇 시스템 프롬프트 빌드·내용 가드.

chat_system_prompt는 {caps}/{cols} 보간이 든 f-string이라 미이스케이프 중괄호 회귀 위험이 있고
(test_ir_compiler_prompt와 동일 부류), 결과 독해 규율(<reading_results>)이 빠지면 라이브에서
"엔진이 연도별을 안 준다"·"비용 확인 불가" 오답이 재발한다 — 둘 다 영구 차단한다.

    cd platform && pytest server/tests/test_chat_prompt.py -q
"""
import pytest

cp = pytest.importorskip("app.chat.prompt")


def test_chat_prompt_builds():
    p = cp.chat_system_prompt()
    assert isinstance(p, str) and len(p) > 500


def test_chat_prompt_forbids_arbitrary_backtest_period():
    """기간 미지정 시 임의 고정 과거 범위를 nl에 넣지 말라는 규율 — 2025/2026 누락 방지(#3)."""
    assert "임의 백테스트 기간" in cp.chat_system_prompt()


def test_chat_prompt_requires_reading_fold_and_cost_results():
    """결과(buckets·explanation·warnings·ir)를 읽어 답하라는 규율이 프롬프트에 실제 노출돼야."""
    p = cp.chat_system_prompt()
    assert "buckets" in p              # 폴드·연도별 수치를 읽으라는 지시
    assert "explanation" in p          # 비용·체결 가정을 인용하라는 지시
    assert "warnings" in p             # 0거래 경고를 먼저 보라
    assert "재실행" in p               # 재실행 시 ir 대조(귀인 오류 차단)


def test_chat_prompt_includes_data_provenance():
    """데이터 출처 메타인지 — 챗봇이 출처를 추측("PER=FnGuide")하지 않도록 계보 표가 프롬프트에
    실제 노출돼야. trailing 밸류=전자공시(OpenDART) vs 추정실적=FnGuide 구분이 핵심."""
    p = cp.chat_system_prompt()
    assert "data_provenance" in p
    assert "OpenDART" in p and "전자공시" in p     # trailing 밸류 정답 출처
    assert "FnGuide" in p                            # 추정실적(별개)
    assert "추측하지 말" in p                         # 출처 지어내기 금지 가드


def test_chat_prompt_surfaces_advanced_analyses():
    """엔진에 이미 있는 고급 분석을 에이전트가 알도록 analysis_menu가 노출돼야(반복 미사용 부류 차단).
    simulate(nl)로 도달 가능한 sweep/extremize/regime/regression/portfolio/연도별을 프롬프트가 명시."""
    p = cp.chat_system_prompt()
    assert "analysis_menu" in p
    for kw in ("민감도", "최적값", "국면", "회귀", "포트폴리오 진단", "연도별"):
        assert kw in p, f"analysis_menu에 '{kw}' 누락 — 에이전트가 해당 분석을 못 권한다"
