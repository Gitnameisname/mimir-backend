"""
MCP 2025-11-25 요청/응답 스키마 — Phase 4 FG4.1 + FG4.3.

표준 envelope:
  {
    "success": bool,
    "data": {...},
    "error": {"code": "...", "message": "..."},
    "metadata": {...}
  }

S3 Phase 4 FG 4-0 (2026-04-28): manifest 표준 부착 (`risk_tier` / `maturity` /
`status` / `exposure_policy`). 등급화 매핑은 `docs/개발문서/S3/phase4/산출물/도구등급화_매핑.md` 정본.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Tool manifest Literal 타입 (S3 Phase 4 FG 4-0)
# ---------------------------------------------------------------------------

RiskTier = Literal["L0", "L1", "L2", "L3", "L4"]
"""헌법 제20조 Typed Action Risk 분류.

L0 = 순수 read, L1 = 결정성 검사 (read + hash 등), L2 = 가역 쓰기,
L3 = 워크플로 전이 (사람 승인 필요), L4 = 비가역·파괴적 (MCP 영구 금지 — R1).
"""

Maturity = Literal["stable", "beta", "experimental", "disabled", "forbidden"]
"""도구 운영 단계.

forbidden = MCP 노출 절대 금지 (R1).
"""

ToolStatus = Literal["enabled", "disabled", "not_exposed"]
"""런타임 노출 상태. not_exposed → tools/list 미응답 + tools/call 거부."""

ExposurePolicy = Literal["MCP_ENABLED", "MCP_DISABLED", "REST_ADMIN_ONLY"]
"""인터페이스 정책. REST_ADMIN_ONLY 는 사람 관리자 UI 만 — MCP 영구 차단."""


# ---------------------------------------------------------------------------
# Capability manifest 확장 (S3 Phase 4 FG 4-5)
# ---------------------------------------------------------------------------

PolicyProfile = Literal[
    "read_safe",       # L0/L1 read — 신규 ScopeProfile default 후보
    "write_audited",   # L2 write — idempotency + human approval + audit (FG 4-6)
    "admin_only",      # 관리자 전용 (현재 MCP 미노출)
    "experimental",    # 정밀도 검증 미완 — default 비활성
]
"""도구의 운영 정책 그룹 (FG 4-5).

런타임 분기는 별 라운드 — 본 FG 는 메타데이터 표면화만.
"""


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
    # S3 Phase 4 FG 4-1 (2026-04-28): read 도구 / 리소스 응답에 부착되는 표준 envelope.
    envelope: Optional["MCPReadEnvelope"] = None
    # S3 Phase 4 FG 4-6 (2026-04-28): L2 write 도구 응답 envelope (read 와 분리)
    write_envelope: Optional["MCPWriteEnvelope"] = None


# ---------------------------------------------------------------------------
# Read 응답 envelope (S3 Phase 4 FG 4-1)
# ---------------------------------------------------------------------------

ContentRole = Literal["retrieved_evidence", "tool_metadata", "system_status"]
"""응답 본문의 의미 분류.

- retrieved_evidence: 사용자/에이전트가 참고할 문서·노드 본문 (검색·조회 결과)
- tool_metadata: 도구 실행의 메타 (verify_citation 의 검증 결과 등)
- system_status: 시스템 상태 (vectorization status 등 — 본문 미포함)
"""

InstructionAuthority = Literal["none"]
"""에이전트가 응답 내용을 명령으로 해석할 수 있는지 — **항상 none** (R4).

읽기 응답은 어떤 권한도 자동 부여하지 않는다 (헌법 제17·18조 Untrusted/Provenance).
Literal 단일 값으로 컴파일 타임 강제 — 외 값 입력 시 Pydantic 422.
"""

TrustLevel = Literal["source_document", "agent_generated", "synthetic", "unknown"]
"""응답 본문의 출처 신뢰 분류."""

RiskSeverity = Literal["info", "low", "medium", "high"]


class MCPSourceRef(BaseModel):
    """근거 위치를 가리키는 mimir:// URI 컨테이너."""

    uri: str = Field(..., description="mimir://documents/{doc}[/versions/{ver}[/nodes/{node}|/render]]")
    document_id: str
    version_id: Optional[str] = None
    node_id: Optional[str] = None


