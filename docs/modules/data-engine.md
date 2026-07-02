# 데이터 엔진 〔담당: 조대표〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다.

## 📌 교훈·함정 (작업 전 먼저 읽기)

- **데이터포인트당 소스 1개(no-backup).** 출처는 데이터포인트마다 1개 원칙 — fallback 소스를 두지 않는다.
- **새 데이터는 `spec.py`부터.** 지원 현황(present/partial/absent)의 진실원천 = `core/quant_core/data/spec.py`. 새 데이터 추가·상태 확인은 항상 여기서 시작.
- **새 데이터 = `feeds/`에 모듈 1개 추가.** 소스별 수집 모듈은 `core/quant_core/data/feeds/`에 하나씩.
- **Store A ↔ Store B 분리는 의도적**(속도 최적화). 스크리너를 무거운 canonical(Store A)로 라우팅하지 말 것 — 회귀다. 일원화는 *출처(sourcing)*만, *서빙(serving)*은 분리 유지.
- **라이브 데이터는 core 밖 엣지에서.** 뉴스는 결정적 엔진 밖(서버 엣지)에서 붙인다 — 라이브 네트워크·env키 의존을 core에 넣으면 골든 테스트 불변식이 깨진다. 같은 이유로 모든 라이브 데이터는 엣지에서.
- **OpenDART 라벨은 필러마다 다르다 — 정확매칭 금지.** 주식총수 보고서의 보통주 행 `se`가 회사별로 '보통주'/'의결권 있는 주식\n(보통주)'/'의결권있는 주식' 등 제각각 → `==` 매칭은 ~8%를 조용히 놓쳤다(주식수 null→pb 미산출). 공시 텍스트 필드는 정규화(공백·개행 제거) 후 포함 판정으로. (`fundamental_kr._is_common_share`, 2026-06-10)
- **KR 펀더멘털 커버리지 천장 = 실기업 수(~2,608/4,307).** 관리종목의 ~40%(1,699)는 ETF/ETN/특수증권이라 DART corp_code 자체가 없음 — "커버리지 90%" 같은 전체 비율 게이트는 도달 불가. 게이트는 "실기업 pb 표면화율"로 잡을 것.
- **재배포 직후 스냅샷(Store B) 필드 검증은 +20분 후에.** 부팅 enrich 체인(KRX→NAVER→materialize)이 콜드스타트에 ~12-20분 걸려, 그 사이 per/pbr 등 enrich 필드는 비어 있다(자가 치유). 이 윈도를 회귀로 오인하지 말 것 — 2026-06-11 A1 검증 때 실제로 오인했다.
- **증분 fetch는 *앞으로만* 간다 — 과거 깊이 소급은 별도 prepend 경로 필요.** `fetch_korean_stocks`는 기존 parquet이 있으면 `마지막일+1`부터만 받는다 → `start`를 2010으로 낮춰도 *기존* 종목의 2010~2014는 영영 안 채워진다(신규 종목만 깊게 받음). 깊이 백필은 `min>floor`면 `floor~(min-1)`만 받아 merge-prepend하는 전용 함수로(`backfill_korean_stocks_depth`, depth-done 마커로 완주=0비용). "start 한 줄 바꾸면 끝"이라 착각하면 배포 후 며칠 헛수고. (2026-06-30 P2-A)
- **외부 소스 feasibility는 *프로브*로 — "스크래퍼 하나로 다 풀림" 류 가정 금지.** KRX MDC(`data.krx.co.kr getJsonData.cmd`)는 2026 로그인 의무화로 **익명 POST가 400**(쿠키 심어도). 단 prod `KRX_ID/PW`로는 주식 flow가 작동 = *익명만* 막힘. 선물 수급/OI는 pykrx 미지원·KIS 투자자TR 없음 → **LS `t8462`(수급)+`t8466`(OHLCV+`openyak`OI)**가 유일 깨끗한 길(단 appkey 계정단위·이력깊이 prod 검증 필요). (2026-06-30 P2 feasibility)
- **선물 연속물 = 만기물 패널(진실원천) + 롤/조정은 백테스트 파라미터(서빙뷰 파생).** 롤 시점(`at_expiry`/`days_before_N`/`volume_cross`/`oi_cross`)·갭조정(`none`/`ratio`/`back_adjust`)을 **데이터에 굽지 말 것** — 트레이더마다 롤 시점이 다르고 만기 꼬리 왜곡가는 *진짜 데이터*라 회피 여부는 전략의 모델링 선택이다. KRX `fut_bydd_trd`는 만기물별 OHLC(`TDD_*`)·정산가(`SETL_PRC`)·기초지수(`SPOT_PRC`)·거래량·OI를 주고, ISU_NM `F YYYYMM`으로 계약월(스프레드 `SP`는 정규식 미스로 자연 제외). 만기는 달력(2nd 목) 계산이 아니라 **패널 마지막 존재일**로 데이터 주도 도출(휴장 이동 강건·재현가능). ⚠ **함정 2건(합성 테스트 못 잡음·실데이터 프로브가 잡음):** ①살아있는 최근월물은 last-present=오늘≠만기 → days_before/cross가 조기롤(만기 이후 거래일 없으면 롤 금지로 수정) ②ratio/back_adjust 앵커를 `order` 마지막(미사용 원월물)이 아닌 **마지막 active 세그먼트**에 고정(안 하면 원월물 베이시스가 최근가 오염). ratio=수익률 정확보존, back_adjust=가법(가격차 보존·대형갭 음수). (2026-07-02 S4/E2)
- **데이터를 엔진에 넣는 것 ≠ 챗봇이 그걸 아는 것 (공급≫소비 갭).** P2-B/S4로 매크로(V-KOSPI·풋콜·국고채·선물OI·ETF flow)를 공급했으나 챗 LLM·NL→IR 컴파일러가 존재를 몰라 활용 불가였다 — `capability_spec` 선물심볼은 하드코딩·매크로 누락, `get_all_indicator_columns` SSOT는 BASE+FUND+FLOW+CONSENSUS만 태우고 매크로 심볼·커버리지 뎁스는 안 태움, `data_spec()` 자기서술은 "문서/프론트"만 소비·LLM 미도달. **닫는 법=하드코딩 신규 카탈로그가 아니라 기존 SSOT를 LLM에 배선**: ①커버리지는 **검증 매니페스트 실측 파생**(`coverage_inventory`=build_manifest per-symbol first/last + coverage_report pct — 하드코딩 "2010~" 금지, 백필 중 실제 뎁스 반영) ②챗 프롬프트에 화이트리스트 인벤토리+*"여기 없으면 미지원, 지어내지 말라"* 지시(미지원 열거=블록리스트는 무한·유지불가 → 보유+뎁스 인지→그외 미지원) ③컴파일러엔 `MACRO_SYMBOLS` 심볼카탈로그+별칭 name_map(풋콜비율→옵션풋콜비율) ④**드리프트 가드 테스트**(MACRO_SYMBOLS⊆data_type_symbols⊆data_spec)로 부류 재발 차단. 검증은 배선(실조립)뿐 아니라 **구독 $0 LLM eval로 행동까지**(매크로 NL 3/3→실제 매크로 심볼 IR 참조 실증). (2026-07-02 챗 커버리지 인지)

