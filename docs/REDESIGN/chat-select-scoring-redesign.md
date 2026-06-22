# 챗 저평가-선별 구조적 재설계 — 뿌리②(스코어 모델) + 뿌리③(자기서술 결과 계약)

> 상태: 설계 승인 → 구현 · 작성 2026-06-22 · 브랜치 `feat/chat-select-scoring`(feat/chat-llm-eval 위 스택)
> 배경: 희제 웹앱 테스트 12개 증상을 4개 구조적 뿌리로 종합진단. 이 spec = **뿌리②+③**(데이터 소싱 뿌리①·성능 뿌리④는 후속).

## 1. 문제 종합 (12증상 → 4뿌리, 본 spec은 ②③)

| 뿌리 | 한계 | 증상 |
|---|---|---|
| ② 스코어 모델 빈곤 | select=raw 단일 팩터 정렬 | 2-3 멀티섹터불가·2-4 산식불투명·2-5c naive ev/ebitda·음수점수 |
| ③ 결과 자기서술 부재 | 결과=raw 프리미티브(코드·원시숫자·단위無·산식無) | 2-1 티커만·2-2 표아닌랭킹그래프·2-5d 단위·2-6 섹터비교표 |

## 2. 핵심 통찰 (재설계 가능 근거)

엔진에 **이미 다 있다** — 새 수학 0, 조합·노출 문제:
- 횡단 정규화: `rank`(분위/개수·방향)·`zscore`·`normalize`·`group_neutralize`(섹터상대)·`winsorize` ([blocks/ops_cs.py](../../core/quant_core/blocks/ops_cs.py))
- 산술 결합: `binary`(＋−×÷)·`unary`(log/abs/sign) ([blocks/ops_arith.py](../../core/quant_core/blocks/ops_arith.py)) → **composite 표현 가능**

확정된 갭: ① `SelectSpec.group_by` 부재 ② core에 종목**명** 조회 없음(classification=Sector/Industry만) ③ NL이 "저평가"를 정규화 composite로 안 쓰고 단일 raw 팩터로 매핑 ④ 결과에 identity/unit/recipe 미부착.

## 3. 뿌리② — 스코어 모델 (기존 atomic 조합 + group_by)

### 3.1 정규화 composite "저평가 점수"
단일 raw 팩터 → **정규화 다팩터 composite**, 기존 연산자 조합으로:
```
저평가점수 = rank(pbr,asc) ＋ rank(per,asc) ＋ rank(ev_ebitda,asc)     # 낮을수록 상위(분위 작음=저평가)
섹터상대   = group_neutralize(factor, "Industry") 로 각 팩터 래핑       # 섹터 내 상대 저평가
```
- 분위(rank pct)라 **음수/스케일 문제 소멸**·결합 의미 명확. 산식 = signal 트리 자체 → **투명**(③에서 recipe로 노출).
- **새 프리미티브 0** — binary+rank+group_neutralize 조합. (사장님 atomic 원칙 부합.)
- 기본정의(조정가능): 저평가 = **섹터상대 백분위 composite(PBR·PER·EV/EBITDA 동일가중)**. 팩터·가중·섹터상대 여부는 NL 협의로 조정.

### 3.2 `select.group_by` 신설 (섹터별 top-N)
`SelectSpec`에 `group_by: Optional[str]`(예: `attribute(Sector)` 또는 "Sector") 추가. 설정 시 **그룹별로 top_n 선별**(배터리 3 + 반도체 3). `run_select`가 그룹별 정렬·head·병합. 멀티섹터 유니버스 = screener `is_in(Industry,[반도체,배터리])`.

### 3.3 screen 도구 확장
`assemble_ir` screen: 단일 score_ref·sector → **다팩터(score_refs)·다섹터·group_by** 허용(모델이 composite·그룹 선별 IR 조립). 단일 입력은 하위호환.

### 3.4 NL idiom (결정적 매핑)
- "저평가"/"싸게 평가된" → 섹터상대 백분위 composite (3.1)
- "섹터별/각 산업 N개" → `group_by`(3.2)
- "배터리랑 반도체" → screener `is_in` 다섹터
- 추상 의도는 기존 `<consult>`로 기본값 제안 후 조정(협의 흐름 유지).

## 4. 뿌리③ — 자기서술 결과 계약 (의미 1곳 부착)

결과 객체에 **identity·unit·meaning**을 *결과 생성 경계서 1회* 부착 → 웹·엑셀·모델 균일 소비.