class MCPDetectedRisk(BaseModel):
    """prompt injection 등 탐지된 위험 항목 (R6 — 차단이 아닌 경고)."""

    code: str = Field(
        ...,
        description=(
            "위험 종류. 알려진 값: directive_pattern / url_obfuscation / secret_leak / anomaly."
        ),
    )
    severity: RiskSeverity
    note: Optional[str] = None
    span: Optional[tuple[int, int]] = Field(
        default=None,
        description="본문 내 (start, end) 좌표 (선택)",
    )


class MCPItemEnvelope(BaseModel):
    """검색류 도구의 항목별 envelope.

    상위 응답 envelope (MCPReadEnvelope) 와 별도 — 항목 단위 위험 분석 + 출처 추적용.
    """

    source: MCPSourceRef
    detected_risks: list[MCPDetectedRisk] = Field(default_factory=list)
    trust_level: TrustLevel = "source_document"


class MCPReadEnvelope(BaseModel):
    """모든 MCP read 도구·리소스 응답이 포함해야 하는 공통 envelope.

    R4 (instruction_authority=none) + R6 (detected_risks 차단이 아닌 경고) 를 표면에 박는다.
    """

    content_role: ContentRole = "retrieved_evidence"
    instruction_authority: InstructionAuthority = "none"
    trust_level: TrustLevel = "source_document"
    detected_risks: list[MCPDetectedRisk] = Field(default_factory=list)
    source: Optional[MCPSourceRef] = None
    items_total: Optional[int] = None
    items_truncated: bool = False


# ---------------------------------------------------------------------------
# search_nodes 도구 (S3 Phase 4 FG 4-2)
# ---------------------------------------------------------------------------


class SearchNodesRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    scope: Optional[str] = Field(default="default")
    document_ids: Optional[list[str]] = Field(
        default=None,
        description="검색 범위 제한 (UUID 리스트). 미지정 시 ACL 통과 모든 문서.",
    )
    node_kinds: Optional[list[str]] = Field(
        default=None,
        description="필터 (예: ['paragraph','heading','list_item']). 미지정 시 모든 종류.",
    )
    top_k: int = Field(default=20, ge=1, le=100)
    access_context: Optional[AccessContext] = None


class SearchNodeItem(BaseModel):
    document_id: str
    version_id: Optional[str] = None
    node_id: str
    node_kind: str
    snippet: str
    score: float = 0.0
    content_hash: Optional[str] = None


class SearchNodesData(BaseModel):
    items: list[SearchNodeItem] = Field(default_factory=list)
    total_matched: int = 0
    truncated_at: Optional[int] = None


# ---------------------------------------------------------------------------
# read_document_render 도구 (S3 Phase 4 FG 4-2)
# ---------------------------------------------------------------------------


class ReadDocumentRenderRequest(BaseModel):
    document_id: str
    version_id: Optional[str] = Field(
        default=None,
        description="vN / UUID / 'latest'. 'latest' 입력 시 서버가 즉시 vN 으로 resolve (R3).",
    )
    format: Literal["plain_text", "markdown"] = "plain_text"
    include_node_anchors: bool = Field(
        default=True,
        description="결과에 node_id ↔ offset 매핑 (node_anchors) 포함 여부.",
    )
    access_context: Optional[AccessContext] = None


class NodeAnchor(BaseModel):
    node_id: str
    offset_start: int
    offset_end: int


class ReadDocumentRenderData(BaseModel):
    document_id: str
    version_id: str  # resolved (R3 — 'latest' 단독 절대 미반환)
    format: Literal["plain_text", "markdown"]
    rendered_text: str
    render_hash: str  # SHA-256 hex (rendered_text 전체)
    node_anchors: list[NodeAnchor] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# resolve_document_reference 도구 (S3 Phase 4 FG 4-2)
# ---------------------------------------------------------------------------


MatchKind = Literal[
    "exact_title",
    "alias",
    "recent_context",
    "semantic",
    "fts_fallback",
]


class ResolveDocumentReferenceContext(BaseModel):
    recent_document_ids: Optional[list[str]] = Field(default_factory=list)
    conversation_id: Optional[str] = None


class ResolveDocumentReferenceRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=500)
    scope: Optional[str] = Field(default="default")
    preferred_doc_types: Optional[list[str]] = None
    context: Optional[ResolveDocumentReferenceContext] = Field(
        default_factory=ResolveDocumentReferenceContext
    )
    max_candidates: int = Field(default=5, ge=1, le=10)
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    access_context: Optional[AccessContext] = None


class ResolveCandidate(BaseModel):
    document_id: str
    version_ref: str = Field(
        ...,
        description=(
            "vN 형식 또는 'latest_published'. R3: 'latest' 단독 문자열 절대 미반환."
        ),
    )
    title: str
    confidence: float = Field(ge=0.0, le=1.0)
    match_kind: MatchKind


class ResolveDocumentReferenceData(BaseModel):
    resolved: bool
    needs_disambiguation: bool
    best_match: Optional[ResolveCandidate] = None
    candidates: list[ResolveCandidate] = Field(default_factory=list)


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
    version_id: str  # vN / UUID — 'latest' 거부 (R3, FG 4-3 §2.1.4)
    node_id: str
    content_hash: str  # citation_basis 에 따라 의미 다름
    # S3 Phase 4 FG 4-3 (2026-04-28): 5중 검증 입력
    citation_basis: Literal["node_content", "rendered_text"] = "node_content"
    quoted_text: Optional[str] = Field(
        default=None,
        description="검사 4 (텍스트 포함). 입력 시 노드/렌더 텍스트에 포함되는지 검증.",
    )
    span_offset: Optional[int] = None
    span_length: Optional[int] = Field(
        default=None,
        description="검사 5 (span 유효성). span_offset 과 함께 입력 시 [offset, offset+length] 가 텍스트 범위 내 검증.",
    )
    access_context: Optional[AccessContext] = None


class VerifyCitationChecks(BaseModel):
    """5중 검증 각 단계 결과 (S3 Phase 4 FG 4-3 §2.1.3)."""

    exists: bool
    pinned: bool
    hash_matches: bool
    quoted_text_in_content: Optional[bool] = None
    span_valid: Optional[bool] = None


class VerifyCitationData(BaseModel):
    """5중 검증 결과 + 디버깅 가시성을 위한 checks 딕셔너리."""

    verified: bool
    checks: VerifyCitationChecks
    current_hash: Optional[str] = None
    rendered_snapshot: Optional[str] = None
    node_snapshot: Optional[str] = None
    # 백워드 호환 필드 (FG 4-2 이전 응답 형태) — content_snapshot / hash_matches / version_valid
    content_snapshot: Optional[str] = None
    hash_matches: bool = False
    version_valid: bool = False
    message: str