## 현재 구조 (안정)

**기능.** 백테스트·인사이트가 쓰는 모든 데이터를 한 곳에서 정의·수집·검증해 공급. 출처는 데이터포인트당 **1개 원칙**(no-backup). 지원 현황(present/partial/absent)의 **진실원천 = `core/quant_core/data/spec.py`** — 새 데이터 추가/상태 확인은 여기부터.

**폴더.**
- `core/quant_core/data/spec.py` — 데이터 종류 등록부(진실원천). 무엇을 지원/미지원하는지
- `core/quant_core/data/feeds/` — **소스별 수집 모듈**(새 데이터 = 여기에 모듈 1개 추가): `fundamental_kr.py`(OpenDART)·`fundamental_us.py`(SEC)·`classification.py`(섹터/FDR)·`listing.py`(상장폐지)·`news_kr.py`(뉴스/네이버 검색 API)
- `core/quant_core/data_fetcher.py`(가격 OHLCV: FDR·yfinance)·`indicators.py`(기술지표 24종 자체산출)·`dataset.py`(조립)
- `core/quant_core/data/gate.py`·`deps.py`·`manifest.py` — 무결성 게이트(PIT·생존편향)·의존성 도출·수집 기록
- **서빙(server/app)**: `data_cache.py` = **Store A**(canonical parquet 시계열 — 백테스트·360리포트·SELECT 공급) · `krx_cache.py` = **Store B**(스크리너 인메모리 스냅샷, 빠른 조회 전용) · `naver_fundamentals.py`·`technical_cache.py`·`us_metrics_cache.py`(보조 캐시) · `main.py`(cron 오케스트레이션 — KR 펀더멘털 매일 17:30 KST 등)

