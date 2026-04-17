"""
Prompt Registry MCP 노출 — Phase 4 FG4.1.

Phase 1에서 구축한 Prompt Registry의 활성 프롬프트를 MCP prompts로 노출한다.
Prompt Registry가 없는 환경에서는 빈 목록 반환 (폐쇄망 안전).
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def list_mcp_prompts(conn) -> list[dict]:
    """DB에서 활성 프롬프트를 조회하여 MCP prompts 형식으로 반환한다."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, description, template, variables
                FROM prompt_templates
                WHERE is_active = TRUE
                ORDER BY name
                LIMIT 100
                """
            )
            rows = cur.fetchall()
        return [_row_to_mcp_prompt(r) for r in rows]
    except Exception as exc:
        # prompt_templates 테이블이 없거나 오류 시 빈 목록 반환
        logger.debug("prompt registry not available: %s", exc)
        return []


def _row_to_mcp_prompt(row) -> dict:
    variables = row.get("variables") or []
    if isinstance(variables, str):
        import json
        variables = json.loads(variables)
    return {
        "name": f"mimir-{row['id']}",
        "description": row.get("description") or row.get("name") or "",
        "arguments": [
            {"name": v, "description": f"{v} 변수", "required": True}
            for v in (variables if isinstance(variables, list) else [])
        ],
    }