# ---------------------------------------------------------------------------
# MCP Tool Schema 정의서 (FG4.3)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_documents",
        "description": "Search Mimir documents using full-text or vector search",
        "risk_tier": "L0",
        "maturity": "stable",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28): capability manifest 확장
        "default_enabled": True,
        "requires": [],
        "preferred_use": "FTS/벡터 문서 검색 (top_k≤50). 외부 Chatbot 의 일반 질의 응답.",
        "policy_profile": "read_safe",
        "streaming_supported": True,
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
        "risk_tier": "L0",
        "maturity": "stable",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28)
        "default_enabled": True,
        "requires": [],
        "preferred_use": "노드 단위 본문 조회 — search_documents 또는 search_nodes 후 깊이 있는 인용.",
        "policy_profile": "read_safe",
        "streaming_supported": False,
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
        "risk_tier": "L1",
        "maturity": "stable",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28)
        "default_enabled": True,
        "requires": [],
        "preferred_use": "5중 검증 (exists / pinned / hash / quoted_text / span). citation_basis 명시 권장.",
        "policy_profile": "read_safe",
        "streaming_supported": False,
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
        "risk_tier": "L0",
        "maturity": "beta",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28)
        "default_enabled": True,
        "requires": [],
        "preferred_use": "인라인 주석 + 멘션 조회. 사용자 협업 컨텍스트 추적용.",
        "policy_profile": "read_safe",
        "streaming_supported": False,
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
        # S3 Phase 4 FG 4-2 (2026-04-28): 노드 단위 검색 도구.
        "name": "search_nodes",
        "description": (
            "Search Mimir documents at the node grain (block / sentence-level). "
            "Use this when you need a tighter citation candidate than search_documents. "
            "Returns nodes ranked by relevance with content_hash for downstream verify_citation."
        ),
        "risk_tier": "L0",
        "maturity": "beta",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28)
        "default_enabled": True,
        "requires": [],
        "preferred_use": "노드 그래뉼래리티 검색 — citation 후보를 좁히고 verify_citation 으로 연결.",
        "policy_profile": "read_safe",
        "streaming_supported": False,
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:search",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "scope": {"type": "string"},
                "document_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
                "node_kinds": {"type": "array", "items": {"type": "string"}},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
                "access_context": {"type": "object"},
            },
            "required": ["query"],
        },
    },
    {
        # S3 Phase 4 FG 4-2 (2026-04-28): 렌더링 텍스트 조회 도구.
        "name": "read_document_render",
        "description": (
            "Read a document's rendered (human-readable) text. "
            "Returns plain_text or markdown plus optional node_anchors mapping node_id ↔ offsets, "
            "and a render_hash for downstream citation verification (FG 4-3 rendered_text basis)."
        ),
        "risk_tier": "L0",
        "maturity": "beta",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28)
        "default_enabled": True,
        "requires": [],
        "preferred_use": "rendered_text + node_anchors — citation_basis=rendered_text 인용 또는 사람용 본문 노출.",
        "policy_profile": "read_safe",
        "streaming_supported": False,
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:search",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "format": "uuid"},
                "version_id": {
                    "type": "string",
                    "description": "vN / UUID / 'latest'. 'latest' 는 서버가 vN 으로 자동 resolve (R3).",
                },
                "format": {"type": "string", "enum": ["plain_text", "markdown"]},
                "include_node_anchors": {"type": "boolean"},
                "access_context": {"type": "object"},
            },
            "required": ["document_id"],
        },
    },
    {
        # S3 Phase 4 FG 4-2 (2026-04-28): 자연어 참조 → document_id + version_ref 정규화.
        "name": "resolve_document_reference",
        "description": (
            "Resolve a natural-language document reference (e.g. 'the sales manual', 'v2024 security policy') "
            "to a concrete document_id + version_ref with confidence + needs_disambiguation. "
            "Stages: exact_title → alias → recent_context → semantic → fts_fallback (offline)."
        ),
        "risk_tier": "L1",
        "maturity": "experimental",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28): default_enabled=False — experimental 정밀도 검증 후 자동 등록 결정
        "default_enabled": False,
        "requires": [],
        "preferred_use": "자연어 참조 → document_id + version_ref. 임계 미달 시 needs_disambiguation 으로 후보 순회.",
        "policy_profile": "experimental",
        "streaming_supported": False,
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:search",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "minLength": 1, "maxLength": 500},
                "scope": {"type": "string"},
                "preferred_doc_types": {"type": "array", "items": {"type": "string"}},
                "context": {
                    "type": "object",
                    "properties": {
                        "recent_document_ids": {
                            "type": "array",
                            "items": {"type": "string", "format": "uuid"},
                        },
                        "conversation_id": {"type": "string", "format": "uuid"},
                    },
                },
                "max_candidates": {"type": "integer", "minimum": 1, "maximum": 10},
                "confidence_threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "access_context": {"type": "object"},
            },
            "required": ["reference"],
        },
    },
    {
        # S3 Phase 4 FG 4-6 (2026-04-28): MCP 표면 최초 L2 write 도구.
        # 4 사전 조건: idempotency / human approval / impact preview / 감사 로그 4종.
        # propose 만 — 자동 머지 없음. reviewer approval 후 별도 트리거 (REST/Admin UI).
        "name": "save_draft",
        "description": (
            "Propose a draft change to a document. "
            "Requires human reviewer approval before merge — agent cannot self-approve. "
            "Idempotent via idempotency_key. The agent's allowed_tools must include 'save_draft' "
            "(default_enabled=False — operator must register explicitly)."
        ),
        "risk_tier": "L2",
        "maturity": "experimental",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 capability manifest 확장
        "default_enabled": False,  # 명시 등록 필수 — write 자동 등록 금지
        "requires": ["fetch_node"],  # 일반 워크플로 — 기존 본문 조회 후 변경 제안
        "preferred_use": (
            "에이전트가 사람 reviewer 의 승인을 받아 draft 변경을 제안할 때. "
            "idempotency_key 로 재시도 안전 보장."
        ),
        "policy_profile": "write_audited",
        "streaming_supported": False,
        "authentication": {
            "method": "oauth2_client_credentials",
            "scope_profile_required": True,
            "delegation": "delegate:write",
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "기존 문서 UUID. None 이면 신규 문서 생성.",
                },
                "document_type_id": {
                    "type": "string",
                    "description": "신규 문서 시 type code 또는 UUID.",
                },
                "title": {"type": "string", "maxLength": 500},
                "content_snapshot": {
                    "type": "object",
                    "description": "ProseMirror doc 표준 포맷 (단일 정본).",
                },
                "metadata": {"type": "object"},
                "reason": {"type": "string", "maxLength": 1000},
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "재시도 안전성 — 같은 (agent, key) 반복 호출 시 같은 proposal.",
                },
                "scope": {"type": "string"},
                "access_context": {"type": "object"},
            },
            "required": ["content_snapshot", "idempotency_key"],
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
        "risk_tier": "L0",
        "maturity": "beta",
        "status": "enabled",
        "exposure_policy": "MCP_ENABLED",
        # FG 4-5 (2026-04-28): default_enabled=False — 운영 진단 용 (특수 목적 도구)
        "default_enabled": False,
        "requires": [],
        "preferred_use": "RAG 답변 부재 진단 — 색인 누락 / 임베딩 차원 mismatch 등을 에이전트가 자가 점검.",
        "policy_profile": "read_safe",
        "streaming_supported": False,
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

