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


def test_chat_prompt_enrichment_tool_guidance():
    """Enrichment 신규 필드가 도구 가이드에 노출 — 거래대금 필터/랭킹·공매도비중(잔고 아님) 조회."""
    p = cp.chat_system_prompt()
    assert "거래대금" in p                          # screen 유동성 필터·랭킹
    assert "short_volume_ratio" in p                # inspect 공매도비중
    assert "잔고 아님" in p                          # 잔고 혼동 방지(고위험 정직 라벨)


def test_chat_prompt_requires_reading_fold_and_cost_results():
    """결과(buckets·explanation·warnings·ir)를 읽어 답하라는 규율이 프롬프트에 실제 노출돼야."""
    p = cp.chat_system_prompt()
    assert "buckets" in p              # 폴드·연도별 수치를 읽으라는 지시
    assert "explanation" in p          # 비용·체결 가정을 인용하라는 지시
    assert "warnings" in p             # 0거래 경고를 먼저 보라
    assert "재실행" in p               # 재실행 시 ir 대조(귀인 오류 차단)


def test_chat_prompt_high_stakes_clarify_gate():
    """고위험 모호성(자산 레버리지/상품형태·미가용 대체·종목 정체성)에선 기본값 추측이 아니라
    먼저 되묻으라는 게이트가 노출돼야 — "코스피200"을 임의로 선물(≈5배)로 컴파일해 -130% MDD
    같은 오도 결과를 주던 부류 차단. 약한 모호성은 기존대로 기본값+가정 진행(과도한 되묻기 방지)."""
    p = cp.chat_system_prompt()
    assert "고위험 모호성" in p                 # stakes 에스컬레이션 조항 존재
    assert "레버리지" in p and "코스피200선물" in p   # 자산 상품형태 되묻기(캐논 예시)
    assert "과도한 되묻기 금지" in p            # 약한 모호성은 여전히 기본값+가정(원칙2 균형)


def test_chat_prompt_answer_quality_gates():
    """답변 품질 3게이트가 프롬프트에 노출돼야 — IP1 시황("오늘 장 어때")이 지수 스냅샷(코스피/코스닥/
    나스닥/S&P/VIX)을 앞세우게, IP3 종합 매수/매도 점수는 날조 말고 팩터 순위로, IP4 세금은 일반론+
    전문가 확인+원화환산. 배선(context·summarize)과 함께 이 지침이 있어야 실제 답변에 반영된다."""
    p = cp.chat_system_prompt()
    assert "시장 스냅샷" in p and "오늘 장 어때" in p          # IP1 시황 지수레벨 앞세우기
    assert "매수/매도 점수" in p                              # IP3 종합점수 미제공 명시
    assert "세무 전문가" in p and "원화환산" in p             # IP4 세금 면책 + 해외주식 FX


def test_chat_prompt_includes_data_provenance():
    """데이터 출처 메타인지 — 챗봇이 출처를 추측("PER=FnGuide")하지 않도록 계보 표가 프롬프트에
    실제 노출돼야. trailing 밸류=전자공시(OpenDART) vs 추정실적=FnGuide 구분이 핵심."""
    p = cp.chat_system_prompt()
    assert "data_provenance" in p
    assert "OpenDART" in p and "전자공시" in p     # trailing 밸류 정답 출처
    assert "FnGuide" in p                            # 추정실적(별개)
    assert "추측하지 말" in p                         # 출처 지어내기 금지 가드


def test_chat_prompt_inspect_longterm_window():
    """과거 장기 추이('과거 PER 추이')엔 inspect window를 크게 쓰라는 가이드 노출 — 기본
    120거래일(~5.5개월)이라 장기 흐름이 잘려 보이던 것 방지(데이터는 있어도 표시 부족)."""
    assert "window를 크게" in cp.chat_system_prompt()


def test_chat_prompt_builds_without_manifest():
    """로컬 매니페스트가 없어도(콜드) 프롬프트가 온전히 빌드 — data_inventory 섹션 graceful 생략."""
    p = cp.chat_system_prompt()
    assert isinstance(p, str) and len(p) > 500      # 인벤토리 없어도 본체 무결


def test_data_inventory_section_renders_when_manifest_present(monkeypatch):
    """매니페스트가 있으면 <data_inventory> 섹션 + '지어내지 말고' 지시가 노출돼야 —
    챗봇이 보유 데이터의 검증 뎁스를 화이트리스트로 인지하게(공급≫소비 갭 차단)."""
    from quant_core.data import DataManifest, SymbolManifest
    m = DataManifest(version=1, symbols={
        "옵션풋콜비율": SymbolManifest(symbol="옵션풋콜비율", first_date="2010-01-04",
                                      last_date="2024-12-27", n_rows=3650),
        "005930": SymbolManifest(symbol="005930", feed="ohlcv.kr", first_date="2000-01-04",
                                 last_date="2024-12-30", n_rows=6000),
    })
    monkeypatch.setattr("quant_core.data.load_manifest", lambda *a, **k: m)
    sec = cp._data_inventory_section()
    assert "<data_inventory>" in sec and "옵션풋콜비율" in sec
    assert "지어내지 말" in sec          # 없는 데이터 환각 금지 지시


def test_data_inventory_section_empty_on_no_manifest(monkeypatch):
    """매니페스트 None이면 섹션은 빈 문자열(프롬프트에서 완전 생략)."""
    monkeypatch.setattr("quant_core.data.load_manifest", lambda *a, **k: None)
    assert cp._data_inventory_section() == ""


def test_chat_prompt_surfaces_advanced_analyses():
    """엔진에 이미 있는 고급 분석을 에이전트가 알도록 analysis_menu가 노출돼야(반복 미사용 부류 차단).
    simulate(nl)로 도달 가능한 sweep/extremize/regime/regression/portfolio/연도별을 프롬프트가 명시."""
    p = cp.chat_system_prompt()
    assert "analysis_menu" in p
    for kw in ("민감도", "최적값", "국면", "회귀", "포트폴리오 진단", "연도별"):
        assert kw in p, f"analysis_menu에 '{kw}' 누락 — 에이전트가 해당 분석을 못 권한다"
