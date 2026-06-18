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


def test_chat_prompt_requires_reading_fold_and_cost_results():
    """결과(buckets·explanation·warnings·ir)를 읽어 답하라는 규율이 프롬프트에 실제 노출돼야."""
    p = cp.chat_system_prompt()
    assert "buckets" in p              # 폴드·연도별 수치를 읽으라는 지시
    assert "explanation" in p          # 비용·체결 가정을 인용하라는 지시
    assert "warnings" in p             # 0거래 경고를 먼저 보라
    assert "재실행" in p               # 재실행 시 ir 대조(귀인 오류 차단)
