# 대체데이터 정식 편입 설계 — 애널 컨센서스/목표가 · 기관수급

〔담당: 조대표 (데이터 엔진·인사이트 엔진) · krdata/개별종목분석 경계는 희제 §조율〕

## 0. 목표 (한 줄)

애널리스트 컨센서스/목표가(한경컨센서스)와 기관·외국인 수급(KRX)을 **OHLCV·펀더멘털과 동일한 IR 데이터로 정식 편입** — PIT parquet 적재 + `data/spec.py` 등록 + 수집 cron(과거 백필 + 일일 증분) → 인사이트 엔진이 백테스트·스크리닝·describe에서 `__SELF__.<col>` 신호 ref로 소비.

**왜 지금:** 기존 `server/app/krdata.py`는 개별종목분석 전용 *메모리 on-demand 스냅샷*(투자자 120일·컨센 현재값만)이라 백테스트/시계열 불가. 한경컨센서스 실측으로 과거 ~11년 목표가·투자의견 소급 확인(2026-06-17). 본 설계는 이를 core 데이터 레이어로 끌어와 결정적 백테스트가 가능하게 한다.

## 1. 아키텍처 적합성 (기존 펀더멘털 패턴 재사용)

`feeds/fundamental_kr.py`의 패턴을 그대로 따른다(검증된 PIT 기계):

- **저장**: 종목별 parquet, **`as_of`(발표/거래일) 인덱스**. atomic write + `.empty` 마커 + mtime 신선도.
- **병합**: `indicators.add_fundamentals`류가 `feed_df.reindex(daily_index, method="ffill")` — as_of→일별 ffill. **날짜 D 백테스트는 D 이전 최신값만 봄(look-ahead 차단).**
- **노출 자동화**: 컬럼명을 카탈로그 리스트에 추가 → `blocks/validate.available_refs` 자동 발견 → `__SELF__.<col>` 신호 ref·`/ir/validate`·노코드 드롭다운(`INDICATOR_GROUPS`)·NL 컴파일러까지 자동 배선.
- **결정성 불변식**: 크롤링은 **server cron(feeds)에서만** → parquet 기록 → **core는 파일만 읽음**(네트워크 의존 0 → 골든 고정). 뉴스(`news_kr`)가 서버 엣지에서만 붙는 것과 동일 경계.

## 2. 데이터 모델

### 2-A. 기관수급 — `feeds/flow_kr.py` (KRX OpenAPI, 일별 dense)

소스: **KRX OpenAPI 무료키**(data.krx.co.kr, 외인·기관 2010~, 일말·재배포 OK). env `QP_KRX_API_KEY`.

종목별 parquet(date 인덱스, 일별 dense):

| 컬럼 | 의미 | 단위 |
|---|---|---|
| `inst_net_buy` | 기관 순매수 | 금액(원) |
| `foreign_net_buy` | 외국인 순매수 | 금액(원) |
| `foreign_hold_pct` (옵션) | 외국인 보유율 | % |

"기관 N일 연속/누적 순매수"·"외인 급증"은 **기존 시계열 연산자**(`ts_sum`·rolling·`pct`)로 사용자가 조합. 원시 일별 컬럼만 적재(직교 프리미티브 원칙).

### 2-B. 컨센서스/목표가 — `feeds/consensus_kr.py` (한경컨센서스, event-sparse → ffill)

소스: `consensus.hankyung.com/analysis/list?sdate=&edate=&report_type=CO&pagenum=80&now_page=N`(무로그인·날짜범위·~2015+). 행: 발표일·증권사·종목·목표가·투자의견.

**적재 모델 = 원시 전건 보관 + 증권사별 최신 standing 횡단 집계 (사용자 확정 — 덮어쓰기 회피):**

1. **원시 리포트 이벤트 영구 저장 (전건, 덮어쓰기 없음)**: `{as_of(발표일), broker, target, opinion}` per 종목 — 완전 아카이브. 향후 증권사별·중앙값·이견 재파생 자유(재크롤 불필요). 한경 동일 리포트 재수집 dedupe(키: 종목·발표일·증권사·제목).
2. **일별 컨센서스 컬럼**: 날짜 D = **각 증권사의 최신 standing 리포트**(as_of≤D · 신선도窓 내) 집합을 **횡단 집계**(이후 ffill). 한 증권사의 새 리포트는 *그 증권사 슬롯만* 갱신 — 다른 증권사 standing은 유지(덮어쓰기 X).

| 컬럼 | 의미 |
|---|---|
| `consensus_target` | active 증권사 목표가 **평균** |
| `consensus_target_median` | 중앙값(outlier 강건) |
| `analyst_count` | 커버 증권사 수(active) |
| `consensus_opinion` | 의견 점수 평균(매수+1/중립0/매도−1) |
| `target_dispersion`(옵션) | 목표가 표준편차(이견 정도) |
| `target_revision_pct` | `consensus_target` 직전 대비 변동률(상향+/하향−) |
| `days_since_report` | 마지막 신규 리포트 후 경과일(신선도) |

   - **신선도窓 기본 180일**: 초과한 리포트는 active에서 제외(애널 뷰 노화). 윈도우 길이는 파라미터.
   - 같은 (종목·증권사)에 신선도窓 내 다건이면 가장 최근 1건만 standing(증권사 1표).
