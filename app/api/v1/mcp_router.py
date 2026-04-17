"""
MCP 2025-11-25 HTTP 라우터 — Phase 4 FG4.1.

엔드포인트:
  POST /mcp/initialize           — 핸드셰이크
  POST /mcp/tools/call           — 도구 호출 (search_documents, fetch_node, verify_citation)
  GET  /mcp/resources            — 리소스 목록
  GET  /mcp/resources/read       — 리소스 조회 (URI 파라미터)
  GET  /mcp/prompts              — Prompt Registry 목록

인증: Bearer JWT 또는 X-API-Key (principal_type=agent 권장).
모든 응답은 MCPResponse envelope.

Rate Limit (REC-4.1):
  - 도구 호출: 인증 사용자 20회/분 (LLM 비용·데이터 접근 남용 방지)
  - 스트리밍: 10회/분 (SSE 연결 비용 고려)
  - 읽기 조회: 60회/분
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.rate_limit import limiter
from app.db.connection import get_db
from app.mcp.errors import MCPError
from app.mcp.prompts import list_mcp_prompts
from app.mcp.resources import parse_resource_uri
from app.mcp.tools import tool_fetch_node, tool_search_documents, tool_verify_citation
from app.schemas.mcp import (
    MIMIR_EXTENSIONS,
    TOOL_SCHEMAS,
    AccessContext,
    FetchNodeRequest,
    MCPCapabilities,
    MCPErrorDetail,
    MCPInitializeRequest,
    MCPInitializeResponse,
    MCPMetadata,
    MCPResponse,
    SearchDocumentsRequest,
    VerifyCitationRequest,
)
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# REC-4.1: MCP 도구별 속도 제한
_MCP_TOOL_LIMIT = "20/minute"       # 도구 호출 (JSON)
_MCP_STREAM_LIMIT = "10/minute"     # 도구 호출 (SSE)
_MCP_READ_LIMIT = "60/minute"       # 리소스/프롬프트 조회
_MCP_INIT_LIMIT = "30/minute"       # 핸드셰이크

_SERVER_VERSION = "2.0.0-s2-phase4"
_CURATED_TOOLS = {s["name"] for s in TOOL_SCHEMAS}


def _make_metadata(actor: ActorContext, start: float) -> MCPMetadata:
    return MCPMetadata(
        request_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_id=actor.agent_id,
        execution_time_ms=int((time.monotonic() - start) * 1000),
        trusted=False,
    )


def _ok(data: Any, actor: ActorContext, start: float) -> MCPResponse:
    return MCPResponse(success=True, data=data, metadata=_make_metadata(actor, start))


def _err(code: str, message: str, actor: ActorContext, start: float) -> MCPResponse:
    return MCPResponse(
        success=False,
        error=MCPErrorDetail(code=code, message=message),
        metadata=_make_metadata(actor, start),
    )


# ---------------------------------------------------------------------------
# POST /mcp/initialize
# ---------------------------------------------------------------------------

@router.post(
    "/initialize",
    response_model=MCPResponse,
    summary="MCP 서버 초기화 핸드셰이크 (MCP 2025-11-25)",
)
@limiter.limit(_MCP_INIT_LIMIT)
def mcp_initialize(
    request: Request,
    body: MCPInitializeRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    start = time.monotonic()
    if not actor.is_authenticated:
        return _err("UNAUTHORIZED", "인증이 필요합니다.", actor, start)

    resp = MCPInitializeResponse(
        server_id="mimir-s2",
        version=_SERVER_VERSION,
        protocol_version=body.protocol_version,
        capabilities=MCPCapabilities(
            tools=list(_CURATED_TOOLS),
            resources=True,
            prompts=True,
            tasks=False,
        ),
    )
    return _ok(
        {
            **resp.model_dump(),
            "extensions": MIMIR_EXTENSIONS,
        },
        actor,
        start,
    )


# ---------------------------------------------------------------------------
# POST /mcp/tools/call
# ---------------------------------------------------------------------------

class MCPToolCallBody(BaseModel):
    """MCP 도구 호출 요청 본문 — 도구별 파라미터를 통합."""
    params: dict = Field(default_factory=dict, description="도구별 입력 파라미터")


@router.post(
    "/tools/call",
    response_model=MCPResponse,
    summary="MCP 도구 호출",
)
@limiter.limit(_MCP_TOOL_LIMIT)
def mcp_tool_call(
    request: Request,
    tool_name: str = Query(description="도구 이름: search_documents | fetch_node | verify_citation"),
    body: MCPToolCallBody = ...,
    actor: ActorContext = Depends(resolve_current_actor),
):
    start = time.monotonic()
    if not actor.is_authenticated:
        return _err("UNAUTHORIZED", "인증이 필요합니다.", actor, start)

    if tool_name not in _CURATED_TOOLS:
        return _err("INVALID_REQUEST", f"지원하지 않는 도구: {tool_name!r}", actor, start)

    try:
        with get_db() as conn:
            data = _dispatch_tool(tool_name, body.params, actor, conn)
        return _ok(data.model_dump() if hasattr(data, "model_dump") else data, actor, start)
    except MCPError as exc:
        return _err(exc.code.value, exc.message, actor, start)
    except Exception as exc:
        logger.error("mcp tool_call error tool=%s: %s", tool_name, exc)
        return _err("INTERNAL_ERROR", f"도구 실행 오류: {exc}", actor, start)


def _dispatch_tool(tool_name: str, body: dict, actor: ActorContext, conn) -> Any:
    if tool_name == "search_documents":
        req = SearchDocumentsRequest(**body)
        return tool_search_documents(req, actor, conn)
    if tool_name == "fetch_node":
        req = FetchNodeRequest(**body)
        return tool_fetch_node(req, actor, conn)
    if tool_name == "verify_citation":
        req = VerifyCitationRequest(**body)
        return tool_verify_citation(req, actor, conn)
    raise ValueError(f"Unknown tool: {tool_name}")


# ---------------------------------------------------------------------------
# POST /mcp/tools/call/stream  (Streamable HTTP / SSE)
# ---------------------------------------------------------------------------

@router.post(
    "/tools/call/stream",
    summary="MCP 도구 호출 — SSE 스트리밍",
)
@limiter.limit(_MCP_STREAM_LIMIT)
async def mcp_tool_call_stream(
    request: Request,
    tool_name: str = Query(),
    body: MCPToolCallBody = ...,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """search_documents 결과를 SSE 스트림으로 반환한다."""
    start = time.monotonic()
    if not actor.is_authenticated:
        async def _denied():
            yield _sse_chunk({"success": False, "error": {"code": "UNAUTHORIZED", "message": "인증이 필요합니다."}})
        return StreamingResponse(_denied(), media_type="text/event-stream")

    import json

    async def generate():
        try:
            with get_db() as conn:
                data = _dispatch_tool(tool_name, body.params, actor, conn)
            result = data.model_dump() if hasattr(data, "model_dump") else data
            items = result.get("results", [result])
            for item in items:
                yield _sse_chunk({"success": True, "data": item})
            meta = _make_metadata(actor, start).model_dump()
            yield f"event: done\ndata: {json.dumps({'metadata': meta})}\n\n"
        except MCPError as exc:
            yield _sse_chunk({"success": False, "error": {"code": exc.code.value, "message": exc.message}})
        except Exception as exc:
            yield _sse_chunk({"success": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}})

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse_chunk(data: dict) -> str:
    import json
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_error(code: str, msg: str):
    yield _sse_chunk({"success": False, "error": {"code": code, "message": msg}})


# ---------------------------------------------------------------------------
# GET /mcp/resources
# ---------------------------------------------------------------------------

@router.get(
    "/resources",
    response_model=MCPResponse,
    summary="MCP 리소스 목록 조회",
)
@limiter.limit(_MCP_READ_LIMIT)
def mcp_list_resources(
    request: Request,
    document_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
):
    start = time.monotonic()
    if not actor.is_authenticated:
        return _err("UNAUTHORIZED", "인증이 필요합니다.", actor, start)

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if document_id:
                    cur.execute(
                        """
                        SELECT d.id AS doc_id, d.title, v.id AS ver_id, n.id AS node_id, n.title AS node_title
                        FROM nodes n
                        JOIN versions v ON v.id = n.version_id
                        JOIN documents d ON d.id = v.document_id
                        WHERE d.id = %s
                        ORDER BY n.order_index LIMIT %s
                        """,
                        (document_id, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT d.id AS doc_id, d.title, v.id AS ver_id, n.id AS node_id, n.title AS node_title
                        FROM nodes n
                        JOIN versions v ON v.id = n.version_id
                        JOIN documents d ON d.id = v.document_id
                        ORDER BY d.created_at DESC, n.order_index LIMIT %s
                        """,
                        (limit,),
                    )
                rows = cur.fetchall()
        resources = [
            {
                "uri": f"mimir://documents/{r['doc_id']}/versions/{r['ver_id']}/nodes/{r['node_id']}",
                "mime_type": "text/plain",
                "description": f"{r['title']} - {r.get('node_title') or r['node_id']}",
            }
            for r in rows
        ]
        return _ok({"resources": resources, "total": len(resources)}, actor, start)
    except Exception as exc:
        logger.error("mcp list_resources error: %s", exc)
        return _err("INTERNAL_ERROR", str(exc), actor, start)


