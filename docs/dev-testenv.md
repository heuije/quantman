# 로컬 테스트 환경 — API $0 시각·데이터 검증

챗봇/엔진 변경을 **API 비용 0**으로 로컬에서 검증한다(프로덕션 배포 전에도 차트·데이터·챗 확인).
로직은 헤드리스 하니스(`scripts/chat_eval.py`·`analysis_diag.py`)가 이미 커버 — 이 문서는 **시각(차트)**
과 **프로덕션 데이터**, **$0 LLM 챗**을 로컬에서 잇는 3부.

## Part A — 프로덕션 데이터 로컬 적재
로컬앱과 동일 경로(`GET /dataset/bundle`·디바이스 토큰)로 프로덕션 전체 parquet을 내려받는다.
```
python scripts/pull_prod_data.py            # → ~/.quant-platform/dev-data (프로덕션 동일 ~1.1GB)
```
그 뒤 실행 시 `QP_CORE_DATA_DIR=~/.quant-platform/dev-data` 지정 → 엔진이 실데이터 사용.

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
# web/.env.local: VITE_API_BASE=http://localhost:8010
cd web && npm run dev
# Chrome → 로그인 → 챗에 자연어 → NL→IR→엔진(실데이터)→차트, 전부 API $0
```

## 검증 커버리지
| 대상 | 방법 | 비용 |
|---|---|---|
| 방법선택·NL→IR·라우팅 | `chat_eval.py`(구독 shim) | $0 |
| 엔진·결과·엑셀 로직 | `analysis_diag.py`·pytest | $0 |
| **차트 렌더**(패널·토글·StatusBanner·구성) | **B1 픽스처 페이지 + Chrome** | $0 |
| **NL→챗→차트 전과정** | **B2 shim 서버 + 웹** | $0 |
| 데이터 | **A: 프로덕션 동일 parquet** | $0 |
