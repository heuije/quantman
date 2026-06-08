"""블록 IR 백테스트 라우터 (신규 — 기존 /backtest·빌더와 병행, 충돌 0).

명세 §11·P1-6. 노코드 블록 트리(Node)를 받아 검증→백테스트→결과(+경고)를 반환.
기존 라우터는 그대로 두고 새 prefix /ir 로만 추가한다.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

import quant_core as qc
from quant_core.blocks import DatasetMeta, available_refs, catalog_spec
from quant_core.blocks.node import Node, referenced_symbols
from quant_core.ir_engine import (StrategyIR, backtest_from_spec, needed_columns,
                                  needed_symbols, strategy_from_spec, validate_strategy)

from .. import data_cache
from ..data_cache import get_dataset
from ..data_manifest import build_dataset_manifest
from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, Strategy, StrategyVersion, User
from ..serialize import clean_json, serialize_backtest

router = APIRouter(prefix="/ir", tags=["ir"])


class IrBacktestIn(BaseModel):
    """IR 백테스트 요청 — buy/sell은 블록 Node 트리(dict)."""

    trade_symbol: str
    buy: dict
    sell: dict | None = None
    hold_days: int | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    trail_atr_mult: float | None = None
    trail_pct: float | None = None
    sell_amount_pct: float | None = None
    fill: str | None = None
    initial_capital: float = 10_000_000.0
    start: str | None = None
    end: str | None = None


@router.get("/catalog")
def ir_catalog(user: User = Depends(get_current_user)):
    """블록 카탈로그 — 빌더가 제공할 블록·슬롯·기본값 명세."""
    return {"blocks": catalog_spec()}


@router.post("/validate")
def ir_validate(body: dict, user: User = Depends(get_current_user)):
    """전략 정의의 논리 정합성 검증 — 백테스트 없이 이슈 목록 반환 (UI 실시간 검증).

    구조(S-rules)·의미(M-rules)·데이터 가용성(R0)·무결성을 한 번에 검사한다.
    ok=false면 에러(차단), issues에 경고도 함께(차단 안 함).
    """
    try:
        s = StrategyIR.model_validate(body)
    except ValidationError as e:
        return {"ok": False, "issues": [{
            "rule": "schema", "severity": 30, "is_error": True,
            "message": f"정의 형식 오류: {e.errors()[0]['msg']}", "path": "root"}]}
    # 실시간 검증은 compute 불필요 — 경량 심볼 인덱스 키 ∪ 전역 컬럼으로 유효참조 구성
    # (전체 지표계산 load_dataset 8.5분에 묶이지 않게). available_refs(dataset)와 동치.
    valid_refs = (set(data_cache.get_symbol_index().keys())
                  | {"Open", "High", "Low", "Close", "Volume"}
                  | set(qc.get_all_indicator_columns()))
    issues = validate_strategy(s, valid_refs=valid_refs)
    out = [{"rule": i.rule, "severity": i.severity, "is_error": i.is_error,
            "message": i.message, "path": i.path} for i in issues]
    return {"ok": not any(i["is_error"] for i in out), "issues": out}


@router.post("/backtest")
def ir_backtest(body: IrBacktestIn, user: User = Depends(get_current_user)):
    """블록 IR 전략을 백테스트. 검증 실패 시 issues, 성공 시 결과+무결성 경고.

    단일 trade_symbol 거래 — 필요 종목(거래종목 ∪ buy/sell이 참조한 외부심볼)만
    부분집합 로드한다(전 유니버스 빌드 우회). buy/sell 파싱 실패는 needed를 좁히지
    못할 뿐이며, 검증은 backtest_from_spec이 동일하게 수행해 같은 에러를 반환한다.
    """
    spec = body.model_dump(exclude_none=True)
    needed = {body.trade_symbol}
    for nd in (body.buy, body.sell):
        if nd:
            try:
                needed |= referenced_symbols(Node.model_validate(nd))
            except Exception:                       # noqa: BLE001 — 검증은 backtest_from_spec이 수행
                pass
    dataset = qc.load_dataset_for(needed)
    res = backtest_from_spec(
        spec, dataset,
        valid_refs=available_refs(dataset),
        meta=DatasetMeta(),
    )
    if not res.get("success"):
        return {"success": False, "error": res.get("error"),
                "issues": res.get("issues", [])}
    payload = serialize_backtest(res)
    payload["warnings"] = res.get("warnings", [])
    return payload


def _persist_ir_backtest(session: Session, user, body: dict, payload: dict) -> None:
    """저장된(소유) 전략의 1회 IR 백테스트를 BacktestRun으로 영속화 — 내 전략 "백테스트 내역" 탭.

    body.strategy_id가 있고 그 전략이 user 소유일 때만 저장(없으면 빌더의 시범 실행 →
    orphan 정책에 따라 미저장). version_no는 그 전략의 최신 버전. operand /backtest/run이
    하던 영속화를 IR 경로로 동형 이전한 것.
    """
    sid = body.get("strategy_id")
    if not sid or user is None:
        return
    strat = session.get(Strategy, sid)
    if strat is None or strat.user_id != user.id:
        return
    vno = session.exec(
        select(StrategyVersion.version_no)
        .where(StrategyVersion.strategy_id == sid)
        .order_by(StrategyVersion.version_no.desc())
    ).first()
    sim = body.get("simulation") or {}
    session.add(BacktestRun(
        user_id=user.id, strategy_id=int(sid), version_no=vno, name=strat.name,
        definition=body, result={"metrics": payload.get("metrics") or {}},
        initial_capital=float(sim.get("initial_capital") or 10_000_000),
        start=sim.get("start"), end=sim.get("end")))
    session.commit()


def _make_strategy_resolver(session: Session, user: Optional[User]):
    """strat:<id>[@<버전>] → 저장 전략 definition(자식 spec). 전략 조합(G3) resolver.

    **본인 소유 전략만** 참조 가능(strat.user_id != user.id → None) — 타 유저 전략을
    합성 자산으로 끌어오는 것을 차단(privacy 경계). 버전 미지정 시 현재 정의.
    """
    if user is None:
        return None

    def resolve(token: str):
        sid_str, _, vno_str = str(token).partition("@")
        if not sid_str.isdigit():
            return None
        strat = session.get(Strategy, int(sid_str))
        if strat is None or strat.user_id != user.id:
            return None
        if vno_str:
            if not vno_str.isdigit():
                return None
            ver = session.exec(select(StrategyVersion).where(
                StrategyVersion.strategy_id == int(sid_str),
                StrategyVersion.version_no == int(vno_str))).first()
            return ver.definition if ver else None
        return strat.definition or None

    return resolve


@router.post("/strategy")
def ir_strategy(body: dict, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)):
    """완전한 StrategyIR(유니버스·신호·포지션 4부품·시뮬·스터디) 백테스트.

    study.axis != none이면 펼침 resultset(버킷), 아니면 1회 백테스트 결과.
    저장된 전략(body.strategy_id) 백테스트면 BacktestRun으로 내역 저장.
    """
    # 부분집합 로드 — 두 축으로 전 유니버스 45컬럼(~9.4GB) 빌드를 우회한다:
    #   · single/list: 필요 종목만(load_dataset_for, ~0.1초). 종목 수가 적어 컬럼은 전체여도 가벼움.
    #   · all/screener: 횡단 랭킹이라 전 종목이 필요하지만 **참조 지표 컬럼만**(get_projected,
    #     컬럼 프로젝션) 계산 → ~9.4GB→~2GB. 값은 compute_all과 byte 동일(골든 프로젝션 불변식).
    #   · strat: 조합 등 결정 불가 → 안전하게 전체(get_dataset). 드묾.
    # 무결성 매니페스트는 그 dataset으로 빌드 — 게이트 판정은 동일(생존편향 D-surv는 universe=all 전용).
    # 파싱 실패는 needed=cols=None→전체 경로로 두면 strategy_from_spec이 동일 에러를 반환(중복 검증 회피).
    try:
        _sir = StrategyIR.model_validate(body)
        needed = needed_symbols(_sir)
        cols = needed_columns(_sir)
    except ValidationError:
        needed = cols = None
    if needed is not None:               # single/list — 필요 종목만
        dataset = qc.load_dataset_for(needed)
    elif cols is not None:               # all/screener — 전 종목 × 참조 컬럼만(프로젝션)
        dataset = data_cache.get_projected(cols, symbols=None)
    else:                                # strat: 조합 등 — 안전하게 전체
        dataset = get_dataset()
    manifest = build_dataset_manifest(dataset, version=data_cache.get_version())
    # 무결성 4액션 게이트 가동 — manifest(실측) vs DataSpec(요구). meta는 manifest에서 도출.
    # strict=true(body)면 편향형 경고를 거부로 승격(실전 자금 투입 前 게이트).
    res = strategy_from_spec(
        body, dataset, valid_refs=available_refs(dataset),
        manifest=manifest, strict=bool(body.get("strict", False)),
        strategy_resolver=_make_strategy_resolver(session, user))
    if not res.get("success"):
        return {"success": False, "error": res.get("error"),
                "issues": res.get("issues", [])}
    # 리서치 질의(select·describe 리포트/진단·extremize) — equity/trades 없는 분석 결과.
    # serialize_backtest는 result["trades"]를 요구해 KeyError로 500을 낸다(프로덕션 /ir/strategy
    # 500 근본원인). 분석 결과 dict는 pandas 객체가 없으므로 전 키를 JSON-안전하게 통과시킨다.
    if (res.get("query") in ("select", "describe") or res.get("report")
            or res.get("reduction") == "extremize"):
        out = {k: v for k, v in res.items()
               if k not in ("equity", "benchmark", "trades", "weight")}
        return clean_json(out)
    if res.get("axis"):   # 펼침 resultset (equity Series는 JSON 비호환이라 제외)
        out = {"success": True, "axis": res["axis"], "warnings": res.get("warnings", [])}
        for k in ("buckets", "overall", "axes", "metrics", "compare", "consistency",
                  "windows", "by_regime", "n_events", "basis", "by_window", "relation",
                  "factor_names"):
            if k in res and res[k] is not None:
                out[k] = res[k]
        return clean_json(out)   # NaN/inf→None (allow_nan=False JSONResponse 호환)
    payload = serialize_backtest(res)          # 1회 백테스트
    payload["warnings"] = res.get("warnings", [])
    _persist_ir_backtest(session, user, body, payload)
    return payload
