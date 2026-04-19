"""
Scope Profile → SQL ACL 필터 변환 헬퍼 — Phase 4 (S2).

Scope Profile에서 scope_name에 해당하는 FilterExpression을 조회하고
access_context 변수를 치환하여 SQL WHERE 절로 변환한다.
"""
from __future__ import annotations

import logging
from typing import Any

from app.repositories.scope_profile_repository import ScopeProfileRepository
from app.services.filter_expression import (
    build_sql_filter,
    parse_filter_expression,
    substitute_ctx,
)

logger = logging.getLogger(__name__)


class ScopeFilterResolutionError(RuntimeError):
    """Scope Profile 기반 ACL 필터를 안전하게 해석할 수 없을 때 발생."""


def apply_scope_filter(
    *,
    scope_profile_id: str,
    scope_name: str,
    access_context: dict[str, Any],
    conn,
) -> dict:
    """Scope Profile에서 ACL 필터를 조회하고 SQL 형태로 반환한다.

    Returns:
        {"sql": "AND (...)", "params": [...]}
    """
    try:
        repo = ScopeProfileRepository(conn)
        scope_def = repo.get_definition(scope_profile_id, scope_name)
    except Exception as exc:
        logger.error("scope_filter unexpected repository error: %s", exc)
        raise ScopeFilterResolutionError(
            f"scope profile 조회 실패: profile={scope_profile_id}, scope={scope_name}"
        ) from exc

    if not scope_def:
        raise ScopeFilterResolutionError(
            f"scope profile에 scope 정의가 없습니다: profile={scope_profile_id}, scope={scope_name}"
        )
    if not scope_def.acl_filter:
        raise ScopeFilterResolutionError(
            f"scope profile acl_filter가 비어 있습니다: profile={scope_profile_id}, scope={scope_name}"
        )

    try:
        expr = parse_filter_expression(scope_def.acl_filter)
        expr = substitute_ctx(expr, access_context)
        sql_fragment, params = build_sql_filter(expr)
        if not sql_fragment:
            raise ScopeFilterResolutionError(
                f"scope profile acl_filter가 빈 SQL로 해석되었습니다: profile={scope_profile_id}, scope={scope_name}"
            )
        return {"sql": sql_fragment, "params": params}
    except ValueError as exc:
        logger.warning("scope_filter validation error profile=%s scope=%s: %s",
                       scope_profile_id, scope_name, exc)
        raise ScopeFilterResolutionError(str(exc)) from exc