### 4.1 결과 형상 (select)
```jsonc
{
  "success": true, "query": "select", "as_of": "2026-06-20",
  "score": { "recipe": "섹터상대 백분위 composite(PBR·PER·EV/EBITDA 동일가중)",
             "factors": ["pb_ratio","trailing_pe","ev_ebitda"],
             "normalization": "rank_pct", "group": "Industry", "direction": "low=cheap" },
  "columns": [                                  // 자기서술 메타 — 소비자 공용
    {"key":"name","label":"종목","kind":"identity"},
    {"key":"code","label":"코드","kind":"identity"},
    {"key":"score","label":"저평가점수","kind":"score","format":"0.000","direction":"high_better"},
    {"key":"pb_ratio","label":"PBR","unit":"배","format":"0.00","direction":"low_better"},
    {"key":"market_cap","label":"시가총액","unit":"백만원","scale":1e6,"format":"#,##0"}
  ],
  "groups": [ {"group":"반도체","results":[ … 3 ]}, {"group":"배터리","results":[ … 3 ]} ],  // group_by 시
  "results": [ {"name":"삼성전자","code":"005930","sector":"반도체","score":0.91,"metrics":{…}} ]
}
```
- **name+code**(티커만 → 이름+코드). core 종목명 조회 헬퍼 `symbol_name(sym)` 추가(ticker_db/managed names; 없으면 코드 폴백).
- **`columns` 메타**: label·unit·scale·format·direction. **단위/포맷/정렬방향 단일정의** → 백만원·라벨·저평가순 정렬 해결. 컬럼 레지스트리(알려진 key별 메타) 1곳.
- **`score.recipe`**: 점수 정의 → 산식 투명.
- **`groups`**: group_by 결과 묶음 → 섹터별 비교표.

### 4.2 부착 위치
`run_select`(core)가 name·score.recipe·groups 부착(엔진이 signal·그룹을 앎). `columns` 메타는 공용 레지스트리(`result_columns(result)`)로 select/describe 등에 적용 — serialize 경계(`serialize_ir_result`)에서 보강 가능. **결과 부류 전체 적용**(select 먼저).

### 4.3 소비자 (균일)
- **웹** select 렌더: `columns`로 **표 우선**(이름+코드·단위·저평가순 정렬·groups 섹터 묶음). 랭킹 막대는 보조.
- **엑셀** `_build_select`: `columns`로 헤더·`number_format`·scale(백만원)·recipe 시트.
- **모델요약** `summarize_result`: recipe·그룹·상위 종목명 노출.

## 5. 4계층 매트릭스 + 검증

| 계층 | 변경 |
|---|---|
| 엔진 | SelectSpec.group_by·run_select(group 선별·name·recipe·groups)·symbol_name |
| NL | idiom(저평가 composite·group_by·다섹터) + capability_spec 노출 |
| serialize/계약 | result_columns 레지스트리·serialize_ir_result 보강 |
| 웹/엑셀 | columns 소비(표·단위·정렬·groups) |

**검증**: core 테스트(group_by·composite·contract) + **챗 평가 하니스**에 시나리오 추가(저평가 섹터상대 composite·배터리+반도체 각3·산식투명·단위)·골든 무변경.

## 6. 스코프 경계
- **데이터 소싱 없음** — forward 추정치·historical 밸류는 **뿌리①**(후속). 여기선 *있는 데이터*(pb_ratio·trailing_pe·ev_ebitda·market_cap)로 스코어·표현 구조만.
- 웹 표우선·섹터비교표는 이 계약 *위에서* 구현 — 깊은 비주얼은 **희제 협의**(개별종목·포트 화면 소유).
- 북극성(뿌리①): **마이스톡 전 데이터를 데이터엔진이 수집·배포** — financials(FnGuide 추정·다기간)를 엔진 dataset에 통합, 웹·챗·엑셀 동일 소스.

## 7. 구현 단계
- **P1 엔진**: SelectSpec.group_by + run_select(그룹 선별·name·recipe·groups) + symbol_name. 테스트.
- **P2 NL**: idiom(저평가 composite·group_by·다섹터) + screen 도구 다팩터/다섹터 + capability 노출.
- **P3 계약**: result_columns 레지스트리(label/unit/scale/format/direction) + 부착.
- **P4 소비**: 웹 select 표렌더 + 엑셀 _build_select(단위·헤더·recipe).
- **P5 검증**: 하니스 시나리오 추가 + 풀 회귀(골든·테스트·하니스).
