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
from datetime import datetime
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
    DraftImpactPreview,
    FetchNodeRequest,
    MCPCapabilities,
    MCPDetectedRisk,
    MCPErrorDetail,
    MCPInitializeRequest,
    MCPInitializeResponse,
    MCPItemEnvelope,
    MCPMetadata,
    MCPReadEnvelope,
    MCPResponse,
    MCPSourceRef,
    MCPWriteEnvelope,
    SearchDocumentsRequest,
    VerifyCitationRequest,
    is_tool_mcp_exposed,
    mcp_exposed_public_view,
    mcp_exposed_tool_schemas,
)
from app.mcp.risk_mapper import map_injection_patterns, map_injection_result
from app.mcp.uri_builder import build_doc_uri, build_node_uri, build_version_uri
from app.config import settings
from app.security.prompt_injection import (
    InjectionDetectionResult,
    content_directive_separator,
    prompt_injection_detector,
)
from app.utils.time import utcnow_iso
from app.utils.json_utils import dumps_ko

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
# S3 Phase 4 FG 4-0 (2026-04-28): manifest 의 status/maturity/risk_tier 로 노출 도구 필터.
# `not_exposed` / `forbidden` / `L4` 도구는 _CURATED_TOOLS 에서 제외 (R1 — L4 MCP 영구 금지).
_CURATED_TOOLS = {s["name"] for s in mcp_exposed_tool_schemas()}


def _make_metadata(
    actor: ActorContext,
    start: float,
    injection: Optional[InjectionDetectionResult] = None,
) -> MCPMetadata:
    return MCPMetadata(
        request_id=str(uuid.uuid4()),
        timestamp=utcnow_iso(),
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
    envelope: Optional[MCPReadEnvelope] = None,
    write_envelope: Optional[MCPWriteEnvelope] = None,
) -> MCPResponse:
    return MCPResponse(
        success=True,
        data=data,
        metadata=_make_metadata(actor, start, injection),
        envelope=envelope,
        write_envelope=write_envelope,
    )


# ---------------------------------------------------------------------------
# S3 Phase 4 FG 4-6 (2026-04-28) — write envelope 빌더
# ---------------------------------------------------------------------------