**구동 워크플로.** `server/app/main.py`의 cron이 시점마다 `feeds/` 수집 → `data_cache`(Store A)에 적재 → 엔진/리포트가 소비. 스크리너는 별도로 가벼운 `krx_cache`(Store B) 스냅샷을 읽음. 뉴스만 예외 — 사전수집 없이 360 리포트 요청 시점에 `server/app/routers/ir.py`에서 **서버 엣지 on-demand 호출**(저장 안 함).

**현황.**
- **partial(되지만 위험):** 한국 시세 분할조정 표기 미검증 / 시총·종목마스터가 백테스트 store 미부착(스크리너 메모리에만) / 소스별 조정정책 혼재.
- **present(2026-07 프로덕션 로그 검증):** 애널 컨센서스·목표가(한경)·수급(기관/외국인, pykrx) — cron 적재 가동중(10분 백필→2010 floor + 일일증분 16:30/19:00). 공매도·KR 지수 멤버십 이력은 여전히 absent.

## 작업계획 로그 (누적·최신 우선)

### [2026-07-02] 2010 Core floor 통일 + 데이터엔진 구조 리팩토링 (대원칙 기반) [진행중]
- 의도: 데이터엔진 정밀 진단(3축 감사: data_fetcher 모놀리스·feeds 모듈러·main.py 오케스트레이션) 결과, 수집이 자산군별로 **깊이·백필문법·세대신호·메타정합이 제각각**이던 것을 대원칙 7(SSOT·PIT·일관깊이·피드계약·단일백필문법·실측주장·세대일관성) 하에 구조 정리. 핵심 실측: **US 신규종목 floor 2015 하드코딩 + US 깊이백필 부재 → US 2010 도달 3%뿐**(KR은 백필 가동중) — 자산군 간 깊이 편차가 백테스트 비교 불공정으로 직결. 설계=`docs/REDESIGN/data-collection-plan.md`.
- 계획: W1 `CORE_FLOOR` SSOT(policy.py·산재 리터럴 치환) → W2 **US 깊이백필 신설**(`backfill_overseas_depth`·min_date 그룹배치·cron `:03`) → W3 날짜커서 백필 공통화(`data/backfill.py`·컨센서스+KRX×4 한 구현·기존 커서파일 호환) → W4 spec 실측정합(flow/consensus absent→present·`floor` 필드·인벤토리 "백필 진행중" 노출) → W5 드리프트 가드 필드형 확장 → W6 세대신호 부류수정(개별 fetch 5곳 mark_data_dirty).
- 진행: W1~W6 구현 + 테스트(신규 3파일·가드 6종). 스코프 제외(overthinking 방지): 공휴일 보정(피해=휴일 재fetch 비효율뿐)·manifest attempted 추적(소비처 미확정)·Q4 시총 피드(KRX `sto` 포털 신청 선행).

