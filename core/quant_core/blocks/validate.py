"""메타규칙 엔진 — 블록 조립의 정합성·완결성 검증 (비전 §8).

명세 §5. 규칙:
  0  데이터 가용성 선검사   — 참조 데이터가 존재하는가
  1  타입 호환성            — 슬롯이 요구하는 ValueType과 자식 출력 타입 일치
  2  형태 호환성            — 횡단/그룹 블록은 PANEL 입력 필요(series 무의미)
  3  조립 순서/구조         — 필수 슬롯 존재·미정의 슬롯 없음·필수 param 존재
  5  완결성·기본값          — 빈 param을 catalog 기본값으로 채움(apply_defaults)

충돌 우선순위: 무결성(4, integrity.py) > 정합성(0·1·2·3) > 완결성(5).
integrity.py가 SEV_INTEGRITY 이슈를 만들어 prioritize로 함께 정렬된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import get, has
from .context import SELF
from .node import OP_CONST, OP_DATA, Node
from .types import Shape

# 심각도 — 비전 우선순위(무결성＞정합성＞완결성)를 수치화.
# is_error 임계 = SEV_ERROR(30). 무결성도 하드 위반(look-ahead)은 차단(40),
# 자문(PIT 미태깅·생존편향)은 경고(25)로 표시만 한다.
SEV_INTEGRITY = 40        # 규칙4 하드 위반 (look-ahead·비-causal) — 거부
SEV_ERROR = 30            # 규칙0·1·2·3 정합성 — 거부
SEV_INTEGRITY_WARN = 25   # 규칙4 자문 (PIT·생존편향) — 표시(차단 안 함)
SEV_INFO = 10             # 규칙5 완결성 — 자동 보정

# eval_fn이 기본값 없이 params[..]로 접근하는 필수 잎 빈칸.
_REQUIRED_PARAMS = {
    OP_DATA: ["ref"], OP_CONST: ["value"], "compare": ["op"],
    "binary": ["op"], "unary": ["func"], "cross": ["direction"],
    "bucket": ["edges"], "is_in": ["values"],
}


@dataclass(frozen=True)
class Issue:
    rule: str        # "R0".."R5" / "catalog" / "integrity"
    severity: int
    message: str
    path: str = "root"

    @property
    def is_error(self) -> bool:
        return self.severity >= SEV_ERROR


# ── 형태 추론 (정적, 데이터 불요) ─────────────────────────────────────────────

def infer_shape(node: Node) -> Shape:
    """블록 트리의 출력 형태를 정적 추론. "SYM.X"(브로드캐스트)는 SERIES로 본다."""
    if node.op == OP_DATA:
        ref = str(node.params.get("ref", ""))
        if "." in ref and not ref.startswith(SELF + "."):
            return Shape.SERIES
        return Shape.PANEL
    if node.op == OP_CONST:
        return Shape.PANEL  # 스칼라 브로드캐스트 — 형태 결정에 중립
    non_const = [infer_shape(c) for c in node.inputs.values() if c.op != OP_CONST]
    pool = non_const or [infer_shape(c) for c in node.inputs.values()]
    if any(s == Shape.PANEL for s in pool):
        return Shape.PANEL
    return pool[0] if pool else Shape.PANEL


# ── 데이터 가용성 (규칙0) ─────────────────────────────────────────────────────

def available_refs(data: dict) -> set[str]:
    """dataset에서 유효 참조 집합(심볼 ∪ 전 컬럼)을 만든다.

    df가 None/빈 심볼 키는 제외한다 — 키만 있고 데이터가 없으면 "SYM.x" 참조가
    NaN→False로 조용히 0건이 되므로(조용한 오답 부류), 참조 시점에 규칙0(R0)이
    정직 거부하도록 '참조 가능'으로 치지 않는다(__SELF__.x의 컬럼 실재 요구와 대칭).
    """
    refs: set[str] = set()
    for sym, df in data.items():
        if df is not None and not getattr(df, "empty", True):
            refs.add(sym)
            refs.update(map(str, df.columns))
    return refs


def _ref_ok(ref: str, valid: set[str]) -> bool:
    if "." in ref:
        sym, indic = ref.split(".", 1)
        if sym == SELF:
            return indic in valid
        return sym in valid
    return ref in valid


# ── 검증 walker ───────────────────────────────────────────────────────────────

def validate(node: Node, valid_refs: set[str] | None = None) -> list[Issue]:
    """블록 트리를 검증해 Issue 목록을 반환. error 이슈가 없으면 평가 안전.

    valid_refs를 주면 규칙0(데이터 가용성)도 검사한다.
    """
    issues: list[Issue] = []
    _walk(node, "root", valid_refs, issues)
    return prioritize(issues)


def _walk(node: Node, path: str, valid_refs, issues: list[Issue]) -> None:
    if not has(node.op):
        issues.append(Issue("catalog", SEV_ERROR, f"미등록 블록: {node.op}", path))
        return
    bdef = get(node.op)

    # 규칙3 — 필수 param 존재
    for p in _REQUIRED_PARAMS.get(node.op, []):
        if p not in node.params:
            issues.append(Issue("R3", SEV_ERROR, f"필수 param 누락: {p}", path))

    # 규칙0 — 데이터 가용성
    if node.op == OP_DATA and valid_refs is not None:
        ref = str(node.params.get("ref", ""))
        if ref and not _ref_ok(ref, valid_refs):
            issues.append(Issue("R0", SEV_ERROR, f"데이터 없음: {ref}", path))

    # 슬롯 구조·타입 (규칙1·3)
    if bdef.variadic:
        if not node.inputs:
            issues.append(Issue("R3", SEV_ERROR, "가변 슬롯 블록에 입력이 1개 이상 필요", path))
        req_type = bdef.variadic_type
        for slot, child in node.inputs.items():
            _check_child(child, req_type, f"{path}/{slot}", valid_refs, issues)
    else:
        for slot, req_type in bdef.slots.items():
            if slot not in node.inputs:
                issues.append(Issue("R3", SEV_ERROR, f"필수 슬롯 누락: {slot}", path))
        for slot, child in node.inputs.items():
            if slot not in bdef.slots:
                issues.append(Issue("R3", SEV_ERROR, f"미정의 슬롯: {slot}", f"{path}/{slot}"))
                continue
            _check_child(child, bdef.slots[slot], f"{path}/{slot}", valid_refs, issues)

    # 규칙2 — 횡단/그룹 블록은 PANEL 입력 필요
    if bdef.requires_panel:
        sig = node.inputs.get("signal")
        if sig is not None and infer_shape(sig) == Shape.SERIES:
            issues.append(Issue("R2", SEV_ERROR,
                                f"{node.op}는 PANEL 입력 필요 — series(시장 단일값)엔 무의미", path))


def _check_child(child: Node, req_type, path: str, valid_refs, issues: list[Issue]) -> None:
    if has(child.op):
        actual = get(child.op).out_type
        if req_type is not None and actual != req_type:
            issues.append(Issue("R1", SEV_ERROR,
                                f"타입 불일치: 슬롯은 {req_type.value} 요구, {child.op}는 {actual.value}",
                                path))
    _walk(child, path, valid_refs, issues)


def prioritize(issues: list[Issue]) -> list[Issue]:
    """무결성＞정합성＞완결성 순으로 정렬 (안정 정렬 — 동일 심각도는 발견 순서 유지)."""
    return sorted(issues, key=lambda i: -i.severity)


def is_valid(node: Node, valid_refs: set[str] | None = None) -> bool:
    return not any(i.is_error for i in validate(node, valid_refs))


# ── 의미 검증 (M-rules) — 타입은 유효하나 의미가 공허·모순인 트리 ───────────────
# validate()는 타입·구조만 본다. 여기선 "백테스트가 무의미해지는" 논리 퇴화를
# 정적으로 잡는다(완벽한 정리증명은 불가능 — 열거 가능한 흔한 클래스만 포착).

_DEGEN_BINARY_OPS = {"-", "/"}          # X−X≡0, X/X≡1 → 상수(무의미)
_WINDOWED_PARAMS = ("window", "lag", "days")   # 음수=런타임 오류, 0=전기간 NaN→무거래


def has_market_source(node: Node) -> bool:
    """트리에 const가 아닌 소스 잎(data·attribute·calendar 등)이 ≥1개 있는가.

    모든 잎이 const면 시장에 반응하지 않는 순수 상수/산술식 → 신호로 무의미.
    (예: const(5)>const(0) → 항상 매수. attribute/calendar는 종목·시점에 따라
    변하므로 정당한 소스로 인정.)
    """
    if not node.inputs:                  # 터미널(잎)
        return node.op != OP_CONST
    return any(has_market_source(c) for c in node.inputs.values())


def meaningfulness_issues(node: Node, path: str = "signal") -> list[Issue]:
    """타입 유효하나 의미 공허·모순인 패턴 검출 — 동어반복·모순·자기참조·퇴화 윈도우.

    호출자가 signal·exit.condition 등 트리 루트마다 적용한다. M1(시장참조)은
    트리 전체 속성이라 has_market_source로 호출자(validate_strategy)가 별도 검사.
    """
    issues: list[Issue] = []
    _walk_meaning(node, path, issues)
    return issues


def _walk_meaning(node: Node, path: str, issues: list[Issue]) -> None:
    # M3 — 윈도우/지연 파라미터 ≥ 1 (음수=eval 크래시, 0=전기간 NaN→조용한 무거래)
    for p in _WINDOWED_PARAMS:
        v = node.params.get(p)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v < 1:
            issues.append(Issue("M-window", SEV_ERROR,
                                f"{node.op}의 {p}는 1 이상이어야 합니다 (현재 {v}).", path))

    # M2 — 반사적 퇴화: 좌우 부분트리가 구조적으로 동일(Node ==는 깊은 비교)
    kids = list(node.inputs.values())
    if len(kids) == 2 and kids[0] == kids[1]:
        if node.op in ("compare", "cross"):
            issues.append(Issue("M-degen", SEV_ERROR,
                                f"양변이 동일해 항상 같은 결과입니다 — 무의미한 {node.op} 조건.", path))
        elif node.op == "binary" and node.params.get("op") in _DEGEN_BINARY_OPS:
            issues.append(Issue("M-degen", SEV_ERROR,
                                f"같은 값끼리 '{node.params.get('op')}' 연산은 상수가 됩니다 — 무의미.", path))

    # M2b — 상수 vs 상수 비교(항상 참/거짓인 상수 조건) — 경고(상위 트리가 의미를 가질 수 있음)
    if (node.op == "compare" and len(kids) == 2
            and all(k.op == OP_CONST for k in kids)):
        issues.append(Issue("M-const-cmp", SEV_INTEGRITY_WARN,
                            "두 상수를 비교 — 항상 참/거짓인 상수 조건입니다.", path))

    for slot, child in node.inputs.items():
        _walk_meaning(child, f"{path}/{slot}", issues)


# ── 규칙5 — 완결성·기본값 ─────────────────────────────────────────────────────

def apply_defaults(node: Node) -> Node:
    """빠진 잎 빈칸(param)을 catalog 기본값으로 채운 새 트리 반환 (retail 친화)."""
    params = dict(node.params)
    if has(node.op):
        for k, v in get(node.op).param_defaults.items():
            params.setdefault(k, v)
    inputs = {k: apply_defaults(v) for k, v in node.inputs.items()}
    return Node(op=node.op, inputs=inputs, params=params)
