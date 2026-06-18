# 챗봇 답변 정확도 루브릭

Claude Code가 `python -m app.chat_analytics transcripts`로 뽑은 트랜스크립트를 읽고 적용한다.
별도 LLM API를 쓰지 않는다 — 채점은 Claude Code 본인이 한다.

## 계층형 역량 인벤토리 (갭 계층 태깅의 기준틀)

채점 전 "이 질문이 현재 역량 안인가"를 판정하려면 무엇이 되는지 알아야 한다. 4계층 호환 계약과 정렬.

- **① 노출 도구(챗봇 tool):** `screen`(스크리닝) · `simulate`(백테스트) · `save_strategy`(저장) ·
  `describe`(단일종목/포트폴리오 진단) · `inspect`(종목 시계열 컬럼 조회).
- **② 가용 데이터:** KR/US OHLCV · 펀더멘털(PER/PBR 등) · 섹터 분류. **미수급(기지 갭):** 뉴스·추정치(컨센서스)·
  수급(플로우)·인트라데이·옵션체인 — `docs/...데이터갭 분석` 참조.
- **③ 엔진 분석로직(IR verbs):** select · describe · relate(회귀) · simulate · extremize.

## 4축 채점 (축별 pass / partial / fail + 한 줄 사유)

1. **의도 이해·가이드** (최우선): 모호해도 진짜 의도를 파악했나? 생산적으로 안내했나(협의 = 선택지·추천 제시,
   더 나은 프레이밍 제안)? 단순 직역 실행에 그치지 않았나?
2. **도구·근거 정확성:** 의도에 맞는 도구를 골랐나? 답변의 수치가 `[결과]` full payload에 실제로 근거하나(날조·
   환각 없음)? — 트랜스크립트의 도구 결과와 답변 숫자를 직접 대조.
3. **질문 완결성:** 끝까지 답했나 / 적절히 되물었나? 빠뜨린 맥락은?
4. **준비도·커버리지:** 역량 밖 질문인가? (a)+(b) 아래.

### 축4 세부

- **(a) 처리 방식:** `graceful`(정직한 한계 고지 + 우회 제안) vs `bad`(환각·자신있게 틀림·무시).
  *준비된 봇은 모르면 곱게 실패해야 한다 — bad는 품질 fail.*
- **(b) 2-facet 태깅:**
  - **증상 태그**(질문에서): `history-reference` · `data-metadata` · `sector/qualitative-filter` · `analysis-type-<x>` …
  - **근본원인 계층 태그**(수정 라우팅):

    | 태그 | 의미 | 수정 트랙 |
    |---|---|---|
    | `missing-tool` | 엔진·데이터엔 있으나 챗봇 도구로 미노출 | 도구 배선(최저비용) |
    | `missing-data` | 기반 데이터 미수급 | 데이터엔진 수급(외부·고비용) |
    | `missing-logic` | IR/엔진 분석 프리미티브 부재 | 엔진 신설 |
    | `missing-metadata-access` | 데이터는 있으나 메타(as-of·유니버스·출처·커버리지) 질의 수단 없음 | 도구/프롬프트 |
    | `history-context` | 과거 대화·결과 참조 실패(컴팩트·리텐션) | 아키텍처 |
    | `out-of-scope` | 설계상 미지원(개인자문·실행) | 올바른 거절이면 OK |

## 채점 출력 형식 (대화당)

```
conv #<id> turn <n>: 의도=pass 근거=pass 완결=partial 커버리지=fail(graceful, history-reference/history-context)
  사유: "아까 그 종목"을 참조했으나 이전 screen 결과가 compact로 치환돼 재호출 못 함.
```

태그를 모아 빈도순으로 정렬 → 개선·로드맵 우선순위(`docs/chat-perf/diagnosis-runbook.md`).
