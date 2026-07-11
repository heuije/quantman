# 로컬 테스트 환경 — API $0 시각·데이터 검증

챗봇/엔진 변경을 **API 비용 0**으로 로컬에서 검증한다(프로덕션 배포 전에도 차트·데이터·챗 확인).
로직은 헤드리스 하니스(`scripts/chat_eval.py`·`analysis_diag.py`)가 이미 커버 — 이 문서는 **시각(차트)**
과 **프로덕션 데이터**, **$0 LLM 챗**을 로컬에서 잇는 3부.

> **코드=프로덕션 동일성.** 이 환경은 **항상 최신 `origin/main`에서 기동**한다(워크트리가 뒤처졌으면
> `git fetch && git checkout origin/main` 후 재기동). shim은 유료 API 대신 `claude -p`를 태우는
> **전송 계층만** 다르고 서버·엔진·웹 코드는 프로덕션과 동일하다.

## 전제조건 (제3자 재현 시 필요)
누구나(팀 collaborator) 아래만 갖추면 이 환경을 그대로 재현한다:
1. **private 레포 접근** — `MercKR/quantman`은 private. clone 가능한 collaborator여야 한다(외부인 불가).
2. **본인 Claude Pro/Max 구독** — Part B2 shim은 `claude -p`로 *운영자 본인 구독*을 태운다(양도 불가).
   `claude setup-token` 1회. 구독이 없으면 유료 `ANTHROPIC_API_KEY`로 대체(그 경우 $0 아님).
3. **디바이스 토큰** — Part A pull은 로컬앱을 1회 실행·로그인해 디바이스를 등록하면 OS 키링 토큰을 재사용한다(새 자격증명 불요).
4. **Python + Node(web) 환경** — core/server/web 의존성 설치.

## Part A — 프로덕션 데이터 로컬 적재 (full 스코프)
로컬앱과 동일 sync 경로(`GET /dataset/bundle?scope=full`·디바이스 토큰)로 프로덕션 볼륨과
**동일한 전 모달리티** parquet을 내려받는다.
```
python scripts/pull_prod_data.py            # → ~/.quant-platform/dev-data
```
- **왜 `full` 스코프**: 자동매매 로컬앱은 price+펀더멘털만 소비하므로 기본(`trading`) 번들엔 서버 챗봇
  전용 피드(**flow·시총·공매도·13F**)가 없다. 챗봇 검증엔 이 피드가 필요해 `pull_prod_data`는
  `scope="full"`로 받는다(가격·펀더멘털 + 4피드 = 프로덕션 볼륨 동일). 스크립트가 각 피드 도착 개수를 출력.
- 그 뒤 실행 시 `QP_CORE_DATA_DIR=~/.quant-platform/dev-data` 지정 → 엔진이 실데이터 사용.
- cron이 매일 full 번들을 재빌드하고 ETag로 변경분만 받으므로, 재실행 시 최신만 증분 동기화(안정).

## Part B1 — 픽스처 페이지로 차트 렌더 검증 (LLM 0·순수 $0)
고정 IR 픽스처를 실데이터 위에서 실행해 **서버와 동일 직렬화**로 결과 JSON을 뽑고, dev 웹 라우트가
**실제 `ChatResultView`** 로 렌더한다. LLM 미사용.
```
QP_CORE_DATA_DIR=~/.quant-platform/dev-data python scripts/dump_results.py   # → web/public/dev-fixtures/
cd web && npm run dev                        # dev 서버
# Chrome → http://localhost:<port>/dev/render → 드롭다운으로 픽스처 선택
```
픽스처: 백테스트(자본곡선·%토글·방법론패널)·자본부족(StatusBanner verdict)·이벤트스터디(구성분해)·
교차달력·팩터IC·파라미터스윕. `dump_results.py`의 `FIXTURES`에 추가/수정.
> `/dev/render`는 `import.meta.env.DEV` 게이트라 프로덕션 빌드엔 미포함(tree-shake).

## Part B2 — $0 LLM 챗 (구독 shim 라이브 서버)
라이브 FastAPI 서버를 띄우되 `anthropic.Anthropic`을 구독 shim(claude -p·$0)으로 패치 → NL→IR·
에이전트가 유료 API 대신 구독 사용. 프로덕션 서버 코드는 무변경(dev 진입점만).
```
claude setup-token                           # 1회 — 구독 토큰
python scripts/run_dev_server.py --port 8010
# web/.env.local: VITE_API_URL=http://localhost:8010
cd web && npm run dev
# Chrome → 로그인 → 챗에 자연어 → NL→IR→엔진(실데이터)→차트, 전부 API $0
```
- shim(`scripts/chat_eval/backend.py`)은 프로덕션 모델 티어를 자동 매칭한다: `_model_alias`가
  CHAT_MODEL(claude-sonnet-5)을 `claude -p --model claude-sonnet-5`로 라우팅(별칭 "sonnet"은
  Sonnet 4.6로 풀려 프로덕션과 어긋나므로 정확한 id 사용). 최종 텍스트도 `text_stream`으로 흘려보내
  웹 SSE 렌더가 프로덕션과 동일하게 표시된다.
- **신뢰성 parity(재시도).** 실 anthropic SDK는 일시 오류를 기본 재시도한다. `claude -p`는 공유
  구독·CLI 히컵으로 간헐 빈응답(is_error·빈 result·파싱 실패)을 내므로, shim `_call`이 이를
  **bounded 재시도(총 3회·선형 백오프)**해 일시 히컵이 턴 실패로 새지 않게 한다(타임아웃·토큰누락은
  비재시도). 단 native tool-use는 구독 CLI 한계상 프롬프트형 흉내로 남는다(§11·의도된 근사).

## 검증 커버리지
| 대상 | 방법 | 비용 |
|---|---|---|
| 방법선택·NL→IR·라우팅 | `chat_eval.py`(구독 shim) | $0 |
| 엔진·결과·엑셀 로직 | `analysis_diag.py`·pytest | $0 |
| **차트 렌더**(패널·토글·StatusBanner·구성) | **B1 픽스처 페이지 + Chrome** | $0 |
| **NL→챗→차트 전과정** | **B2 shim 서버 + 웹** | $0 |
| 데이터 | **A: 프로덕션 동일 parquet** | $0 |
