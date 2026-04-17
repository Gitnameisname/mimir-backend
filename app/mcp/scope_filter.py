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
        필터 없음 또는 오류 시: {"sql": "", "params": []}
    """
    try:
        repo = ScopeProfileRepository(conn)
        scope_def = repo.get_definition(scope_profile_id, scope_name)
        if not scope_def or not scope_def.acl_filter:
            return {"sql": "", "params": []}

        expr = parse_filter_expression(scope_def.acl_filter)
        expr = substitute_ctx(expr, access_context)
        sql_fragment, params = build_sql_filter(expr)
        return {"sql": sql_fragment, "params": params}

    except ValueError as exc:
        logger.warning("scope_filter validation error profile=%s scope=%s: %s",
                       scope_profile_id, scope_name, exc)
        raise
    except Exception as exc:
        logger.error("scope_filter unexpected error: %s", exc)
        return {"sql": "", "params": []}
