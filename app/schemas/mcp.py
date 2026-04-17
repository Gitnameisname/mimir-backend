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
]

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
