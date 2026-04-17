"""
FilterExpression 파서 및 $ctx 동적 치환 — Phase 4 (S2).

S2 원칙 ⑤: 접근 범위는 하드코딩 금지 — ScopeProfile의 acl_filter JSON을
파싱하여 실행 시 access_context 변수로 치환한다.

보안 원칙:
  - $ctx 변수 화이트리스트 검증 (인젝션 방어)
  - 허용되지 않은 필드/연산자는 ValueError 발생 → API 레이어에서 400 처리
  - acl_filter의 구조는 and/or 두 단계만 지원 (중첩 없음)
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.models.scope_profile import FilterCondition, FilterExpression

logger = logging.getLogger(__name__)

# $ctx 변수 패턴 — 화이트리스트
_CTX_VAR_PATTERN = re.compile(r"^\$ctx\.([a-z_]+)$")
_ALLOWED_CTX_KEYS = frozenset({
    "organization_id", "team_id", "user_id", "permissions",
})


def parse_filter_expression(raw: dict) -> FilterExpression:
    """acl_filter JSON dict를 FilterExpression으로 파싱한다.

    Args:
        raw: {"and": [...], "or": [...]} 형태의 dict

    Returns:
        FilterExpression 객체

    Raises:
        ValueError: 허용되지 않은 필드/연산자/구조
    """
    and_conditions: list[FilterCondition] = []
    or_conditions: list[FilterCondition] = []

    for cond_raw in raw.get("and", []):
        cond = _parse_condition(cond_raw)
        cond.validate()
        and_conditions.append(cond)

    for cond_raw in raw.get("or", []):
        cond = _parse_condition(cond_raw)
        cond.validate()
        or_conditions.append(cond)

    return FilterExpression(and_=and_conditions, or_=or_conditions)


def _parse_condition(raw: dict) -> FilterCondition:
    field = raw.get("field", "")
    op = raw.get("op", "")
    value = raw.get("value")
    if not field or not op:
        raise ValueError(f"FilterCondition 필수 필드 누락: {raw!r}")
    return FilterCondition(field=field, op=op, value=value)


def substitute_ctx(expr: FilterExpression, access_context: dict[str, Any]) -> FilterExpression:
    """$ctx.* 변수를 access_context 값으로 치환한다.

    보안: 허용되지 않은 $ctx 키는 ValueError 발생.

    Args:
        expr:           파싱된 FilterExpression
        access_context: {organization_id, team_id, user_id, ...} 딕셔너리

    Returns:
        치환된 FilterExpression (새 객체)
    """
    return FilterExpression(
        and_=[_substitute_condition(c, access_context) for c in expr.and_],
        or_=[_substitute_condition(c, access_context) for c in expr.or_],
    )


def _substitute_condition(cond: FilterCondition, ctx: dict[str, Any]) -> FilterCondition:
    value = cond.value
    if isinstance(value, str):
        value = _resolve_ctx_var(value, ctx)
    elif isinstance(value, list):
        value = [_resolve_ctx_var(v, ctx) if isinstance(v, str) else v for v in value]
    return FilterCondition(field=cond.field, op=cond.op, value=value)


def _resolve_ctx_var(value: str, ctx: dict[str, Any]) -> Any:
    m = _CTX_VAR_PATTERN.match(value)
    if not m:
        return value  # 리터럴값 — 그대로 반환

    key = m.group(1)
    if key not in _ALLOWED_CTX_KEYS:
        raise ValueError(
            f"허용되지 않은 $ctx 변수: $ctx.{key!r}. "
            f"허용 목록: {sorted(_ALLOWED_CTX_KEYS)}"
        )

    if key not in ctx:
        logger.warning("$ctx.%s requested but not in access_context — using None", key)
        return None

    return ctx[key]


def build_sql_filter(expr: FilterExpression) -> tuple[str, list[Any]]:
    """FilterExpression을 SQL WHERE 절과 파라미터 리스트로 변환한다.

    Returns:
        (sql_fragment, params) — sql_fragment는 'AND (...)' 형태
        expr가 empty이면 ('', []) 반환
    """
    if expr.is_empty():
        return "", []

    clauses: list[str] = []
    params: list[Any] = []

    for cond in expr.and_:
        clause, p = _cond_to_sql(cond)
        clauses.append(clause)
        params.extend(p)

    if expr.or_:
        or_clauses: list[str] = []
        for cond in expr.or_:
            clause, p = _cond_to_sql(cond)
            or_clauses.append(clause)
            params.extend(p)
        clauses.append("(" + " OR ".join(or_clauses) + ")")

    if not clauses:
        return "", []

    return "AND (" + " AND ".join(clauses) + ")", params


_FIELD_COLUMN_MAP = {
    "organization_id": "d.organization_id",
    "team_id": "d.team_id",
    "visibility": "d.visibility",
    "classification": "d.classification",
    "document_type": "d.document_type",
    "is_public": "dc.is_public",
    "accessible_roles": "dc.accessible_roles",
    "accessible_org_ids": "dc.accessible_org_ids",
}


def _cond_to_sql(cond: FilterCondition) -> tuple[str, list[Any]]:
    col = _FIELD_COLUMN_MAP.get(cond.field, cond.field)
    if cond.op == "eq":
        return f"{col} = %s", [cond.value]
    if cond.op == "neq":
        return f"{col} != %s", [cond.value]
    if cond.op == "in":
        vals = list(cond.value) if isinstance(cond.value, (list, tuple)) else [cond.value]
        placeholders = ",".join(["%s"] * len(vals))
        return f"{col} IN ({placeholders})", vals
    if cond.op == "not_in":
        vals = list(cond.value) if isinstance(cond.value, (list, tuple)) else [cond.value]
        placeholders = ",".join(["%s"] * len(vals))
        return f"{col} NOT IN ({placeholders})", vals
    if cond.op == "contains":
        return f"%s = ANY({col})", [cond.value]
    raise ValueError(f"Unsupported op in SQL builder: {cond.op!r}")