def _build_write_envelope(tool_name: str, raw: Any) -> MCPWriteEnvelope:
    """L2 write 도구 응답 envelope 빌더 (FG 4-6 §2.1.6).

    R4 (instruction_authority=none) 자동 보장. R6 (detected_risks 차단 아닌 경고)
    는 read 와 별개 — write 는 풍부한 audit metadata 가 주된 보호.
    """
    raw_dict = raw if isinstance(raw, dict) else (
        raw.model_dump() if hasattr(raw, "model_dump") else {}
    )
    if tool_name == "save_draft":
        impact_data = raw_dict.get("impact")
        impact = DraftImpactPreview(**impact_data) if isinstance(impact_data, dict) else None
        return MCPWriteEnvelope(
            content_role="mutation_proposed",
            instruction_authority="none",
            impact=impact,
            proposal_id=raw_dict.get("proposal_id"),
            requires_human_approval=bool(raw_dict.get("requires_human_approval", True)),
            audit_chain=[raw_dict.get("audit_event") or "agent_proposal.requested"],
        )
    # 기본 — write 도구 추가 시 분기 추가
    return MCPWriteEnvelope(
        content_role="mutation_proposed",
        instruction_authority="none",
        requires_human_approval=True,
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
        # read_annotations (FG 4-1): annotations[*].content
        if "annotations" in data and isinstance(data["annotations"], list):
            for ann in data["annotations"]:
                if isinstance(ann, dict):
                    texts.append(str(ann.get("content") or ""))
        # search_nodes (FG 4-2): items[*].snippet
        if "items" in data and isinstance(data["items"], list):
            for it in data["items"]:
                if isinstance(it, dict):
                    texts.append(str(it.get("snippet") or ""))
        # read_document_render (FG 4-2): rendered_text
        if "rendered_text" in data:
            texts.append(str(data.get("rendered_text") or ""))
    elif hasattr(data, "model_dump"):
        return _run_injection_detection(data.model_dump())

    if not texts:
        from app.security.prompt_injection import InjectionDetectionResult
        return InjectionDetectionResult(injection_risk=False, injection_patterns_detected=[])

    results = prompt_injection_detector.detect_batch(texts)
    return prompt_injection_detector.merge_results(results)


# ---------------------------------------------------------------------------
# S3 Phase 4 FG 4-1 §2.1.4 — 응답 envelope 빌더
# ---------------------------------------------------------------------------


def _source_from_doc(document_id: Optional[str]) -> Optional[MCPSourceRef]:
    if not document_id:
        return None
    return MCPSourceRef(uri=build_doc_uri(document_id), document_id=document_id)


def _source_from_version(
    document_id: Optional[str], version_id: Optional[str]
) -> Optional[MCPSourceRef]:
    if not document_id or not version_id:
        return _source_from_doc(document_id)
    return MCPSourceRef(
        uri=build_version_uri(document_id, version_id),
        document_id=document_id,
        version_id=version_id,
    )


def _source_from_node(
    document_id: Optional[str],
    version_id: Optional[str],
    node_id: Optional[str],
) -> Optional[MCPSourceRef]:
    if not (document_id and version_id and node_id):
        return _source_from_version(document_id, version_id)
    return MCPSourceRef(
        uri=build_node_uri(document_id, version_id, node_id),
        document_id=document_id,
        version_id=version_id,
        node_id=node_id,
    )


def _enrich_search_node_items_with_envelope(items: list[dict]) -> None:
    """FG 4-2: search_nodes 결과 항목에 ``_envelope`` 부착.

    각 항목별 prompt_injection_detector 실행 → per-item risks.
    source = mimir://documents/{id}/versions/{vN}/nodes/{node_id}.
    """
    if not items:
        return
    texts = [str(it.get("snippet") or "") for it in items]
    detection_results = prompt_injection_detector.detect_batch(texts)
    for item, result in zip(items, detection_results):
        if not isinstance(item, dict):
            continue
        risks = map_injection_result(result)
        doc_id = item.get("document_id")
        ver_id = item.get("version_id")
        node_id = item.get("node_id")
        source = _source_from_node(doc_id, ver_id, node_id) or _source_from_doc(doc_id)
        if source is None:
            continue
        item["_envelope"] = MCPItemEnvelope(source=source, detected_risks=risks).model_dump()


def _enrich_resolve_candidates_with_envelope(raw: dict) -> None:
    """FG 4-2: resolve_document_reference 의 best_match + candidates 에 envelope 부착.

    detected_risks 는 비움 (resolve 응답은 tool_metadata — 본문 텍스트 없음).
    source = mimir://documents/{id} (version_ref 가 'latest_published' 일 수 있어 doc URI).
    """
    def _enrich_one(c: dict) -> None:
        if not isinstance(c, dict):
            return
        doc_id = c.get("document_id")
        if not doc_id:
            return
        c["_envelope"] = MCPItemEnvelope(
            source=_source_from_doc(doc_id), detected_risks=[]
        ).model_dump()

    bm = raw.get("best_match")
    if bm:
        _enrich_one(bm)
    for c in raw.get("candidates", []) or []:
        _enrich_one(c)


def _enrich_search_items_with_envelope(items: list[dict]) -> None:
    """search_documents 결과 항목에 ``_envelope`` (MCPItemEnvelope dump) 부착.

    각 항목별로 별도 prompt_injection_detector 실행 → per-item risks 포함.
    """
    if not items:
        return
    texts = [str(it.get("content") or it.get("snippet") or "") for it in items]
    detection_results = prompt_injection_detector.detect_batch(texts)
    for item, result in zip(items, detection_results):
        if not isinstance(item, dict):
            continue
        risks = map_injection_result(result)
        # citation 또는 직접 필드에서 source 추출
        citation = item.get("citation") or {}
        doc_id = item.get("document_id") or citation.get("document_id")
        ver_id = item.get("version_id") or citation.get("version_id")
        node_id = item.get("node_id") or citation.get("node_id")
        source = _source_from_node(doc_id, ver_id, node_id) or _source_from_doc(doc_id)
        if source is None:
            # source 가 빌드 불가하면 envelope 자체 미부착 (필수 필드 부재)
            continue
        item_env = MCPItemEnvelope(source=source, detected_risks=risks)
        item["_envelope"] = item_env.model_dump()


def _build_envelope(
    tool_name: str,
    raw: Any,
    injection: Optional[InjectionDetectionResult] = None,
) -> MCPReadEnvelope:
    """도구 실행 결과 + 인젝션 탐지 → MCPReadEnvelope (FG 4-1 §2.1.4 매핑 표).

    R4 (instruction_authority=none) 는 MCPReadEnvelope 의 Literal 단일값으로 자동 보장.
    """
    risks_top = map_injection_result(injection) if injection else []
    raw_dict = raw if isinstance(raw, dict) else (
        raw.model_dump() if hasattr(raw, "model_dump") else {}
    )

    if tool_name == "verify_citation":
        # tool_metadata — 검증 결과는 메타. 본문 일부 (content_snapshot) 가 있으나
        # 응답 의도 자체가 검증 보고서이므로 risk 적용 안 함 (§2.1.4).
        return MCPReadEnvelope(
            content_role="tool_metadata",
            trust_level="source_document",
            source=_source_from_node(
                raw_dict.get("document_id"),
                raw_dict.get("version_id"),
                raw_dict.get("node_id"),
            ),
            detected_risks=[],
        )
    if tool_name == "mimir.vectorization.status":
        return MCPReadEnvelope(
            content_role="system_status",
            trust_level="unknown",
            source=_source_from_doc(raw_dict.get("document_id")),
            detected_risks=[],
        )
    if tool_name == "fetch_node":
        return MCPReadEnvelope(
            content_role="retrieved_evidence",
            trust_level="source_document",
            source=_source_from_node(
                raw_dict.get("document_id"),
                raw_dict.get("version_id"),
                raw_dict.get("node_id"),
            ),
            detected_risks=risks_top,
        )
    if tool_name == "read_annotations":
        anns = raw_dict.get("annotations", []) or []
        return MCPReadEnvelope(
            content_role="retrieved_evidence",
            trust_level="source_document",
            source=_source_from_doc(raw_dict.get("document_id")),
            detected_risks=risks_top,
            items_total=len(anns),
            items_truncated=bool(raw_dict.get("truncated", False)),
        )
    if tool_name == "search_documents":
        items = raw_dict.get("results", []) if isinstance(raw_dict, dict) else []
        return MCPReadEnvelope(
            content_role="retrieved_evidence",
            trust_level="source_document",
            source=None,  # 검색은 항목별 envelope 의 source 사용
            detected_risks=risks_top,
            items_total=len(items),
            items_truncated=False,
        )
    # FG 4-2 신규 도구 3종
    if tool_name == "search_nodes":
        items = raw_dict.get("items", []) if isinstance(raw_dict, dict) else []
        return MCPReadEnvelope(
            content_role="retrieved_evidence",
            trust_level="source_document",
            source=None,  # 항목별 envelope 의 source 사용
            detected_risks=risks_top,
            items_total=raw_dict.get("total_matched", len(items)),
            items_truncated=raw_dict.get("truncated_at") is not None,
        )
    if tool_name == "read_document_render":
        # source = mimir://documents/{id}/versions/{vN}/render
        doc_id = raw_dict.get("document_id")
        ver_id = raw_dict.get("version_id")
        source = None
        if doc_id and ver_id:
            from app.mcp.uri_builder import build_render_uri
            try:
                source = MCPSourceRef(
                    uri=build_render_uri(doc_id, ver_id),
                    document_id=doc_id,
                    version_id=ver_id,
                )
            except ValueError:
                source = _source_from_version(doc_id, ver_id)
        return MCPReadEnvelope(
            content_role="retrieved_evidence",
            trust_level="source_document",
            source=source,
            detected_risks=risks_top,
        )
    if tool_name == "resolve_document_reference":
        # tool_metadata — 응답 자체가 메타 (best_match + candidates).
        # detected_risks 적용 안 함 — 입력 reference 만 본문 텍스트라 검색 결과 본문이 없음.
        return MCPReadEnvelope(
            content_role="tool_metadata",
            trust_level="source_document",
            source=None,
            detected_risks=[],
            items_total=len(raw_dict.get("candidates", [])),
        )
    # 알 수 없는 도구 — 안전한 기본값 (R4 기본값 = none)
    return MCPReadEnvelope(detected_risks=risks_top)


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
        # FG5.2 / FG 4-2: 본문 보유 read 도구 인젝션 탐지
        injection_tools = (
            "search_documents",
            "fetch_node",
            "read_annotations",
            "search_nodes",
            "read_document_render",
        )
        injection = _run_injection_detection(raw) if tool_name in injection_tools else None
        # 검색 결과에 untrusted 어노테이션 추가 (FG5.2)
        if isinstance(raw, dict) and "results" in raw:
            raw["results"] = content_directive_separator.annotate_results(raw["results"])
        # FG 4-1 §2.1.5: search_documents 항목별 envelope 부착 (per-item source + risks)
        if tool_name == "search_documents" and isinstance(raw, dict):
            _enrich_search_items_with_envelope(raw.get("results", []))
        # FG 4-2: search_nodes 항목별 envelope 부착 (per-item source + risks)
        if tool_name == "search_nodes" and isinstance(raw, dict):
            _enrich_search_node_items_with_envelope(raw.get("items", []))
        # FG 4-2: resolve_document_reference 후보별 envelope 부착
        if tool_name == "resolve_document_reference" and isinstance(raw, dict):
            _enrich_resolve_candidates_with_envelope(raw)
        # FG 4-6: write 도구는 read envelope 대신 write envelope 사용
        if tool_name == "save_draft":
            write_env = _build_write_envelope(tool_name, raw)
            return _ok(raw, actor, start, injection=None, envelope=None, write_envelope=write_env)
        # FG 4-1 §2.1.4: 도구별 응답 envelope (R4 — instruction_authority=none 자동)
        envelope = _build_envelope(tool_name, raw, injection)
        return _ok(raw, actor, start, injection, envelope=envelope)
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
    # S3 Phase 3 FG 3-3 (2026-04-27): 주석 조회 Tool — 읽기 전용
    if tool_name == "read_annotations":
        from app.mcp.tools import tool_read_annotations
        from app.schemas.mcp import ReadAnnotationsRequest
        req = ReadAnnotationsRequest(**body)
        return tool_read_annotations(req, actor, conn)
    # S3 Phase 4 FG 4-2 (2026-04-28): 신규 read 도구 3종
    if tool_name == "search_nodes":
        from app.mcp.tools import tool_search_nodes
        from app.schemas.mcp import SearchNodesRequest
        return tool_search_nodes(SearchNodesRequest(**body), actor, conn)
    if tool_name == "read_document_render":
        from app.mcp.tools import tool_read_document_render
        from app.schemas.mcp import ReadDocumentRenderRequest
        return tool_read_document_render(ReadDocumentRenderRequest(**body), actor, conn)
    if tool_name == "resolve_document_reference":
        from app.mcp.tools import tool_resolve_document_reference
        from app.schemas.mcp import ResolveDocumentReferenceRequest
        return tool_resolve_document_reference(
            ResolveDocumentReferenceRequest(**body), actor, conn
        )
    # S3 Phase 4 FG 4-6 (2026-04-28): L2 write 도구 — propose 만, 자동 머지 없음
    if tool_name == "save_draft":
        from app.mcp.tools import tool_save_draft
        from app.schemas.mcp import SaveDraftRequest
        return tool_save_draft(SaveDraftRequest(**body), actor, conn)
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
            injection = (
                _run_injection_detection(result)
                if tool_name in ("search_documents", "fetch_node", "read_annotations")
                else None
            )
            # FG 4-1 §2.1.5: search_documents 항목별 envelope 부착
            if tool_name == "search_documents" and isinstance(result, dict):
                _enrich_search_items_with_envelope(result.get("results", []))
            items = result.get("results", [result])
            for item in items:
                annotated = content_directive_separator.annotate_result(item) if isinstance(item, dict) else item
                yield _sse_chunk({"success": True, "data": annotated})
            # FG 4-1 §2.1.4: SSE 종료 메시지에 envelope 동봉
            envelope = _build_envelope(tool_name, result, injection).model_dump()
            meta = _make_metadata(actor, start, injection).model_dump()
            yield f"event: done\ndata: {json.dumps({'metadata': meta, 'envelope': envelope})}\n\n"
        except MCPError as exc:
            yield _sse_chunk({"success": False, "error": {"code": exc.code.value, "message": exc.message}})
        except Exception as exc:
            yield _sse_chunk({"success": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}})

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse_chunk(data: dict) -> str:
    import json
    return f"data: {dumps_ko(data)}\n\n"


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
        raw = data.model_dump()
        # FG 4-1 §2.1.5 / Step 5: 리소스(read) 표면도 envelope 적용 (fetch_node 와 동일)
        injection = _run_injection_detection(raw)
        envelope = _build_envelope("fetch_node", raw, injection)
        return _ok(raw, actor, start, injection, envelope=envelope)
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
    """tools/list — manifest 필터 + 보수 외부 view 적용 (FG 4-0 §3.3 / §4 옵션 A).

    외부 응답에는 `name` / `description` / `maturity` / `authentication` / `inputSchema` 만 노출.
    `risk_tier` / `status` / `exposure_policy` 는 운영자용 별 endpoint (FG 4-5 이월) 에서 조회.

    S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28): 인증된 에이전트의 경우 ScopeProfile.allowed_tools
    로 추가 필터 (per-actor view). 사람/시스템 actor 는 manifest 노출 도구 전체.
    """
    start = time.monotonic()
    exposed = mcp_exposed_tool_schemas()
    # Per-actor allowed_tools 필터 (에이전트 전용)
    if actor.is_authenticated and actor.is_agent:
        allowed = _agent_allowed_tools(actor)
        if allowed is not None:
            exposed = [s for s in exposed if s["name"] in allowed]
    public_tools = [mcp_exposed_public_view(s) for s in exposed]
    return _ok({"tools": public_tools, "extensions": MIMIR_EXTENSIONS}, actor, start)


def _agent_allowed_tools(actor: ActorContext) -> Optional[set[str]]:
    """에이전트 actor 의 ScopeProfile.allowed_tools 조회.

    반환:
      - None: scope_profile_id 없음 또는 조회 실패 → 호출자가 필터 미적용
      - set[str]: 허용 도구 이름 집합 (빈 집합도 정상 — default-deny 의미)
    """
    if not actor.scope_profile_id:
        return None
    try:
        from app.repositories.scope_profile_repository import ScopeProfileRepository
        with get_db() as conn:
            repo = ScopeProfileRepository(conn)
            profile = repo.get_by_id(actor.scope_profile_id)
        if profile is None:
            return None
        return set(profile.allowed_tools or [])
    except Exception as exc:
        logger.warning("agent allowed_tools lookup failed for %s: %s", actor.scope_profile_id, exc)
        return None
