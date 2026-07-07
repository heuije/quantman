"""데이터 계보 레지스트리 — 챗봇 메타인지 SSOT. 출처 추측 오답("PER=FnGuide") 차단."""
from quant_core.data.provenance import data_provenance, provenance_for_prompt


def test_provenance_registry_key_sources():
    cats = {p["category"]: p for p in data_provenance()}
    # trailing 밸류(PBR/PER/EV)는 전자공시(OpenDART) — 챗봇이 'FnGuide'로 오답하던 것의 정답
    val = next(p for c, p in cats.items() if "밸류" in c)
    assert "OpenDART" in val["source"] or "전자공시" in val["source"]
    assert "FnGuide" not in val["source"]            # 밸류는 FnGuide가 아님(분열 핵심)
    # 추정실적(forward)은 FnGuide — 별개 소스
    est = next(p for c, p in cats.items() if "추정실적" in c)
    assert "FnGuide" in est["source"]


def test_provenance_for_prompt_format():
    s = provenance_for_prompt()
    assert "OpenDART" in s and "전자공시" in s        # trailing 밸류 출처
    assert "FnGuide" in s                              # 추정실적 출처
    assert "PBR" in s or "PER" in s
    assert s.count("\n") >= 5                          # 여러 범주가 표로


def test_provenance_exposes_cot_and_earnings_calendar():
    """P4 카탈로그 노출: COT(투기순포지션·미결제약정 US선물)·실적캘린더가 SSOT provenance에 명시돼
    챗 프롬프트가 pull한다 — 수집됐으나 LLM이 존재를 몰라 라우팅 못 하던 갭 종결.
    (COT/실적캘린더 데이터는 수집 완료: cot_cftc.py·earnings_calendar.py)."""
    cats = {p["category"]: p for p in data_provenance()}
    cot = next(p for c, p in cats.items() if "COT" in c)
    assert "CFTC" in cot["source"]                     # COT 출처=CFTC
    assert "투기순포지션" in cot["category"] and "미결제약정" in cot["category"]
    assert "투기순포지션" in cot["method"]             # naming 패턴 예시(LLM 라우팅용)
    earn = next(p for c, p in cats.items() if "실적캘린더" in c)
    assert "발표일" in earn["category"]
    # 프롬프트 렌더에도 COT·실적캘린더가 실린다(LLM이 실제로 본다)
    s = provenance_for_prompt()
    assert "COT" in s and "실적캘린더" in s