# ---------------------------------------------------------------------------
# Manifest 헬퍼 (S3 Phase 4 FG 4-0 §2.1.3 / §2.1.6)
# ---------------------------------------------------------------------------

def known_tool_names() -> frozenset[str]:
    """TOOL_SCHEMAS 에 등재된 모든 도구 이름.

    `ScopeProfile.allowed_tools` 검증 + manifest drift 감지의 단일 정본.
    """
    return frozenset(s["name"] for s in TOOL_SCHEMAS)


def is_tool_mcp_exposed(tool_schema: dict) -> bool:
    """도구가 MCP 표면에 노출 가능한지 검사 (R1 — L4 / forbidden / not_exposed 차단).

    - status == "not_exposed" → False
    - maturity == "forbidden" → False
    - risk_tier == "L4" → False (정의 단계 안전망)
    - 그 외 → True
    """
    if tool_schema.get("status") == "not_exposed":
        return False
    if tool_schema.get("maturity") == "forbidden":
        return False
    if tool_schema.get("risk_tier") == "L4":
        return False
    return True


def mcp_exposed_tool_schemas() -> list[dict]:
    """`is_tool_mcp_exposed` 통과 도구만 반환 (tools/list 응답 + dispatcher 사용).

    원본 TOOL_SCHEMAS 는 운영 정책 기록을 위해 모든 항목을 포함하므로,
    런타임 노출은 본 헬퍼로 필터.
    """
    return [s for s in TOOL_SCHEMAS if is_tool_mcp_exposed(s)]


def mcp_exposed_public_view(tool_schema: dict) -> dict:
    """tools/list 외부 응답용 보수 view (도구등급화_매핑.md §4 옵션 A + FG 4-5).

    외부에는 운영 정보 (`risk_tier`, `status`, `exposure_policy`, `default_enabled`,
    `policy_profile`) 비노출. `maturity` + FG 4-5 의 외부 후보 (`requires`,
    `preferred_use`, `streaming_supported`) 만 노출.
    """
    public_keys = {
        "name", "description", "maturity",
        "authentication", "inputSchema",
        # FG 4-5: 외부 노출 필드
        "requires", "preferred_use", "streaming_supported",
    }
    return {k: v for k, v in tool_schema.items() if k in public_keys}


