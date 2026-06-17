# LS API Knowledge Base Changelog

발견·fix 시계열. release version과 연동.

---

## 2026-06-17

### KB 초기 구축 (A2)

- `docs/ls-api/` 신설
- 국내주식 핵심 6 TR (CSPAT00601·CSPAT00701·CSPAT00801·t0424·t0425·t1102) endpoint 문서 초안
- 공개 소스 기반 (LS 공식 howto-sample·teranum/ls-openapi-samples·ermaker/xingAPI)
- 키 미발급 상태 — 미검증 필드 ⚠️ 표시
- GOTCHAS.md 초안 9건 (G1~G9)
- INDEX.md, README.md 작성
- docs/api-index.md에 LS 행 등록

### KB 갱신 (B6 구현·리뷰 반영)

- GOTCHAS **G10** 추가 — `order_status`가 t0425 미체결-only라 체결/취소를 인지 못 함(정산 reconcile 백스톱). Phase C에서 chegb="0" 전환.
- INDEX.md·endpoint `우리 코드 위치`를 실제 LsBroker 메서드명으로 갱신(`buy/sell/cancel/account_snapshot/price/pending_orders/order_status`).

다음 단계: 키 발급 후 ⚠️ 필드 라이브 확정 → 🟢 전환, GOTCHAS 업데이트.
