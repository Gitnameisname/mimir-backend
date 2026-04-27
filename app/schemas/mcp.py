"""
MCP 2025-11-25 요청/응답 스키마 — Phase 4 FG4.1 + FG4.3.

표준 envelope:
  {
    "success": bool,
    "data": {...},
    "error": {"code": "...", "message": "..."},
    "metadata": {...}
  }
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 표준 응답 envelope (FG4.3)
# ---------------------------------------------------------------------------

class MCPErrorDetail(BaseModel):
    code: str
    message: str


class MCPMetadata(BaseModel):
    request_id: str
    timestamp: str
    agent_id: Optional[str] = None
    execution_time_ms: int = 0
    trusted: bool = False
    # FG5.2: 프롬프트 인젝션 방어 메타플래그
    injection_risk: bool = False
    injection_patterns_detected: list[str] = Field(default_factory=list)
    source_data_untrusted: bool = True


class MCPResponse(BaseModel):
    """모든 MCP 도구 응답의 표준 envelope."""
    success: bool
    data: Optional[Any] = None
    error: Optional[MCPErrorDetail] = None
    metadata: Optional[MCPMetadata] = None


# ---------------------------------------------------------------------------
# MCP Initialize (FG4.1)
# ---------------------------------------------------------------------------

class MCPInitializeRequest(BaseModel):
    client_id: str
    protocol_version: str = "2025-11-25"
    client_metadata: dict = Field(default_factory=dict)


class MCPCapabilities(BaseModel):
    tools: list[str]
    resources: bool = True
    prompts: bool = True
    tasks: bool = False


class MCPInitializeResponse(BaseModel):
    server_id: str = "mimir-s2"
    version: str
    protocol_version: str = "2025-11-25"
    capabilities: MCPCapabilities


# ---------------------------------------------------------------------------
# AccessContext (에이전트 위임)
# ---------------------------------------------------------------------------

class AccessContext(BaseModel):
    user_id: Optional[str] = None
    organization_id: Optional[str] = None
    team_id: Optional[str] = None
    permissions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# search_documents 도구 (FG4.1)
# ---------------------------------------------------------------------------

class SearchDocumentsRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    scope: Optional[str] = Field(default="default")
    document_types: Optional[list[str]] = None
    top_k: int = Field(default=5, ge=1, le=50)
    conversation_id: Optional[str] = None
    access_context: Optional[AccessContext] = None


class CitationResult(BaseModel):
    document_id: str
    version_id: Optional[str] = None
    node_id: Optional[str] = None
    span_offset: Optional[int] = None
    content_hash: Optional[str] = None


class SearchResultItem(BaseModel):
    document_id: str
    document_title: str
    version_id: Optional[str] = None
    node_id: Optional[str] = None
    content: str
    citation: Optional[CitationResult] = None
    relevance_score: float = 0.0
    retrieval_time_ms: int = 0


class SearchDocumentsData(BaseModel):
    results: list[SearchResultItem]
    total_count: int
    retrieval_method: str = "fts"


# ---------------------------------------------------------------------------
# fetch_node 도구 (FG4.1)
# ---------------------------------------------------------------------------

class FetchNodeRequest(BaseModel):
    document_id: str
    version_id: Optional[str] = None
    node_id: str
    access_context: Optional[AccessContext] = None


class NodeRelationships(BaseModel):
    parent_node_id: Optional[str] = None
    children: list[str] = Field(default_factory=list)


class FetchNodeData(BaseModel):
    document_id: str
    document_title: str
    version_id: Optional[str] = None
    node_id: str
    content: str
    metadata: dict = Field(default_factory=dict)
    relationships: NodeRelationships = Field(default_factory=NodeRelationships)


# ---------------------------------------------------------------------------
# verify_citation 도구 (FG4.1)
# ---------------------------------------------------------------------------

class VerifyCitationRequest(BaseModel):
    document_id: str
    version_id: str
    node_id: str
    content_hash: str
    span_offset: Optional[int] = None
    access_context: Optional[AccessContext] = None


class VerifyCitationData(BaseModel):
    verified: bool
    current_hash: Optional[str] = None
    hash_matches: bool
    content_snapshot: Optional[str] = None
    version_valid: bool
    message: str


# ---------------------------------------------------------------------------
# MCP Tool Schema 정의서 (FG4.3)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_documents",
        "description": "Search Mimir documents using full-text or vector search",
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:search",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 쿼리", "minLength": 1, "maxLength": 500},
                "scope": {"type": "string", "description": "접근 범위 (Scope Profile에 정의된 scope_name)"},
                "document_types": {"type": "array", "items": {"type": "string"}, "description": "문서 유형 필터"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "description": "최대 결과 수"},
                "conversation_id": {"type": "string", "format": "uuid", "description": "대화 컨텍스트 ID"},
                "access_context": {
                    "type": "object",
                    "description": "위임 컨텍스트 (사용자 대행 시)",
                    "properties": {
                        "user_id": {"type": "string"},
                        "organization_id": {"type": "string"},
                        "team_id": {"type": "string"},
                        "permissions": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_node",
        "description": "Fetch the full content of a specific document node",
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:search",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "format": "uuid"},
                "version_id": {"type": "string", "format": "uuid", "description": "버전 ID (생략 시 최신)"},
                "node_id": {"type": "string", "description": "노드 ID"},
                "access_context": {"type": "object"},
            },
            "required": ["document_id", "node_id"],
        },
    },
    {
        "name": "verify_citation",
        "description": "Verify a Citation 5-tuple using content_hash comparison",
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "format": "uuid"},
                "version_id": {"type": "string", "format": "uuid"},
                "node_id": {"type": "string"},
                "content_hash": {"type": "string", "description": "SHA-256 해시 (hex)"},
                "span_offset": {"type": "integer", "description": "스팬 오프셋 (선택)"},
                "access_context": {"type": "object"},
            },
            "required": ["document_id", "version_id", "node_id", "content_hash"],
        },
    },
    {
        # S3 Phase 3 FG 3-3 (2026-04-27): 에이전트가 문서의 인라인 주석을 읽을 수 있도록
        # read_annotations Tool 표면 추가. 읽기 전용 (쓰기는 본 FG 에서 노출 안 함).
        "name": "read_annotations",
        "description": (
            "Read inline annotations attached to a document. "
            "Read-only — agents cannot create / modify / delete annotations via this tool. "
            "ACL is enforced via the agent's Scope Profile (404 if document is out of scope)."
        ),
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:read",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "주석을 조회할 문서 ID",
                },
                "include_resolved": {
                    "type": "boolean",
                    "description": "resolved 주석 포함 여부 (기본 true)",
                    "default": True,
                },
                "include_orphans": {
                    "type": "boolean",
                    "description": "orphan 주석 포함 여부 (기본 true)",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
            "required": ["document_id"],
        },
    },
    {
        # FG 0-5 (2026-04-23): 에이전트가 RAG 답변 부재 원인을 스스로 진단할 수 있게
        # 문서 벡터화 상태를 읽기 전용으로 노출. 재벡터화 실행은 Tool 에 노출하지 않는다
        # (운영 안전 — 사람 명시적 클릭만).
        "name": "mimir.vectorization.status",
        "description": (
            "문서의 벡터화 상태를 조회한다. "
            "에이전트가 RAG 답변을 찾지 못할 때 '해당 문서가 색인됐는지' 를 진단하는 용도. "
            "읽기 전용. 재벡터화는 사람이 UI 에서 명시적으로 수행."
        ),
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:search",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "벡터화 상태를 조회할 문서 ID",
                },
            },
            "required": ["document_id"],
        },
    },
]


# S3 Phase 3 FG 3-3: read_annotations Tool 의 Request / Response 스키마
class ReadAnnotationsRequest(BaseModel):
    document_id: str = Field(..., description="문서 UUID")
    include_resolved: bool = Field(default=True)
    include_orphans: bool = Field(default=True)
    limit: int = Field(default=200, ge=1, le=500)


class ReadAnnotationItem(BaseModel):
    id: str
    document_id: str
    node_id: str
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    author_id: str
    actor_type: str
    content: str
    status: str
    parent_id: Optional[str] = None
    is_orphan: bool
    created_at: str
    updated_at: str
    mentioned_user_ids: list[str] = Field(default_factory=list)


class ReadAnnotationsResponse(BaseModel):
    document_id: str
    annotations: list[ReadAnnotationItem] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="응답이 limit 으로 잘렸는지 (limit 와 동일 길이면 추가 데이터 가능성)",
    )


# FG 0-5: mimir.vectorization.status Tool 의 Request / Response 스키마
class VectorizationStatusToolRequest(BaseModel):
    document_id: str = Field(..., description="문서 UUID")


class VectorizationStatusToolResponse(BaseModel):
    document_id: str
    status: str
    latest_published_version_id: Optional[str] = None
    indexed_version_id: Optional[str] = None
    chunk_count: int = 0
    last_vectorized_at: Optional[str] = None
    last_error: Optional[str] = None
    # 에이전트 경로에서는 can_reindex 는 항상 False (재벡터화 Tool 미노출)
    can_reindex: bool = False

# Mimir Extension 선언 (FG4.3)
MIMIR_EXTENSIONS: list[dict] = [
    {
        "name": "mimir.citation-5tuple",
        "version": "1.0",
        "description": (
            "Citation verification using 5-tuple "
            "(document_id, version_id, node_id, span_offset, content_hash)"
        ),
        "capabilities": {
            "verify_citation": True,
            "cite_format": "5-tuple",
        },
    },
    {
        "name": "mimir.span-backref",
        "version": "1.0",
        "description": "Span-level back-reference to original content",
        "capabilities": {
            "span_verification": True,
            "position_tracking": True,
        },
    },
]
