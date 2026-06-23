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
