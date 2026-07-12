"""자동매매 템플릿 종가창 스캔 — limit_up_close_v1 진입 후보 합성 (장중 템플릿 설계 §2.5).

서버 preview는 일봉(전일 확정분)이라 이 템플릿의 신호("당일 상한가 마감")를 만들 수 없다 —
종가창(15:25 마감 동시호가)의 브로커 실시간 스캔이 후보를 합성하고, 이후 경로
(run_close_netting → _enter_from_preview)는 기존 그대로 재사용해 킬스위치·손실한도·커버리지·
사이징·멱등 등 안전장치 전수를 상속한다. 전략 파라미터(임계·시장·상한)는 IR에서만 읽는다
(quant_core.ir_engine.templates.scan_params — 단일 출처, 이중 기재 드리프트 차단).
"""

from __future__ import annotations

import logging

log = logging.getLogger("localapp.template_scan")


def scan_template_candidates(broker, strategies: list[dict]) -> list[dict]:
    """템플릿(장중 스캔) 전략들의 종가창 매수 후보 — by_strategy 합성 항목 리스트.

    브로커 스캔은 전략 수와 무관하게 **1회**(가장 낮은 임계로 당겨 전략별 재필터) —
    랭킹 TR은 전 시장 뷰라 종목 수·전략 수 한도가 원천적으로 없다. 반환 항목은
    _enter_from_preview 소비 스키마의 최소형(실소비 키 symbol·direction):
      {"strategy_id": <id>, "candidates": [{"symbol": …, "direction": "long"}, …]}

    개별 전략의 IR 결함은 그 전략만 제외+경고(서버 저장 검증 통과분이 원칙 — 방어),
    스캔 TR 자체 실패는 예외 전파 — 호출자(run_close_cycle)가 신규 진입만 포기하고
    경보로 표면화한다(fail-soft: 브로커 장애가 청산·안전장치를 막으면 안 됨).
    """
    from quant_core.expression_parser import symbol_market
    from quant_core.ir_engine import StrategyIR
    from quant_core.ir_engine.templates import TEMPLATES, scan_params

    wants: list[tuple[dict, dict]] = []          # (전략 row, scan_params)
    for s in strategies or []:
        d = s.get("definition") or {}
        tid = ((d.get("template") or {}).get("id"))
        if not tid:
            continue                              # 일반 전략 — 스캔 무관
        if tid not in TEMPLATES:
            # 이중 안전망 — 서버 앱버전 게이트가 정상이면 도달 불가. 조용히 넘기지 않는다.
            log.warning("[템플릿] 미지 템플릿 id=%s (전략 #%s) — 이 앱 버전 미지원, 진입 제외",
                        tid, s.get("id"))
            continue
        try:
            p = scan_params(StrategyIR.model_validate(d))
        except Exception as e:  # noqa: BLE001 — 결함 전략 1개가 나머지 진입을 막으면 안 됨
            log.warning("[템플릿] 전략 #%s 파라미터 추출 실패 — 진입 제외: %s", s.get("id"), e)
            continue
        wants.append((s, p))
    if not wants:
        return []

    min_thr = min(p["threshold_pct"] for _, p in wants)
    rows = broker.scan_close_surge(min_thr)
    log.info("[템플릿] 종가창 스캔 — 임계 %.1f%% 이상 %d건(상한가 잠김 %d건)",
             min_thr, len(rows), sum(1 for r in rows if r.get("is_limit_up")))

    out: list[dict] = []
    for s, p in wants:
        picked = [r for r in rows
                  if r.get("is_limit_up")          # 잠김(예상 상한가 마감) 필수 — 연구상 엣지 조건
                  and float(r.get("change_pct") or 0) >= p["threshold_pct"]
                  and symbol_market(r.get("symbol", "")) in p["markets"]]
        picked.sort(key=lambda r: float(r.get("change_pct") or 0), reverse=True)
        picked = picked[: p["max_entries"]]
        if not picked:
            continue
        log.info("[템플릿] 전략 #%s 종가 진입 후보: %s", s.get("id"),
                 ", ".join(f"{r['symbol']}({float(r['change_pct']):.1f}%)" for r in picked))
        out.append({"strategy_id": s.get("id"),
                    "candidates": [{"symbol": r["symbol"], "direction": "long"}
                                   for r in picked]})
    return out
