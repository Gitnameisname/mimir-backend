"""
MCP 도구 3종 구현 — Phase 4 FG4.1.

  - search_documents: FTS + Vector 검색
  - fetch_node: 문서 노드 전문 조회
  - verify_citation: Citation 5-tuple 검증

S2 원칙:
  - 모든 도구는 Scope Profile 기반 ACL 필터를 적용 (S2 원칙 ⑤)
  - 에이전트 킬스위치 상태를 미리 확인 (S2 원칙 ⑥)
  - 감사 로그에 actor_type=agent 필수 기록
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from app.api.auth.models import ActorContext
from app.audit.emitter import audit_emitter
from app.mcp.errors import MCPError, MCPErrorCode, invalid_scope, not_found, unauthorized
from app.mcp.scope_filter import ScopeFilterResolutionError, apply_scope_filter
from app.schemas.mcp import (
    AccessContext,
    CitationResult,
    FetchNodeData,
    FetchNodeRequest,
    NodeRelationships,
    SearchDocumentsData,
    SearchDocumentsRequest,
    SearchResultItem,
    VectorizationStatusToolRequest,
    VectorizationStatusToolResponse,
    VerifyCitationData,
    VerifyCitationRequest,
)

logger = logging.getLogger(__name__)


def tool_search_documents(
    request: SearchDocumentsRequest,
    actor: ActorContext,
    conn,
) -> SearchDocumentsData:
    """search_documents 도구 — FTS/Hybrid 검색 + Scope ACL 적용."""
    start = time.monotonic()
    _check_agent_write_blocked(actor)

    from app.services.search_service import SearchService
    svc = SearchService()

    # 검색 실행 (기존 SearchService 인터페이스)
    try:
        raw = svc.search_documents(
            conn=conn,
            q=request.query,
            actor_role=actor.role,
            limit=request.top_k,
            doc_type=request.document_types[0] if request.document_types else None,
        )
    except Exception as exc:
        logger.error("mcp.search_documents failed: %s", exc)
        raise MCPError(MCPErrorCode.INTERNAL_ERROR, f"검색 오류: {exc}", 500)

    # Scope Profile ACL 필터: document_ids 기준 후처리 필터링
    acl_extra = _resolve_acl_filter(actor, request.scope, request.access_context, conn)
    allowed_doc_ids: Optional[set] = None
    if acl_extra.get("sql") and acl_extra.get("params") is not None:
        allowed_doc_ids = _fetch_allowed_doc_ids(conn, acl_extra)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    results = []
    raw_items = raw.results if hasattr(raw, "results") else raw
    for item in raw_items:
        citation_obj = getattr(item, "citation", None)
        doc_id = str(
            getattr(item, "document_id", None)
            or getattr(item, "id", None)
            or getattr(citation_obj, "document_id", "")
            or ""
        )
        # Scope ACL 후처리 필터
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        ver_id = str(
            getattr(item, "version_id", None)
            or getattr(citation_obj, "version_id", "")
            or ""
        ) or None
        node_id = str(
            getattr(item, "node_id", None)
            or getattr(citation_obj, "node_id", "")
            or ""
        ) or None
        snippets = getattr(item, "snippets", None) or []
        snippet_text = ""
        if snippets:
            snippet_text = " ".join(
                str(getattr(snippet, "text", "") or "")
                for snippet in snippets
                if getattr(snippet, "text", None)
            ).strip()
        content = str(
            getattr(item, "snippet", "")
            or getattr(item, "content", "")
            or snippet_text
            or getattr(item, "summary", "")
            or ""
        )
        score = float(getattr(item, "rank", 0) or getattr(item, "score", 0) or 0)
        citation = None
        if citation_obj is not None:
            citation = CitationResult(
                document_id=doc_id,
                version_id=ver_id,
                node_id=node_id,
                span_offset=getattr(citation_obj, "span_offset", None),
                content_hash=getattr(citation_obj, "content_hash", None),
            )
        elif node_id:
            citation = CitationResult(
                document_id=doc_id,
                version_id=ver_id,
                node_id=node_id,
                content_hash=_compute_content_hash(content),
            )
        results.append(SearchResultItem(
            document_id=doc_id,
            document_title=str(getattr(item, "document_title", "") or getattr(item, "title", "") or ""),
            version_id=ver_id,
            node_id=node_id,
            content=content,
            citation=citation,
            relevance_score=score,
            retrieval_time_ms=elapsed_ms,
        ))

    _emit_audit(
        event_type="mcp.search_documents",
        action="mcp.tool.call",
        actor=actor,
        metadata={"query": request.query[:100], "results": len(results), "scope": request.scope},
    )

    return SearchDocumentsData(
        results=results,
        total_count=len(results),
        retrieval_method="fts",
    )


def tool_fetch_node(
    request: FetchNodeRequest,
    actor: ActorContext,
    conn,
) -> FetchNodeData:
    """fetch_node 도구 — 특정 문서 노드 전문 조회."""
    _check_agent_write_blocked(actor)
    acl_extra = _resolve_acl_filter(actor, None, request.access_context, conn)
    _ensure_document_allowed(conn, request.document_id, acl_extra)
    chunk_row = _fetch_accessible_chunk(
        conn,
        actor,
        request.document_id,
        request.version_id,
        request.node_id,
        request.access_context,
    )
    if not chunk_row:
        raise not_found(f"노드 {request.node_id}를 찾을 수 없습니다.")

    with conn.cursor() as cur:
        # 버전 ID 결정
        if request.version_id:
            ver_id = request.version_id
        else:
            cur.execute(
                "SELECT id FROM versions WHERE document_id = %s ORDER BY version_number DESC LIMIT 1",
                (request.document_id,),
            )
            vrow = cur.fetchone()
            if not vrow:
                raise not_found(f"문서 {request.document_id}의 버전을 찾을 수 없습니다.")
            ver_id = str(vrow["id"])

        # 문서 기본 정보 조회
        cur.execute(
            "SELECT d.title FROM documents d WHERE d.id = %s",
            (request.document_id,),
        )
        doc_row = cur.fetchone()
        if not doc_row:
            raise not_found(f"문서 {request.document_id}를 찾을 수 없습니다.")

        # 노드 조회
        cur.execute(
            "SELECT id, version_id, parent_id, node_type, title, content, metadata, created_at"
            " FROM nodes WHERE version_id = %s AND id = %s",
            (ver_id, request.node_id),
        )
        node_row = cur.fetchone()
        if not node_row:
            raise not_found(f"노드 {request.node_id}를 찾을 수 없습니다.")

        # 자식 노드 조회
        cur.execute(
            "SELECT id FROM nodes WHERE parent_id = %s ORDER BY order_index",
            (request.node_id,),
        )
        children = [str(r["id"]) for r in cur.fetchall()]

    content = node_row.get("content") or chunk_row.get("source_text") or ""
    metadata = node_row.get("metadata") or {}
    if isinstance(metadata, str):
        import json
        metadata = json.loads(metadata)

    _emit_audit(
        event_type="mcp.fetch_node",
        action="mcp.tool.call",
        actor=actor,
        metadata={"document_id": request.document_id, "node_id": request.node_id},
    )

    return FetchNodeData(
        document_id=request.document_id,
        document_title=doc_row["title"],
        version_id=ver_id,
        node_id=request.node_id,
        content=content,
        metadata=metadata,
        relationships=NodeRelationships(
            parent_node_id=str(node_row["parent_id"]) if node_row.get("parent_id") else None,
            children=children,
        ),
    )


def tool_verify_citation(
    request: VerifyCitationRequest,
    actor: ActorContext,
    conn,
) -> VerifyCitationData:
    """verify_citation 도구 — Citation 5-tuple content_hash 검증."""
    _check_agent_write_blocked(actor)
    acl_extra = _resolve_acl_filter(actor, None, request.access_context, conn)
    _ensure_document_allowed(conn, request.document_id, acl_extra)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM versions WHERE id = %s AND document_id = %s",
            (request.version_id, request.document_id),
        )
        version_valid = cur.fetchone() is not None

    if not version_valid:
        _emit_audit("mcp.verify_citation", "mcp.tool.call", actor,
                    metadata={"result": "version_not_found"})
        return VerifyCitationData(
            verified=False,
            current_hash=None,
            hash_matches=False,
            content_snapshot=None,
            version_valid=False,
            message="버전이 유효하지 않습니다.",
        )

    chunk_row = _fetch_accessible_chunk(
        conn,
        actor,
        request.document_id,
        request.version_id,
        request.node_id,
        request.access_context,
    )
    if not chunk_row:
        _emit_audit("mcp.verify_citation", "mcp.tool.call", actor,
                    metadata={"result": "node_not_found"})
        return VerifyCitationData(
            verified=False,
            current_hash=None,
            hash_matches=False,
            content_snapshot=None,
            version_valid=True,
            message="노드를 찾을 수 없습니다.",
        )

    content = chunk_row.get("source_text") or ""
    current_hash = _compute_content_hash(content)
    hash_matches = current_hash == request.content_hash

    # span_offset 기반 스니펫 추출 (선택)
    snapshot: Optional[str] = None
    if request.span_offset is not None and content:
        start = max(0, request.span_offset)
        snapshot = content[start: start + 200]

    verified = version_valid and hash_matches

    _emit_audit("mcp.verify_citation", "mcp.tool.call", actor,
                metadata={"verified": verified, "hash_matches": hash_matches})

    return VerifyCitationData(
        verified=verified,
        current_hash=current_hash,
        hash_matches=hash_matches,
        content_snapshot=snapshot,
        version_valid=version_valid,
        message="검증 성공." if verified else "콘텐츠 해시가 일치하지 않습니다 — 문서가 수정되었을 수 있습니다.",
    )


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _fetch_allowed_doc_ids(conn, acl_extra: dict) -> set:
    """ACL 필터 SQL을 실행하여 허용된 document_id 집합을 반환한다."""
    sql = "SELECT DISTINCT d.id FROM documents d " + acl_extra["sql"].replace("AND (", "WHERE (", 1)
    with conn.cursor() as cur:
        cur.execute(sql, acl_extra["params"])
        return {str(r["id"]) for r in cur.fetchall()}


def _check_agent_write_blocked(actor: ActorContext) -> None:
    """에이전트 킬스위치 상태 확인 — is_disabled이면 즉시 예외."""
    # 읽기 도구이므로 킬스위치는 쓰기에만 적용 (Phase 5에서 쓰기 도구 추가 시 활용)
    # 현재는 AGENT가 비활성 상태면 인증 단계에서 이미 차단됨
    pass


def _resolve_acl_filter(
    actor: ActorContext,
    scope: Optional[str],
    access_context: Optional[AccessContext],
    conn,
) -> dict:
    """Scope Profile 기반 추가 ACL 필터를 SQL 형태로 반환한다."""
    if not actor.is_agent or not actor.scope_profile_id:
        return {"sql": "", "params": []}

    try:
        return apply_scope_filter(
            scope_profile_id=actor.scope_profile_id,
            scope_name=scope or "default",
            access_context={
                "organization_id": access_context.organization_id if access_context else None,
                "team_id": access_context.team_id if access_context else None,
                "user_id": access_context.user_id if access_context else actor.acting_on_behalf_of,
                "permissions": access_context.permissions if access_context else [],
            },
            conn=conn,
        )
    except ScopeFilterResolutionError as exc:
        logger.warning("scope filter resolve failed: %s", exc)
        raise invalid_scope(str(exc)) from exc


def _compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_chunk_acl_clause(
    actor: ActorContext,
    access_context: Optional[AccessContext],
) -> tuple[str, list[Any]]:
    clauses = ["dc.is_public = TRUE"]
    params: list[Any] = []

    actor_role = actor.role
    actor_user_id = access_context.user_id if access_context and access_context.user_id else actor.acting_on_behalf_of or actor.actor_id
    organization_id = access_context.organization_id if access_context else None

    if actor_role:
        clauses.append("%s = ANY(dc.accessible_roles)")
        params.append(actor_role)
    if actor_user_id:
        clauses.append("%s = ANY(dc.accessible_user_ids)")
        params.append(actor_user_id)
    if organization_id:
        clauses.append("%s = ANY(dc.accessible_org_ids)")
        params.append(organization_id)

    return "(" + " OR ".join(clauses) + ")", params


def _fetch_accessible_chunk(
    conn,
    actor: ActorContext,
    document_id: str,
    version_id: Optional[str],
    node_id: str,
    access_context: Optional[AccessContext],
):
    acl_cond, acl_params = _build_chunk_acl_clause(actor, access_context)
    version_cond = "AND dc.version_id = %s::uuid" if version_id else ""
    params: list[Any] = [document_id]
    if version_id:
        params.append(version_id)
    params.append(node_id)
    params.extend(acl_params)

    sql = f"""
        SELECT dc.document_id, dc.version_id, dc.node_id, dc.source_text
        FROM document_chunks dc
        WHERE dc.document_id = %s::uuid
          {version_cond}
          AND dc.node_id = %s::uuid
          AND dc.is_current = TRUE
          AND {acl_cond}
        ORDER BY dc.chunk_index
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _ensure_document_allowed(conn, document_id: str, acl_extra: dict) -> None:
    if not acl_extra.get("sql"):
        return
    allowed_doc_ids = _fetch_allowed_doc_ids(conn, acl_extra)
    if document_id not in allowed_doc_ids:
        raise unauthorized("요청한 문서에 접근할 수 없습니다.")