def mcp_admin_full_view(tool_schema: dict) -> dict:
    """admin /admin/mcp/manifest 전용 — 전체 manifest 노출 (FG 4-5 §2.1.4).

    운영자가 모든 정책 필드 (default_enabled / policy_profile 포함) 를 보고
    Admin UI 에서 토글 결정.
    """
    return dict(tool_schema)


# ---------------------------------------------------------------------------
# FG 4-5 헬퍼 (정책 그룹화 / default 결정)
# ---------------------------------------------------------------------------


def default_enabled_tool_names() -> list[str]:
    """``default_enabled=True`` 인 노출 도구 이름 (정렬).

    신규 ScopeProfile 의 ``allowed_tools`` 자동 등록 후보 (FG 4-5 §2.1.5).
    """
    return sorted(
        s["name"]
        for s in TOOL_SCHEMAS
        if is_tool_mcp_exposed(s) and s.get("default_enabled", False)
    )


def tools_by_policy_profile(profile: str) -> list[str]:
    """``policy_profile`` 별 노출 도구 이름 (정렬).

    Args:
        profile: ``read_safe`` / ``write_audited`` / ``admin_only`` / ``experimental``
    """
    return sorted(
        s["name"]
        for s in TOOL_SCHEMAS
        if is_tool_mcp_exposed(s) and s.get("policy_profile") == profile
    )


# ---------------------------------------------------------------------------
# S3 Phase 4 FG 4-6 — L2 write 도구 (save_draft) 스키마
# ---------------------------------------------------------------------------


class DraftImpactPreview(BaseModel):
    """save_draft 적용 시 발생할 영향 사전 계산 (FG 4-6 §2.1.3).

    실제 DB 변경 없이 diff 만 산출. snapshot_sync_service / diff_service 위임.
    """

    document_id: Optional[str] = None
    target_version_id: Optional[str] = None  # None = 신규 문서
    overwrites_existing_draft: bool = False
    nodes_added: int = 0
    nodes_modified: int = 0
    nodes_deleted: int = 0
    chars_added: int = 0
    chars_removed: int = 0
    summary: str = ""  # 자연어 요약


class SaveDraftRequest(BaseModel):
    document_id: Optional[str] = Field(
        default=None,
        description="기존 문서 UUID. None 이면 신규 문서 생성 (document_type_id 필요).",
    )
    document_type_id: Optional[str] = Field(
        default=None,
        description="신규 문서 생성 시 type code 또는 UUID.",
    )
    title: Optional[str] = Field(default=None, max_length=500)
    content_snapshot: dict = Field(
        ...,
        description="ProseMirror doc 표준 포맷 (snapshot 단일 정본).",
    )
    metadata: dict = Field(default_factory=dict)
    reason: str = Field(
        default="",
        max_length=1000,
        description="에이전트가 본 draft 를 제안하는 의도 (감사 로그에 보존).",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "동일 (agent_id, idempotency_key) 재시도 시 같은 proposal 반환. "
            "FG 4-6 의 첫째 사전 조건 — 네트워크 재시도 안전성 보장. **필수**."
        ),
    )
    scope: Optional[str] = Field(default="default")
    access_context: Optional[AccessContext] = None


class SaveDraftData(BaseModel):
    proposal_id: str
    status: str  # 'pending' (proposed) — reviewer approval 대기
    document_id: str
    version_id: str
    impact: DraftImpactPreview
    requires_human_approval: bool = True
    audit_event: str = "agent_proposal.requested"
    message: str


class MCPWriteEnvelope(BaseModel):
    """L2 write 도구 응답 envelope (FG 4-6 §2.1.6).

    read envelope (MCPReadEnvelope) 와 분리 — content_role 이 다른 그룹 (mutation).
    R4 (instruction_authority=none) 그대로 유지.
    """

    content_role: Literal["mutation_proposed"] = "mutation_proposed"
    instruction_authority: Literal["none"] = "none"  # R4
    impact: Optional[DraftImpactPreview] = None
    proposal_id: Optional[str] = None
    requires_human_approval: bool = True
    audit_chain: list[str] = Field(default_factory=list)  # ['agent_proposal.requested', ...]


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
