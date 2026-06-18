# 챗봇 진단→수정→검증 런북

## 0. 데이터 끌어오기 (Claude Code 세션에서)

```bash
railway run python -m app.chat_analytics stats --days 7
railway run python -m app.chat_analytics transcripts --days 7 --suspect
railway run python -m app.chat_analytics transcripts --conv <id>     # 특정 대화 정밀
```
`railway`는 Neon URL(QP_DB_URL)을 주입한다. 로컬(QP_DB_URL 미설정)은 SQLite를 본다.

## 1. 정량 진단 (stats)

| 신호 | 의심 근본원인 |
|---|---|
| `latency_ms.p90` 높음 + `rounds_dist` 큰 라운드 多 | 도구 과호출·프롬프트 비효율(불필요한 도구 루프) |
| `ttft_ms.p90` 높음 | 첫 라운드 입력 비대(컨텍스트·시스템 프롬프트) |
| `cache_hit_rate` ≈ 0 지속 | 히스토리 prompt caching 미작동(PR#159 회귀) |
| `input_tok.p90` 큼 | 컨텍스트 비대(히스토리 compact 미흡·도구결과 과다) |
| `error_rate` 상승 | 도구 예외·LLM 실패 — Railway 로그 `[chat] turn failed` 대조 |
| `tools` 편중 | 특정 도구 과/미사용 — 의도 매칭 점검 |

## 2. 정성 진단 (transcripts + 루브릭)

1. `--suspect`로 미답변 후보부터 본다.
2. 각 턴을 `accuracy-rubric.md` 4축으로 채점, 축4는 2-facet 태깅.
3. 태그를 빈도순 집계.

## 3. 두 루프

### 품질 루프 (축 1~3 + 토큰/지연)
증상 → 근본원인 → **타깃 수정** → **검증**:
- 근본원인 후보: 프롬프트 갭(prompt.py) · 도구 스키마/설명(tools.py) · 컨텍스트 비대(history compact) · 모델 티어(QP_CHAT_MODEL).
- 검증: ① 회귀를 고정하는 unit test 추가(`test_chat_*`) ② 수정 후 `stats` 재실행해 토큰/지연 델타 확인 ③ 같은 류 질문 샘플 재채점.

### 로드맵 루프 (축 4)
미충족 의도의 **계층 태그**를 집계 → 계층별 라우팅:
- `missing-tool` → 엔진·데이터에 이미 있으니 챗봇 도구로 배선(가장 싼 개선). 예: 섹터필터.
- `missing-data` → 데이터엔진 수급 백로그(외부 의존·고비용).
- `missing-logic` → 엔진 프리미티브 신설.
- `missing-metadata-access` → 메타 질의 도구 또는 시스템 프롬프트 보강.
- `history-context` → 컨텍스트 유지 아키텍처(compact 설계 재검토).
- 우선순위 = 빈도 × 가치.

## 4. 진단 예시

- `cache_hit_rate=0` 7일 연속 → 히스토리 마커 미작동 가설 → agent.py `_mark_cache_breakpoint` + Railway `[chat usage] cache_read` 대조 → 수정 → `stats`로 hit_rate 상승 확인.
- "저평가 반도체주" 반복 fail, tag=`missing-tool`(섹터필터 미노출) → screen에 섹터 인자 배선 → 재질문 재채점.
- "아까 그 종목 다시" fail, tag=`history-context` → compact(full→compact)가 이전 도구결과를 치환 → 참조형 질문에 한해 full 유지/재조회 설계.