# ---------------------------------------------------------------------------
# GET /mcp/resources/read
# ---------------------------------------------------------------------------

@router.get(
    "/resources/read",
    response_model=MCPResponse,
    summary="MCP 리소스 조회 (mimir:// URI)",
)
@limiter.limit(_MCP_READ_LIMIT)
def mcp_read_resource(
    request: Request,
    uri: str = Query(description="mimir://documents/.../versions/.../nodes/... 형식"),
    actor: ActorContext = Depends(resolve_current_actor),
):
    start = time.monotonic()
    if not actor.is_authenticated:
        return _err("UNAUTHORIZED", "인증이 필요합니다.", actor, start)

    resource = parse_resource_uri(uri)
    if not resource:
        return _err("INVALID_REQUEST", f"올바르지 않은 URI: {uri!r}", actor, start)

    try:
        with get_db() as conn:
            req = FetchNodeRequest(
                document_id=resource.document_id,
                version_id=resource.version_id,
                node_id=resource.node_id,
            )
            data = tool_fetch_node(req, actor, conn)
        return _ok(data.model_dump(), actor, start)
    except MCPError as exc:
        return _err(exc.code.value, exc.message, actor, start)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc), actor, start)


# ---------------------------------------------------------------------------
# GET /mcp/prompts
# ---------------------------------------------------------------------------

@router.get(
    "/prompts",
    response_model=MCPResponse,
    summary="MCP Prompt Registry 목록",
)
@limiter.limit(_MCP_READ_LIMIT)
def mcp_list_prompts(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    start = time.monotonic()
    if not actor.is_authenticated:
        return _err("UNAUTHORIZED", "인증이 필요합니다.", actor, start)

    with get_db() as conn:
        prompts = list_mcp_prompts(conn)
    return _ok({"prompts": prompts, "total": len(prompts)}, actor, start)


# ---------------------------------------------------------------------------
# GET /mcp/tools
# ---------------------------------------------------------------------------

@router.get(
    "/tools",
    response_model=MCPResponse,
    summary="사용 가능한 MCP 도구 목록 (Tool Schema 포함)",
)
@limiter.limit(_MCP_READ_LIMIT)
def mcp_list_tools(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
):
    start = time.monotonic()
    return _ok({"tools": TOOL_SCHEMAS, "extensions": MIMIR_EXTENSIONS}, actor, start)
