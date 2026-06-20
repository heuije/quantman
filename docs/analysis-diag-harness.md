# LLM-free 분석·엑셀 진단 하니스

챗봇 결정적 코어(IR→엔진→결과→요약/엑셀)를 **고정 IR 코퍼스**로 $0·재현가능하게 진단·개선하는
도구. LLM(자연어→IR) 단계를 우회하므로 **토큰 비용 0**. 다양한 분석·엑셀 생성 품질을 무제한 검증.

## 왜 LLM-free인가 (경계)
```
자연어 → [LLM: NL컴파일러] → IR → [결정적 엔진] → 결과 → [결정적] → 요약 + 엑셀
          └─ 1단계만 LLM ─┘        └────────── 2·3단계 = 이 하니스가 검증($0) ──────────┘
```
- 엔진·요약·엑셀·데이터품질·신호패널은 **고정 IR이면 전부 무료·무한·재현가능**.
- LLM이 필요한 건 "자연어를 *맞는 IR로* 이해하나" 한 가지뿐 → 별도 얇은 계층(Haiku, 가끔).

## 구성
| 파일 | 역할 |
|---|---|
| `core/tests/analysis_corpus.py` | **단일 진실원천** — 13 result_shape + 엣지(크로스에셋 신호패널·데이터품질 stale·KR선물 실데이터) IR 픽스처 + `run_case`(strategy_from_spec=챗봇 정본 경로) + frozen 로더 |
| `scripts/analysis_diag.py` | 진단 CLI — 실 .xlsx + `REPORT.md`(형상·핵심지표·경고·라이브수식 표) 생성 |
| `scripts/capture_frozen.py` | frozen 실데이터 스냅샷 캡처(데이터 접근 환경에서) |
| `core/tests/test_analysis_corpus.py` | pytest 회귀(전 케이스 구조 + 신호패널 라이브수식 + 데이터품질 경고 + 13형상 커버리지) |
| `core/tests/test_ir_excel_export_shapes.py` | 엑셀 *구조* 회귀(run_query 경로) — 코퍼스 import |

## 2계층 전략
- **A. 결정적($0·매 변경):** 코퍼스 → 엔진 → 요약+엑셀. 회귀 어설션 + 사람 검토.
- **B. NL→IR(Haiku·가끔):** 자연어 이해 골든쌍(이 하니스 범위 밖 — 비용 절감은 `QP_CHAT_MODEL=haiku`).

## 실행
```bash
# 합성 데이터(결정적, 항상 동작):
PYTHONUTF8=1 PYTHONPATH=core python scripts/analysis_diag.py
# frozen 실데이터(스냅샷 있으면 그것, 없으면 합성 폴백):
PYTHONUTF8=1 PYTHONPATH=core python scripts/analysis_diag.py --real
# 특정 케이스만:
PYTHONUTF8=1 PYTHONPATH=core python scripts/analysis_diag.py --only cross_asset
# 회귀 테스트:
PYTHONUTF8=1 PYTHONPATH=core python -m pytest core/tests/test_analysis_corpus.py -q
```
→ 산출: `analysis_diag_out/<case>.xlsx` + `REPORT.md`(사람 검토용 표). `analysis_diag_out/`은 git 미저장.

## frozen 실데이터 캡처 (현실적 숫자)
```bash
# 데이터 접근 환경(온라인 / railway run)에서:
PYTHONUTF8=1 PYTHONPATH=core python scripts/capture_frozen.py "S&P500" 코스피200선물
```
- 코스피200선물은 번들 CSV로 이미 frozen 스냅샷 존재(`core/tests/fixtures/frozen/`).
- 미캡처 심볼이 있는 케이스는 진단이 **합성으로 graceful 폴백**(끊기지 않음).

## 케이스 추가 (다양성 확장)
`analysis_corpus.CASES`에 dict 한 줄:
```python
dict(name="...", desc="...", ds=빌더함수, ir={...IR dict...},
     checks={"시트명": ["반드시 포함될 문자열", ...]}, real=["실데이터 심볼"]|None)
```

## 진단 → 개선 루프
1. **회귀(골든):** 무심코 깨지면 pytest 빨강 → "작동하는 것 보호".
2. **진단 CLI(사람 검토):** `REPORT.md`·실 xlsx로 "이게 맞나" 안목 검토 → "개선점 발굴".
3. **국소화:** 이상하면 → 엔진(`ir_engine/run.py`)·엑셀(`excel_export.py`)·데이터(`data_quality.py`) 중 어디인지 분리.