def _emit_audit(
    event_type: str,
    action: str,
    actor: ActorContext,
    metadata: Optional[dict] = None,
) -> None:
    try:
        audit_emitter.emit(
            event_type=event_type,
            action=action,
            actor_id=actor.actor_id,
            actor_type=actor.audit_actor_type,
            resource_type="mcp_tool",
            resource_id=None,
            result="success",
            metadata=metadata or {},
        )
    except Exception as exc:
        logger.warning("MCP tool 감사 이벤트 기록 실패: %s", exc)


# --------------------------------------------------------------------------- #
# FG 0-5 (2026-04-23): mimir.vectorization.status — 읽기 전용 진단 Tool
# --------------------------------------------------------------------------- #


def tool_vectorization_status(
    request: VectorizationStatusToolRequest,
    actor: ActorContext,
    conn,
) -> VectorizationStatusToolResponse:
    """FG 0-5: 에이전트가 문서의 벡터화 상태를 조회한다 (읽기 전용).

    재벡터화 실행 권한은 **노출하지 않는다** (운영 안전, task0-5 §4.4).
    에이전트 관점에선 `can_reindex` 는 항상 False.

    보안 게이트 (보안보고서 F05-01/F05-02 수정, 2026-04-23):
      - document.read 권한을 라우트와 동일 기준으로 재검증 (IDOR 방어).
      - last_error 는 내부 호스트 정보 노출을 피하기 위해 **요약 코드만** 노출.
    """
    _check_agent_write_blocked(actor)

    # 최소 유효성 — UUID 형식 점검
    import re
    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )
    if not _UUID_RE.match(request.document_id):
        raise MCPError(
            MCPErrorCode.INVALID_PARAMS,
            "document_id 는 유효한 UUID 여야 합니다.",
            400,
        )

    # F05-02 (P1) 수정: document-level authorize 를 MCP Tool 경로에도 적용.
    try:
        from app.api.auth import ResourceRef, authorization_service  # noqa: WPS433
        authorization_service.authorize(
            actor=actor,
            action="document.read",
            resource=ResourceRef(resource_type="document", resource_id=request.document_id),
            require_authenticated=True,
        )
    except Exception as exc:
        # authorize 가 raise 하는 권한 에러는 MCPError 로 정규화
        raise MCPError(
            MCPErrorCode.UNAUTHORIZED,
            f"문서 조회 권한이 없습니다: {type(exc).__name__}",
            403,
        )

    from app.services.vectorization_status_service import get_vectorization_status

    info = get_vectorization_status(
        conn,
        request.document_id,
        actor_user_id=None,
        actor_role=None,
        cooldown_remaining_sec=0,
    )
    if info is None:
        raise MCPError(MCPErrorCode.NOT_FOUND, "문서를 찾을 수 없습니다.", 404)

    _emit_audit(
        event_type="mcp.tool.vectorization_status",
        action="mcp.vectorization.status",
        actor=actor,
        metadata={"document_id": request.document_id, "status": info.status},
    )

    # F05-01 (P1) 수정: 에이전트에게는 상세 에러 대신 요약 코드만 노출.
    # 내부 호스트명·스택 등은 Admin 에게만 REST API 경로로 노출 (라우트는 그대로 info.last_error 사용).
    last_error_code: Optional[str] = None
    if info.last_error:
        lower = info.last_error.lower()
        if "milvus" in lower:
            last_error_code = "milvus_unreachable"
        elif "embedding" in lower:
            last_error_code = "embedding_service_unavailable"
        elif "timeout" in lower:
            last_error_code = "timeout"
        else:
            last_error_code = "vectorization_failed"

    return VectorizationStatusToolResponse(
        document_id=info.document_id,
        status=info.status,
        latest_published_version_id=info.latest_published_version_id,
        indexed_version_id=info.indexed_version_id,
        chunk_count=info.chunk_count,
        last_vectorized_at=info.last_vectorized_at.isoformat() if info.last_vectorized_at else None,
        last_error=last_error_code,
        can_reindex=False,
    )
