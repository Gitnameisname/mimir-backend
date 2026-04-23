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
from app.mcp.tools import (
    tool_fetch_node,
    tool_search_documents,
    tool_vectorization_status,
    tool_verify_citation,
)
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
from app.security.prompt_injection import (
    InjectionDetectionResult,
    content_directive_separator,
    prompt_injection_detector,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# REC-4.1: MCP 도구별 속도 제한 (IP 기반, 전역)
_MCP_TOOL_LIMIT = "20/minute"       # 도구 호출 (JSON)
_MCP_STREAM_LIMIT = "10/minute"     # 도구 호출 (SSE)
_MCP_READ_LIMIT = "60/minute"       # 리소스/프롬프트 조회
_MCP_INIT_LIMIT = "30/minute"       # 핸드셰이크

# REC-4.1 (FG5.3 이월): 에이전트별 Rate Limit — Valkey 카운터 기반
_AGENT_RATE_LIMITS: dict[str, int] = {
    "tool_call": 20,    # /mcp/tools/call — 분당 최대
    "stream": 10,       # /mcp/tools/call/stream
    "read": 60,         # /mcp/resources, /mcp/prompts
    "init": 30,         # /mcp/initialize
}
_AGENT_RATE_WINDOW = 60  # seconds


def _check_agent_rate_limit(agent_id: str, endpoint: str) -> bool:
    """에이전트별 Valkey 기반 Rate Limit 검사.

    반환: True = 통과, False = 한도 초과.
    Valkey 연결 실패 시 True(통과) — 서비스 degradation 방지 (S2 원칙 ⑦).
    """
    limit = _AGENT_RATE_LIMITS.get(endpoint, 60)
    key = f"agent:{agent_id}:rate:{endpoint}"
    try:
        from app.cache.valkey import get_valkey
        r = get_valkey()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _AGENT_RATE_WINDOW)
        results = pipe.execute()
        count = results[0]
        return count <= limit
    except Exception as exc:
        logger.warning("Agent rate limit Valkey check failed for %s: %s", agent_id, exc)
        return True  # fail-open

_SERVER_VERSION = "2.0.0-s2-phase5"
_CURATED_TOOLS = {s["name"] for s in TOOL_SCHEMAS}


def _make_metadata(
    actor: ActorContext,
    start: float,
    injection: Optional[InjectionDetectionResult] = None,
) -> MCPMetadata:
    return MCPMetadata(
        request_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_id=actor.agent_id,
        execution_time_ms=int((time.monotonic() - start) * 1000),
        trusted=False,
        injection_risk=injection.injection_risk if injection else False,
        injection_patterns_detected=(
            injection.injection_patterns_detected if injection else []
        ),
        source_data_untrusted=True,
    )


def _ok(
    data: Any,
    actor: ActorContext,
    start: float,
    injection: Optional[InjectionDetectionResult] = None,
) -> MCPResponse:
    return MCPResponse(
        success=True,
        data=data,
        metadata=_make_metadata(actor, start, injection),
    )


def _err(code: str, message: str, actor: ActorContext, start: float) -> MCPResponse:
    return MCPResponse(
        success=False,
        error=MCPErrorDetail(code=code, message=message),
        metadata=_make_metadata(actor, start),
    )


def _run_injection_detection(data: Any) -> InjectionDetectionResult:
    """도구 결과에서 검색 텍스트를 추출하여 인젝션 탐지를 실행한다."""
    texts: list[str] = []
    if isinstance(data, dict):
        # search_documents: results 목록
        for item in data.get("results", []):
            if isinstance(item, dict):
                texts.append(item.get("content") or item.get("snippet") or "")
        # fetch_node: content 직접
        if "content" in data:
            texts.append(str(data["content"]))
    elif hasattr(data, "model_dump"):
        return _run_injection_detection(data.model_dump())

    if not texts:
        from app.security.prompt_injection import InjectionDetectionResult
        return InjectionDetectionResult(injection_risk=False, injection_patterns_detected=[])

    results = prompt_injection_detector.detect_batch(texts)
    return prompt_injection_detector.merge_results(results)


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

    # FG5.3 REC-4.1: 에이전트별 Rate Limit 검사
    if actor.agent_id and not _check_agent_rate_limit(actor.agent_id, "tool_call"):
        return _err("RATE_LIMIT", f"에이전트 {actor.agent_id}의 도구 호출 한도를 초과했습니다.", actor, start)

    if tool_name not in _CURATED_TOOLS:
        return _err("INVALID_REQUEST", f"지원하지 않는 도구: {tool_name!r}", actor, start)

    try:
        with get_db() as conn:
            data = _dispatch_tool(tool_name, body.params, actor, conn)
        raw = data.model_dump() if hasattr(data, "model_dump") else data
        # FG5.2: 검색 결과에 인젝션 탐지 수행 (search_documents, fetch_node)
        injection = _run_injection_detection(raw) if tool_name in ("search_documents", "fetch_node") else None
        # 검색 결과에 untrusted 어노테이션 추가
        if isinstance(raw, dict) and "results" in raw:
            raw["results"] = content_directive_separator.annotate_results(raw["results"])
        return _ok(raw, actor, start, injection)
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
    # FG 0-5 (2026-04-23): 벡터화 상태 조회 Tool — 읽기 전용
    if tool_name == "mimir.vectorization.status":
        from app.schemas.mcp import VectorizationStatusToolRequest
        req = VectorizationStatusToolRequest(**body)
        return tool_vectorization_status(req, actor, conn)
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

    # FG5.3 REC-4.1: 에이전트별 Rate Limit 검사
    if actor.agent_id and not _check_agent_rate_limit(actor.agent_id, "stream"):
        async def _rate_limited():
            yield _sse_chunk({"success": False, "error": {"code": "RATE_LIMIT", "message": f"에이전트 {actor.agent_id}의 스트리밍 한도를 초과했습니다."}})
        return StreamingResponse(_rate_limited(), media_type="text/event-stream")

    import json

    async def generate():
        try:
            with get_db() as conn:
                data = _dispatch_tool(tool_name, body.params, actor, conn)
            result = data.model_dump() if hasattr(data, "model_dump") else data
            injection = _run_injection_detection(result) if tool_name in ("search_documents", "fetch_node") else None
            items = result.get("results", [result])
            for item in items:
                annotated = content_directive_separator.annotate_result(item) if isinstance(item, dict) else item
                yield _sse_chunk({"success": True, "data": annotated})
            meta = _make_metadata(actor, start, injection).model_dump()
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
