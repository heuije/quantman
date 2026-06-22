"""블록 IR 백테스트 라우터 (신규 — 기존 /backtest·빌더와 병행, 충돌 0).

명세 §11·P1-6. 노코드 블록 트리(Node)를 받아 검증→백테스트→결과(+경고)를 반환.
기존 라우터는 그대로 두고 새 prefix /ir 로만 추가한다.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

import quant_core as qc
from quant_core.blocks import DatasetMeta, available_refs, catalog_spec
from quant_core.blocks.node import Node, referenced_symbols
from quant_core.data.feeds import estimate_kr, news_kr
from quant_core.ir_engine import (StrategyIR, backtest_from_spec, build_strategy_excel,
                                  needed_columns, needed_symbols, strategy_from_spec,
                                  validate_strategy)

from .. import data_cache, kis_master_cache
from ..data_cache import get_dataset
from ..data_manifest import build_dataset_manifest
from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, Strategy, StrategyVersion, User
from ..serialize import serialize_backtest, serialize_ir_result

router = APIRouter(prefix="/ir", tags=["ir"])

_log = logging.getLogger("app.routers.ir")
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _load_ir_dataset(body: dict):
    """IR 본문 → (dataset, manifest). /ir/strategy·export.xlsx 공용 — 단일 출처.

    부분집합 로드로 전 유니버스 45컬럼(~9.4GB) 빌드를 우회:
      · single/list: 필요 종목만(load_dataset_for). · all/screener: 전 종목 × 참조 컬럼만
        (get_projected; SELECT는 최근 ~400행만 — OOM·타임아웃 회피). · strat/파싱실패:
        안전하게 전체(get_dataset).
    무결성 매니페스트는 그 dataset으로 빌드 — SELECT(읽기전용)는 skip(게이트 미가동).
    """
    is_select = False
    try:
        _sir = StrategyIR.model_validate(body)
        needed = needed_symbols(_sir)
        cols = needed_columns(_sir)
        is_select = _sir.query == "select"
    except ValidationError:
        needed = cols = None
    if needed is not None:
        dataset = qc.load_dataset_for(needed)
    elif cols is not None:
        dataset = data_cache.get_projected(cols, symbols=None,
                                           recent_days=400 if is_select else None)
    else:
        dataset = get_dataset()
    manifest = (None if is_select
                else build_dataset_manifest(dataset, version=data_cache.get_version()))
    return dataset, manifest


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


def _attach_symbol_news(out: dict) -> dict:
    """단일 360 리포트(describe·report="single")에 '왜 움직였나' 뉴스 facet을 붙인다.

    뉴스는 라이브 네트워크·env키(NAVER) 의존이라 **결정적 엔진(run_describe_report) 밖**, 서버
    엣지에서 조회한다(엔진 골든 불변식 유지). 종목명은 /symbols와 동일 출처(kis_master_cache)로
    해석 — 코드("005930")→네이버 검색어("삼성전자"). 이름 미해석·키 미설정이면 빈 리스트(가짜
    채움 0). 단일 리포트가 아니면 무변경(포트 진단·select·extremize는 대상 종목이 1개가 아님).
    """
    if out.get("report") != "single":
        return out
    name = kis_master_cache.get_name(out.get("symbol") or "")
    out["news"] = news_kr.fetch_news(name, display=5) if name else []
    return out


def _attach_symbol_estimates(out: dict) -> dict:
    """단일 360 리포트에 forward 추정실적 facet(estimate.earnings_kr·FnGuide)을 붙인다.

    추정실적은 라이브 스크래핑 의존이라 결정적 엔진(run_describe_report) 밖, 서버 엣지에서
    조회한다(골든 불변·describe 함수 미수정). 데이터 없으면 미부착(가짜 0 금지). forward PER=
    현재가/추정EPS = 현시점 전망(PIT 백테스트 아님). 단일 리포트가 아니면 무변경.
    """
    if out.get("report") != "single":
        return out
    blk = estimate_kr.estimate_block(str(out.get("symbol") or ""),
                                     last_price=(out.get("price") or {}).get("last"))
    if blk:
        out["estimates"] = blk
    return out


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
    dataset, manifest = _load_ir_dataset(body)
    # 무결성 4액션 게이트 가동 — manifest(실측) vs DataSpec(요구). meta는 manifest에서 도출.
    # strict=true(body)면 편향형 경고를 거부로 승격(실전 자금 투입 前 게이트).
    res = strategy_from_spec(
        body, dataset, valid_refs=available_refs(dataset),
        manifest=manifest, strict=bool(body.get("strict", False)),
        strategy_resolver=_make_strategy_resolver(session, user))
    if not res.get("success"):
        return {"success": False, "error": res.get("error"),
                "issues": res.get("issues", [])}
    # 직렬화 정책은 serialize_ir_result(단일 출처·챗 도구와 공유). 라우터 고유 부가처리만 분기:
    # analysis→단일 360 '왜 움직였나' 뉴스 facet, backtest→DB persist.
    payload, kind = serialize_ir_result(res)
    if kind == "analysis":
        _attach_symbol_news(payload)
        _attach_symbol_estimates(payload)
    elif kind == "backtest":
        _persist_ir_backtest(session, user, body, payload)
    return payload


@router.post("/strategy/export.xlsx")
def ir_strategy_export(body: dict, user: User = Depends(get_current_user),
                       session: Session = Depends(get_session)):
    """StrategyIR 분석 → 데이터 + (라이브)수식 '증빙' 엑셀(.xlsx).

    /ir/strategy 와 같은 본문·실행 경로(LLM 없음 — 토큰 0). 결과만 주지 않고 '어떤 데이터를
    어떤 연산으로' 산출했는지 엑셀로 증빙한다(선물 export 취지). **전 분석유형 지원** —
    build_strategy_excel이 결과 형상(simulate·sweep·period·extremize·select·describe·
    relate·event·signal)별로 전용 시트 구성으로 직렬화한다(메타분석은 감사표).

    뿌리④ 계측: 무거운 동기 경로(데이터 로드·spec 실행·excel 빌드)의 단계별 소요를 INFO로
    남기고, 500 크래시 시 실제 예외·단계·컨텍스트를 ERROR(exc_info)로 캡처한다 — 드물게
    트리거되는 export 크래시의 근본을 다음 발생 시 즉시 특정(추측 수정 회피·Iron Law).
    """
    _t0 = time.monotonic()
    q = str(body.get("query") or "simulate")
    try:
        dataset, manifest = _load_ir_dataset(body)
        _t_load = time.monotonic()
        res = strategy_from_spec(
            body, dataset, valid_refs=available_refs(dataset),
            manifest=manifest, strict=bool(body.get("strict", False)),
            strategy_resolver=_make_strategy_resolver(session, user))
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("error") or "분석 실행 실패")
        _t_run = time.monotonic()
        try:
            sir = StrategyIR.model_validate(body)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=f"전략 정의 오류: {e.errors()[0]['msg']}")
        try:
            xlsx = build_strategy_excel(sir, dataset, res)   # 형상별 디스패치(P2 — 전 분석유형)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"엑셀 내보내기 미지원 형상: {e}")
        _end = time.monotonic()
        _log.info("xlsx export ok: query=%s syms=%d load=%.1fs run=%.1fs build=%.1fs total=%.1fs",
                  q, len(dataset), _t_load - _t0, _t_run - _t_load, _end - _t_run, _end - _t0)
    except HTTPException:
        raise   # 400류(검증·미지원 형상)는 정상 흐름 — 크래시 로깅 대상 아님
    except Exception:   # noqa: BLE001 — 500 크래시 근본 진단용 캡처 후 재전파(상태코드 불변)
        _log.error("xlsx export 500 실패: query=%s elapsed=%.1fs",
                   q, time.monotonic() - _t0, exc_info=True)
        raise
    # 한글 전략명은 RFC 5987 filename*(UTF-8)로, 호환 위해 ASCII filename 폴백 병기.
    fname = quote(f"{sir.name or 'backtest'}.xlsx")
    return Response(
        content=xlsx, media_type=_XLSX_MEDIA,
        headers={"Content-Disposition":
                 f"attachment; filename=\"strategy_backtest.xlsx\"; filename*=UTF-8''{fname}"})
