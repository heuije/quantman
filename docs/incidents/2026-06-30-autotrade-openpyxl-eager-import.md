# 2026-06-30 — 자동매매 사이클 중단 (openpyxl eager import)

> 분류: 프로덕션 자동매매 장애 (로컬앱) · 상태: 근본수정 (재빌드·릴리스 대기) · 영향: 자동매매 미발주

## 발생 / 증상
- 사장님: 로컬앱에서 **"자동매매 시작"을 눌러도 반응이 없음**.
- 로컬앱 로그(`~/.quant-platform/logs/localapp.log`) 실측:
  ```
  04:09:44 WARNING localapp.trader 원장 전략 파싱 실패 [10]: No module named 'openpyxl'
  04:09:47 WARNING localapp.runner cycle 실행 예외 (시도 1/4) — 60초 후 재시도: No module named 'openpyxl'
  ```
  → 자동매매 사이클이 `ModuleNotFoundError: No module named 'openpyxl'`로 크래시하고 60초마다 무한 재시도. 발주 0건이라 "반응 없음"으로 체감. `auto_state="stopped"`(사이클 미완료로 started 전환 실패).
- 부차 체감 요인(별개): 사이클 ① dataset 번들 23,761개 다운로드 **203s** + Railway 서버 502/timeout 빈발.

## 근본 원인
- `core/quant_core/ir_engine/__init__.py:24`가 `from .excel_export import build_strategy_excel`로 **excel_export를 eager import** → `excel_export.py:28`이 `from openpyxl import Workbook`을 **모듈 레벨로 eager import**.
- 결과: **`import quant_core.ir_engine`을 하기만 해도 openpyxl이 필수**가 됨. 자동매매 사이클(`trader.py:192 from quant_core.ir_engine import StrategyIR`)·로컬앱은 **엑셀 export를 전혀 쓰지 않는데도** openpyxl에 강제 의존(잘못된 레이어링, 4원칙).
- 로컬앱 PyInstaller 번들/requirements에 **openpyxl 미선언** → 릴리스 exe에 미포함 → import 시점 크래시. (dev 소스 환경엔 openpyxl 3.1.5 설치돼 있어 재현 안 됨.)

## 대응 (근본수정)
- `ir_engine/__init__.py`: `build_strategy_excel`을 **PEP 562 `__getattr__`로 lazy 재노출**. `from quant_core.ir_engine import build_strategy_excel` 또는 속성 접근 시에만 excel_export(→openpyxl) 로드. 엑셀을 안 쓰는 import는 openpyxl 불요.
- 범위: `core/quant_core/ir_engine/__init__.py` 1파일. `oil_futures/__init__.py:40`도 동일 패턴이나 트레이딩 경로 아님 + futures 세션 활성이라 제외(후속 권고).

## 검증
- openpyxl 차단(=exe 모사) 상태에서 `import quant_core.ir_engine` + `from ... import StrategyIR, strategy_from_spec, run_query` **성공**(이전 크래시 → 해소).
- openpyxl 존재 시 `build_strategy_excel` lazy 로드·callable 확인.
- `core/tests` **554 passed · 2 skipped**, 엑셀 export·골든 **회귀 0**.

## 재발 방지 / 잔여
- **잔여(필수)**: 로컬앱 **재빌드·릴리스(v0.9.58 핫픽스)** 후에야 사장님 exe에 반영됨 — exe는 핫픽스 불가.
- 후속 권고: `oil_futures/__init__.py`도 동일 lazy화(futures 세션 협의). 로컬앱 PyInstaller에 openpyxl 명시 추가는 선택(엑셀을 로컬앱이 쓰지 않으므로 불요 — lazy fix로 충분).
- 교훈: **부차 기능(엑셀 export)의 무거운 외부 의존성을 패키지 `__init__`에서 eager re-export하면, 그 기능과 무관한 모든 import 경로가 의존성에 묶인다.** 광범위 사용 모듈은 lazy 재노출(PEP 562).