### [2026-06-29~] 완결 일관 데이터셋 — 커버리지 강화 (P0~P2) [진행중]
- 의도: 챗봇이 과거 단면(예: N년 전 시총상위)을 **편향 없이** 답하도록, KR/US 유니버스 내에서 *동일 기간·동일 (적용)필드·무결손* "완결 일관 데이터셋"을 유지한다. 편향의 진짜 원인은 데이터가 얕은 것이 아니라 *없는 걸 0으로 취급*하는 것 → **null≠0**(커버리지 측정·노출)이 1순위, 그 다음 깊이 백필. 완결="모든 필드"가 아니라 **인스트루먼트 타입별 적용필드**(KR주식·KR선물·US주식이 필드셋 자체가 다름). 설계서=`docs/REDESIGN/data-completeness-coverage-redesign.md`.
- 계획: P0 커버리지 매니페스트(측정·노출·무위험) → P1 US 유니버스 권위정의 → P2 깊이 백필(A=KR 2010, B=선물 수급/OI LS, C=Tier1 보류) → P3 무결손검증 → P4 갭필드.
- 진행: **P0(#253 `51eb5aa`)·P1(#258 `3f49e97`)·P2-A(#264 `ad9d7ea`)·P2-B S1~S3(#267 `8b92cd6`) 머지·배포·prod LIVE.** P2-B는 OI+옵션P/C+ETF flow+V-KOSPI+채권/국채를 **공식 KRX API**(`data-dbg.krx.co.kr`·AUTH_KEY·2010~)로 — S1(시장지표5)→S2(선물OI)→S3(ETF flow) 완결. **S4+E2(선물 만기물 패널+백테스트 롤) 구현·실데이터검증·미push** (branch `feat/futures-panel-roll`, 커밋 5d96711·a811118·ee443c5): KOSPI200을 투자닷컴CSV+KIS → 공식 KRX `fut_bydd_trd` **만기물 패널(진실원천)**로 전환하고, 예약만 돼 있던 롤/조정(E2)을 엔진에 배선 — 롤을 데이터에 굽지 않고 백테스트 파라미터로(기본 at_expiry 무가공). `data/futures_roll.py`(5롤×3조정 stitch)·`_apply_futures_roll`(엔진)·죽은 CSV/KIS 234줄 제거. 실데이터 e2e: 연속물 recent=1385(실제가·옛 합성1434 아님)·서빙뷰=at_expiry·oi_cross 오버라이드 확인. **S5(LS 선물 투자자수급 t8462)만 남음**(LS 데이터계정 프로비저닝 blocker). **P2-C(Tier1)→공식 KRX API로 무료 재편입**(보류 해제). 미국채=FRED 기존. 교훈은 §교훈 distill.
- ⚠ feasibility 반전 2건: **①KRX MDC(`data.krx.co.kr`)는 로그인벽**(익명400)이나 **②공식 KRX Open API(`data-dbg.krx.co.kr`)는 별개·무로그인·AUTH_KEY·2010~** — OI·P/C·V-KOSPI·ETF·채권 전부 무료 수집(라이브검증). §교훈·설계서 §9.

### [2026-06-17] 애널 컨센서스·기관수급을 IR 데이터로 정식 편입 [진행중]
- 의도: 애널 컨센서스/목표가(한경컨센서스)와 기관·외국인 수급(KRX, pykrx 로그인)을 OHLCV·펀더멘털과 **동일한 core IR 데이터로 편입**한다. 기존 server `krdata.py`는 개별종목분석 전용 메모리 on-demand 스냅샷(~120일)이라 백테스트·시계열 불가 — PIT parquet 적재 + `spec.py` 등록 + 수집 cron으로 끌어와 인사이트 엔진이 `__SELF__.<col>` 신호 ref로 백테스트/스크리닝/describe에서 소비하게 한다. 범위는 core 데이터 엔진(feeds·spec·indicators·cron)만 — `krdata.py`·개별종목분석은 additive(미변경, 후속 조율로 core store 재사용).
- 계획: P0 spec 계약 → P1 feed 2종(`consensus_kr` 한경HTML·`flow_kr` pykrx) → P3 indicators 배선(`add_consensus`/`add_flow`·`target_upside`·INDICATOR_GROUPS 수급/컨센서스) → P2 cron(백필+증분) → P4 end-to-end. 설계=`docs/REDESIGN/altdata_consensus_flow_spec.md`.
- 진행: **P0·P1·P3 구현·검증 완료**(core 299·root 390 green·골든 byte-identical). 컨센서스=한경 실데이터 라이브검증(증권사별 standing 집계·no-overwrite·180일 만료·리비전). 수급=pykrx key-safe 검증, 실데이터는 KRX creds(Railway)로 P2 cron에서. 남은 건 **P2(cron)·P4**. draft PR=`feat/ir-altdata-feeds`.
- ⚠ 발견: **KRX가 2025-12-27 전체 로그인 의무화**(안티봇) — 공식 무료 OpenAPI엔 투자자 데이터 없음, `data.krx.co.kr` 조회는 무료지만 KRX 계정 로그인 필수 → **pykrx(`KRX_ID`/`KRX_PW`)가 유일 무료 10년 경로**. 한경컨센서스는 무로그인·11년. (조사 상세: 메모리 reference-free-data-sourcing)

### [2026-06-08~11] 스크리너 PBR/PER을 OpenDART로 통일 (A1) [완료]
- 의도: 스크리너(Store B)의 PBR/PER을 360 리포트·백테스트(Store A)와 같은 OpenDART 출처로 통일해 두 화면의 밸류 불일치를 없앤다. 서빙 분리는 유지(출처만 일원화).
- 계획→경과: `feat/data-engine-a1-valuation`에 구현·로컬검증(2026-06-08) 후 "백필 ~90% 대기"로 보류했으나, **게이트 재해석**(2026-06-10): 90%는 ETF 때문에 도달 불가, 올바른 기준=실기업 pb 표면화 — 아래 shares 수정으로 충족됨. 원 브랜치는 main −146커밋 stale이라 직접 머지 대신 **코드 커밋(742d345)만 최신 main에 cherry-pick**(`feat/a1-valuation-rebase`).
- 시행착오: cherry-pick 충돌 2건은 그 사이 cron 개편(10분 청크+17:30 invalidate 앵커) 탓. materialize 배선을 원안(17:30 앵커)이 아닌 **`_refresh_naver` 끝**으로 이동 — 스냅샷(Store B)은 부팅·15:45마다 통째 재구축되므로 NAVER와 같은 자리(부팅+120s·17:00)에서 채워야 재배포 후 밸류 공백이 없다.
- 결과: A1 테스트 10 green(146커밋 전 작성한 스크리너PBR==describePBR 일관성 테스트 포함)·서버 전체 184 green. **PR #103 머지·배포·라이브 섀도우검증 통과(2026-06-11): 스크리너 저PBR 15종목 전수 pbr == canonical select pb_ratio, float 정확 일치 15/15.** 스크리너 밸류는 이제 4계층(스크리너·360·백테스트) 단일 출처(OpenDART).
- ⚠ 라이브에서 배운 것(부팅 윈도): 재배포 후 **~12-20분간 스크리너 pbr/per가 비어 있다** — NAVER refresh(~2800종목 HTTP, 수 분) 뒤 materialize(get_raw_dataset 전체 로드+load_fund_all ~45s+15,917종목 프로젝션)가 이어지는 콜드스타트 시간. NAVER 시절에도 같은 부류의 윈도(NAVER fetch 시간)가 있었고 materialize가 그 꼬리를 몇 분 늘린 것 — 자가 치유되므로 수정 불요(과수정 금지). 첫 검증 때 이 윈도를 "회귀"로 오인해 진단 우회로를 팠다가 재확인으로 판명 — **재배포 직후 스냅샷 필드 검증은 +20분 후에**.

### [2026-06-10] KR 펀더멘털 백필 완료 검증 + 주식수 라벨 버그 근본수정 [완료]
- 의도: 백필 진행 점검 → "스크리너/describe에서 실사용 가능한가" 검증.
- 시행착오: ①"1,699개 실종목 false 마킹" 진단은 **오진**(실측: 전부 ETF류, 실기업은 수집돼 있었음 — pb=null을 데이터 부재로 오인). ②진짜 버그는 따로 있었음: 실기업 ~8%(216개, LG전자·SK·신한지주 등)가 **주식총수 `se` 라벨 정확매칭 누락**으로 shares=null→pb 미산출.
- 결과: `_is_common_share` robust 매칭(PR#90)+216개 재수집 마이그레이션→**select pb 30/30 검증**. 임시 진단·마이그레이션 코드(coverage/probe/clear_markers/refetch) PR#92로 전부 제거. 교훈 2건은 §교훈에 distill.