3. **`indicators`에서 종가 결합 파생**: `target_upside = consensus_target/Close − 1`(괴리율) — `pb_ratio`(equity/shares/close) 패턴과 동일.

> 산출: 원시 이벤트 → (종목,증권사)별 최신 standing as-of 패널 → 신선도窓 멤버십 → 일별 횡단 평균/중앙값/count/std. 결정적·core 외부(server 수집) 1회 계산 후 parquet.

### 2-C. spec.py 등록 (진실원천)

- `estimate.consensus`(이미 등록·`absent`) → `source` 채우고 `current_status="present"`(또는 backfill 중 `partial`), `provides=[consensus_target, consensus_target_median, analyst_count, consensus_opinion, target_dispersion, target_revision_pct, days_since_report]`, `required_meta=_BASE_META+["as_of"]`.
- 신규 `register(DataTypeSpec(key="flow.kr_investor", pclass=..., point_in_time=True, frequency="daily", source="KRX OpenAPI", provides=[inst_net_buy, foreign_net_buy], required_meta=_BASE_META+["as_of"], downstream=["signal","screener"], current_status="present"))`.
- 카탈로그 그룹: `INDICATOR_GROUPS`에 "수급"·"컨센서스" 그룹 신설(펀더멘털과 별 의미 구분). 컬럼 리스트(`FLOW_COLS`·`CONSENSUS_COLS`)를 merge·catalog가 함께 참조.

## 3. 수집 cron (펀더멘털 cron 패턴 재사용)

`server/app/main.py` APScheduler + `_run_with_retry`(backoff [5,15,30,60,120]분) + `misfire_grace_time/coalesce/max_instances` 가드.

- **과거 백필 (1회·청크·재개가능)**: `_backfill_consensus_chunk()`·`_backfill_flow_chunk()` 10분마다, `budget` 제한, 신선도(mtime) 정렬, atomic write, `.empty` 마커. 한경=날짜범위 페이지 역순, KRX=종목×날짜범위. 펀더멘털처럼 **수일 소요**.
- **일일 증분**:
  - 수급: KRX 마감 후 **16:30 KST**(`krx_1st` 15:45 직후) — 당일 투자자 데이터 append.
  - 컨센서스: 저녁 **19:00 KST** — 마지막 수집 이후 신규 리포트 fetch → 원시 append → 컬럼 재산출.
- **startup 초기 fetch**: 데몬 스레드 지연 시작(기존 `_initial_*` 패턴).
- 모니터링: `/admin` 또는 `*_cache` 상태(fetched_at·n_ok·n_fail·last_error).

## 4. PIT·정합·검증

- as_of = 발표일/거래일 → ffill → look-ahead 0. 체결지연(`delay`)과 결합.
- `manifest`에 `has_as_of=True` 기록 → `gate.py` PIT 게이트 인식.
- **골든 byte-identical**(기존 컬럼·경로 불변, 신규 컬럼은 opt-in ref).
- 신규: feed 단위 테스트(파싱·정규화·ffill), PIT/look-ahead 테스트(미래 리포트 미반영), 실종목 백필 스모크(소수 종목 deploy 환경).

## 5. 정직한 한계

- 한경=**전 증권사 리포트 아님**(대표 표본 근사)·소형주 sparse → 해당 종목 컨센 컬럼 NaN(엔진 자연 제외).
- KRX 직접 다운로드 sandbox 봇차단 → **OpenAPI 키 필수**(deploy env). 키 미설정 시 feed 빈결과(자연 비활성, 골든 무영향).
- 백필 수일 소요(청크).
- **재배포 약관**: 개인 이용 전제(사용자 확정). 정식 상용 확장 시 합법 벤더 재검토(별도 경영 판단).

## 6. 담당 경계

- core 데이터 엔진(feeds·spec·indicators·cron) = 조대표.
- `krdata.py`·개별종목분석 = 희제 → 본 설계는 **additive**(krdata 미변경). 후속: 개별종목분석이 core store(과거)를 읽어 크롤 중복 제거 — 별도 조율.

## 7. 단계 (각 PR)

- **P0** spec.py 등록(estimate.consensus→present·flow.kr_investor 신규) + 컬럼 카탈로그/그룹 + 테스트.
- **P1** feed 2종(`flow_kr`·`consensus_kr`): `fetch_one`/`fetch`(budget·fresh_days·rate_limited) + parquet store + reduce(ffill 컬럼). 단위 테스트.
- **P2** cron(main.py): 백필 청크 + 일일 증분 + startup + config env(`QP_KRX_API_KEY`). retry/가드.
- **P3** indicators 배선(`FLOW_COLS`·`CONSENSUS_COLS` merge + INDICATOR_GROUPS) → 엔진/노코드/NL 자동 노출 + `target_upside` 종가 결합.
- **P4** 검증: 골든 불변·PIT 테스트·실종목 백필 스모크·NL→IR→백테스트 1바퀴(예: "기관 5일 연속 순매수 + 목표가 상향 종목").
