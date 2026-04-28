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
    _check_tool_allowed(actor, "search_documents", conn=conn)

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
    _check_tool_allowed(actor, "fetch_node", conn=conn)
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
):
    """verify_citation 도구 — Citation 5중 검증 (S3 Phase 4 FG 4-3 강화).

    검사 단계 (모두 통과해야 ``verified=True``):
      1. **exists**: document/version/node 모두 존재
      2. **pinned**: ``version_id`` 가 ``"latest"`` 가 아닌 구체값
      3. **hash_matches**: ``citation_basis`` 에 따라 node_content 또는 rendered_text hash 비교
      4. **quoted_text_in_content**: ``quoted_text`` 입력 시 정본 텍스트에 포함되는지 (None = 미입력)
      5. **span_valid**: ``span_offset`` + ``span_length`` 입력 시 정본 텍스트 범위 내 (None = 미입력)

    R3 강제: ``version_id="latest"`` 즉시 거부 (MCPError INVALID_PARAMS, HTTP 400).
    """
    from app.schemas.mcp import VerifyCitationChecks

    _check_agent_write_blocked(actor)
    _check_tool_allowed(actor, "verify_citation", conn=conn)

    # R3: latest 거부 — 도구 진입점에서 즉시 차단
    if request.version_id == "latest":
        raise MCPError(
            MCPErrorCode.INVALID_REQUEST,
            "verify_citation 은 pinned 버전(vN/UUID)만 수용합니다. 'latest' 는 인용 시점에 resolve 후 저장하세요.",
            400,
        )

    acl_extra = _resolve_acl_filter(actor, None, request.access_context, conn)
    _ensure_document_allowed(conn, request.document_id, acl_extra)

    # 검사 1: 존재성 (version)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM versions WHERE id = %s AND document_id = %s",
            (request.version_id, request.document_id),
        )
        version_row = cur.fetchone()
    version_exists = version_row is not None
    version_status = version_row.get("status") if version_row else None

    if not version_exists:
        _emit_audit("mcp.verify_citation", "mcp.tool.call", actor,
                    metadata={"result": "version_not_found", "verified": False})
        return VerifyCitationData(
            verified=False,
            checks=VerifyCitationChecks(
                exists=False, pinned=True, hash_matches=False,
            ),
            current_hash=None,
            hash_matches=False,
            version_valid=False,
            content_snapshot=None,
            message="버전이 유효하지 않습니다.",
        )

    # 검사 2: pinned — 'latest' 거부 통과 + version 이 published 또는 archived (draft 가 아님)
    # FG 4-3 의 pinned 정의: "draft 가 아닌 인용 가능 상태" (published 상태)
    pinned = version_status in ("published", "archived")

    # 검사 3 / 4 / 5: 정본 텍스트 + hash + 텍스트 포함 + span
    text_for_check: str = ""
    current_hash: Optional[str] = None
    rendered_snapshot: Optional[str] = None
    node_snapshot: Optional[str] = None

    chunk_row = _fetch_accessible_chunk(
        conn,
        actor,
        request.document_id,
        request.version_id,
        request.node_id,
        request.access_context,
    )

    if request.citation_basis == "rendered_text":
        # rendered_text 기반 — render_service 위임
        try:
            from app.repositories.versions_repository import VersionsRepository
            from app.services.render_service import render_service

            versions_repo = VersionsRepository()
            version_obj = versions_repo.get_by_document_and_version_id(
                conn, request.document_id, request.version_id
            )
            if version_obj is None:
                rendered = ""
            else:
                rdoc = render_service.render_version(version_obj)
                # FG 4-2 의 _walk_blocks_for_text 와 동일 패턴
                rendered_text, _ = _walk_blocks_for_text(
                    rdoc.blocks, format="plain_text", include_anchors=False,
                )
                rendered = rendered_text
        except Exception as exc:
            logger.warning("verify_citation rendered_text 계산 실패: %s", exc)
            rendered = ""
        text_for_check = rendered
        current_hash = _compute_content_hash(rendered) if rendered else None
        if request.span_offset is not None and rendered:
            start = max(0, request.span_offset)
            length = request.span_length if request.span_length else 200
            rendered_snapshot = rendered[start: start + length]
    else:
        # node_content 기반 — 기존 동작 (chunk source_text)
        if chunk_row is None:
            _emit_audit("mcp.verify_citation", "mcp.tool.call", actor,
                        metadata={"result": "node_not_found", "verified": False})
            return VerifyCitationData(
                verified=False,
                checks=VerifyCitationChecks(
                    exists=False, pinned=pinned, hash_matches=False,
                ),
                current_hash=None,
                hash_matches=False,
                version_valid=True,
                content_snapshot=None,
                message="노드를 찾을 수 없습니다.",
            )
        node_text = chunk_row.get("source_text") or ""
        text_for_check = node_text
        current_hash = _compute_content_hash(node_text)
        if request.span_offset is not None and node_text:
            start = max(0, request.span_offset)
            length = request.span_length if request.span_length else 200
            node_snapshot = node_text[start: start + length]

    hash_matches = current_hash is not None and current_hash == request.content_hash

    # 검사 4: 텍스트 포함
    quoted_in: Optional[bool] = None
    if request.quoted_text:
        quoted_in = request.quoted_text in text_for_check

    # 검사 5: span 유효성
    span_valid: Optional[bool] = None
    if request.span_offset is not None and request.span_length is not None:
        span_end = request.span_offset + request.span_length
        span_valid = (
            request.span_offset >= 0
            and request.span_length > 0
            and span_end <= len(text_for_check)
        )

    # 종합: 강제 검사 (1/2/3) + 선택 검사 (4/5 — 입력 시 통과 필요)
    verified = (
        version_exists
        and pinned
        and hash_matches
        and (quoted_in if quoted_in is not None else True)
        and (span_valid if span_valid is not None else True)
    )

    if verified:
        message = "검증 성공."
    elif not pinned:
        message = "버전이 published 가 아닙니다 (draft 또는 unknown)."
    elif not hash_matches:
        message = "콘텐츠 해시가 일치하지 않습니다 — 문서가 수정되었을 수 있습니다."
    elif quoted_in is False:
        message = "인용된 텍스트가 본문에 포함되어 있지 않습니다."
    elif span_valid is False:
        message = "span_offset / span_length 가 본문 범위를 벗어났습니다."
    else:
        message = "검증 실패."

    _emit_audit(
        "mcp.verify_citation", "mcp.tool.call", actor,
        metadata={
            "verified": verified,
            "citation_basis": request.citation_basis,
            "checks": {
                "exists": version_exists,
                "pinned": pinned,
                "hash_matches": hash_matches,
                "quoted_text_in_content": quoted_in,
                "span_valid": span_valid,
            },
        },
    )

    return VerifyCitationData(
        verified=verified,
        checks=VerifyCitationChecks(
            exists=version_exists,
            pinned=pinned,
            hash_matches=hash_matches,
            quoted_text_in_content=quoted_in,
            span_valid=span_valid,
        ),
        current_hash=current_hash,
        rendered_snapshot=rendered_snapshot,
        node_snapshot=node_snapshot,
        # 백워드 호환 필드
        content_snapshot=node_snapshot or rendered_snapshot,
        hash_matches=hash_matches,
        version_valid=version_exists,
        message=message,
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


def _check_tool_allowed(actor: ActorContext, tool_name: str, conn=None) -> None:
    """ScopeProfile.allowed_tools 게이트 — 에이전트가 본 도구 호출 허가받았는가.

    S3 Phase 4 FG 4-0 §2.1.6 (2026-04-28). task3-3.md §[129,223–225,318] 흡수.
    모든 MCP `tool_*` 진입점 첫 줄 (`_check_agent_write_blocked` 다음) 에서 호출.

    - actor_type ≠ AGENT → 통과 (사람/시스템 호출은 본 게이트 비대상; 별 게이트가 처리).
    - actor_type == AGENT 이고 ScopeProfile.allowed_tools 에 ``tool_name`` 포함 → 통과.
    - 외 모든 경우 → ``MCPError(UNAUTHORIZED, 403)``.

    ``conn`` 전달 시 동일 트랜잭션에서 ScopeProfile 조회 (성능 + 일관성).
    """
    if not actor.can_call_tool(tool_name, conn=conn):
        raise MCPError(
            MCPErrorCode.UNAUTHORIZED,
            f"본 ScopeProfile 은 도구 '{tool_name}' 을 허용하지 않습니다.",
            403,
        )


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
    _check_tool_allowed(actor, "mimir.vectorization.status", conn=conn)

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


# ---------------------------------------------------------------------------
# S3 Phase 3 FG 3-3: read_annotations Tool
# ---------------------------------------------------------------------------

def tool_read_annotations(
    request: "ReadAnnotationsRequest",
    actor: ActorContext,
    conn,
) -> "ReadAnnotationsResponse":
    """read_annotations 도구 — 문서의 인라인 주석을 읽기 전용으로 반환.

    ACL:
      - documents_service.get_document(actor=actor) 가 viewer scope 밖 문서를 404 처리.
      - 에이전트 actor 의 ScopeProfile 이 문서 접근을 허용해야 함.

    쓰기 (생성/수정/삭제) 는 본 Tool 에 노출되지 않는다 (read-only).
    에이전트가 주석 생성이 필요하면 향후 별 ADR 로 별도 Tool 신설 검토.
    """
    from app.schemas.mcp import ReadAnnotationItem, ReadAnnotationsResponse
    from app.services.annotations_service import annotations_service as _ann_svc

    _check_agent_write_blocked(actor)
    _check_tool_allowed(actor, "read_annotations", conn=conn)

    # UUID 기본 검증
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

    # document.read 권한 재검증 (IDOR 방어 — vectorization_status 와 동일 패턴)
    try:
        from app.api.auth import ResourceRef, authorization_service  # noqa: WPS433
        authorization_service.authorize(
            actor=actor,
            action="document.read",
            resource=ResourceRef(resource_type="document", resource_id=request.document_id),
            require_authenticated=True,
        )
    except Exception as exc:
        raise MCPError(
            MCPErrorCode.UNAUTHORIZED,
            f"문서 조회 권한이 없습니다: {type(exc).__name__}",
            403,
        )

    try:
        items = _ann_svc.list_for_document(
            conn,
            actor=actor,
            document_id=request.document_id,
            include_resolved=request.include_resolved,
            include_orphans=request.include_orphans,
            limit=request.limit,
        )
    except Exception as exc:
        # ACL 차단 (ApiNotFoundError) 또는 기타 → MCP 에러로 매핑
        from app.api.errors.exceptions import ApiNotFoundError
        if isinstance(exc, ApiNotFoundError):
            raise MCPError(MCPErrorCode.NOT_FOUND, "문서를 찾을 수 없습니다.", 404)
        logger.error("mcp.read_annotations failed: %s", exc)
        raise MCPError(MCPErrorCode.INTERNAL_ERROR, f"주석 조회 오류: {exc}", 500)

    _emit_audit(
        event_type="mcp.tool.read_annotations",
        action="mcp.read_annotations",
        actor=actor,
        metadata={
            "document_id": request.document_id,
            "count": len(items),
            "include_resolved": request.include_resolved,
            "include_orphans": request.include_orphans,
        },
    )

    return ReadAnnotationsResponse(
        document_id=request.document_id,
        annotations=[
            ReadAnnotationItem(
                id=a.id,
                document_id=a.document_id,
                node_id=a.node_id,
                span_start=a.span_start,
                span_end=a.span_end,
                author_id=a.author_id,
                actor_type=a.actor_type,
                content=a.content,
                status=a.status,
                parent_id=a.parent_id,
                is_orphan=a.is_orphan,
                created_at=a.created_at.isoformat() if a.created_at else "",
                updated_at=a.updated_at.isoformat() if a.updated_at else "",
                mentioned_user_ids=list(a.mentioned_user_ids or []),
            )
            for a in items
        ],
        truncated=(len(items) >= request.limit),
    )


# ===========================================================================
# S3 Phase 4 FG 4-2 (2026-04-28) — 신규 read 도구 3종
# ===========================================================================

# --------------------------------------------------------------------------- #
# search_nodes — 노드 단위 검색
# --------------------------------------------------------------------------- #


def tool_search_nodes(request, actor: ActorContext, conn):
    """search_nodes 도구 — 노드 그래뉼래리티 검색.

    SearchService.search_nodes 위임 + ScopeProfile ACL post-filter +
    document_ids / node_kinds Python post-filter + content_hash 부착.
    """
    from app.schemas.mcp import SearchNodeItem, SearchNodesData
    from app.services.search_service import SearchService

    _check_agent_write_blocked(actor)
    _check_tool_allowed(actor, "search_nodes", conn=conn)

    # ScopeProfile ACL — search_documents 와 동일 패턴
    acl_extra = _resolve_acl_filter(actor, request.scope, request.access_context, conn)
    allowed_doc_ids: Optional[set] = None
    if acl_extra.get("sql") and acl_extra.get("params") is not None:
        allowed_doc_ids = _fetch_allowed_doc_ids(conn, acl_extra)

    # 단일 document_id 가 주어지면 SQL 필터로 좁힘 — 외 SQL 무필터 후 Python 후처리
    single_doc = (
        request.document_ids[0]
        if request.document_ids and len(request.document_ids) == 1
        else None
    )

    svc = SearchService()
    try:
        # oversample 후 post-filter — top_k * 2, max 100
        oversample_limit = min(max(request.top_k * 2, request.top_k), 100)
        raw = svc.search_nodes(
            conn=conn,
            q=request.query,
            document_id=single_doc,
            actor_role=actor.role,
            limit=oversample_limit,
        )
    except Exception as exc:
        logger.error("mcp.search_nodes failed: %s", exc)
        raise MCPError(MCPErrorCode.INTERNAL_ERROR, f"검색 오류: {exc}", 500)

    raw_items = raw.results if hasattr(raw, "results") else (raw or [])
    requested_doc_ids = set(request.document_ids or [])
    requested_kinds = set(request.node_kinds or [])

    items: list[SearchNodeItem] = []
    total_matched = len(raw_items)
    for node in raw_items:
        doc_id = str(getattr(node, "document_id", "") or "")
        if not doc_id:
            continue
        # ScopeProfile ACL
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        # document_ids 다중 필터 (단일은 SQL 단계에서 이미 적용)
        if requested_doc_ids and doc_id not in requested_doc_ids:
            continue
        # node_kinds 필터
        node_kind = str(getattr(node, "node_type", "") or "")
        if requested_kinds and node_kind not in requested_kinds:
            continue
        node_id = str(getattr(node, "node_id", "") or "")
        version_id = str(getattr(node, "version_id", "") or "") or None
        snippet = str(
            getattr(node, "content_snippet", "")
            or getattr(node, "title", "")
            or ""
        )
        score = float(getattr(node, "rank", 0) or 0.0)
        items.append(
            SearchNodeItem(
                document_id=doc_id,
                version_id=version_id,
                node_id=node_id,
                node_kind=node_kind,
                snippet=snippet,
                score=score,
                content_hash=_compute_content_hash(snippet),
            )
        )
        if len(items) >= request.top_k:
            break

    truncated_at = request.top_k if total_matched > request.top_k else None

    _emit_audit(
        event_type="mcp.search_nodes",
        action="mcp.tool.call",
        actor=actor,
        metadata={"query": request.query[:100], "items": len(items)},
    )

    return SearchNodesData(
        items=items,
        total_matched=total_matched,
        truncated_at=truncated_at,
    )


# --------------------------------------------------------------------------- #
# read_document_render — 렌더링 텍스트 + node_anchors + render_hash
# --------------------------------------------------------------------------- #


def _walk_blocks_for_text(
    blocks,
    *,
    format: str,
    include_anchors: bool,
):
    """RenderBlock 트리를 walk 하여 (rendered_text, node_anchors) 반환.

    format: 'plain_text' (단순 텍스트 + 줄바꿈) 또는 'markdown' (heading/list 보존).
    """
    parts: list[str] = []
    anchors: list = []
    cursor = 0

    def _emit_node(node_id: str, text: str) -> None:
        nonlocal cursor
        if not text:
            return
        if include_anchors and node_id:
            anchors.append({
                "node_id": node_id,
                "offset_start": cursor,
                "offset_end": cursor + len(text),
            })
        parts.append(text)
        cursor += len(text)

    def _emit_separator(sep: str) -> None:
        nonlocal cursor
        parts.append(sep)
        cursor += len(sep)

    def _g(b, key: str, default=None):
        """dict / object 양쪽에서 안전하게 속성 추출."""
        if isinstance(b, dict):
            return b.get(key, default)
        return getattr(b, key, default)

    def _walk(items):
        for b in items or []:
            block_type = _g(b, "block_type", "")
            block_id = _g(b, "block_id", "")
            content = _g(b, "content")
            heading_level = _g(b, "heading_level")
            ordered = _g(b, "ordered")
            children = _g(b, "children") or []
            text = str(content or "")
            if format == "markdown":
                if block_type == "heading":
                    level = int(heading_level or 1)
                    rendered = ("#" * max(1, min(6, level))) + " " + text + "\n"
                elif block_type == "list_item":
                    bullet = "- " if not ordered else "1. "
                    rendered = bullet + text + "\n"
                elif block_type == "paragraph":
                    rendered = text + "\n"
                elif block_type == "quote":
                    rendered = "> " + text + "\n"
                elif block_type in ("section", "appendix"):
                    rendered = "## " + text + "\n" if text else ""
                else:
                    rendered = text + ("\n" if text else "")
            else:
                rendered = text + ("\n" if text else "")
            _emit_node(block_id, rendered)
            if children:
                _walk(children)

    _walk(blocks)
    if format == "markdown":
        # 트레일링 줄바꿈 정리
        text = "".join(parts).rstrip("\n") + "\n"
    else:
        text = "".join(parts).rstrip("\n")
    # cursor 와 text length 가 일치해야 anchors 가 유효
    if len(text) != cursor:
        # rstrip 으로 길이가 줄었을 때 anchors 가 텍스트 끝을 넘으면 클램프
        anchors = [
            {**a, "offset_end": min(a["offset_end"], len(text)), "offset_start": min(a["offset_start"], len(text))}
            for a in anchors
        ]
    return text, anchors


def tool_read_document_render(request, actor: ActorContext, conn):
    """read_document_render 도구 — 렌더링 텍스트 + node_anchors + render_hash.

    R3 (pinned): version_id='latest' 입력 시 즉시 vN 으로 resolve.
    응답의 version_id 는 항상 구체 (UUID).
    """
    from app.schemas.mcp import NodeAnchor, ReadDocumentRenderData
    from app.mcp.uri_builder import resolve_version_id
    from app.services.render_service import render_service
    from app.repositories.versions_repository import VersionsRepository

    _check_agent_write_blocked(actor)
    _check_tool_allowed(actor, "read_document_render", conn=conn)

    # ACL — fetch_node 와 동일 (document.read 재검증)
    acl_extra = _resolve_acl_filter(actor, None, request.access_context, conn)
    _ensure_document_allowed(conn, request.document_id, acl_extra)

    # version 결정 — 'latest' 또는 None 이면 published 로 resolve
    resolved_version_id = resolve_version_id(conn, request.document_id, request.version_id)
    if not resolved_version_id:
        raise not_found(f"문서 {request.document_id} 의 published 버전을 찾을 수 없습니다.")

    # version 객체 조회
    versions_repo = VersionsRepository()
    version = versions_repo.get_by_document_and_version_id(
        conn, request.document_id, resolved_version_id
    )
    if version is None:
        raise not_found(f"버전을 찾을 수 없습니다: {resolved_version_id}")

    # render
    try:
        rendered_doc = render_service.render_version(version)
    except Exception as exc:
        logger.error("mcp.read_document_render failed: %s", exc)
        raise MCPError(MCPErrorCode.INTERNAL_ERROR, f"렌더링 오류: {exc}", 500)

    # blocks → text + anchors
    rendered_text, anchors_dicts = _walk_blocks_for_text(
        rendered_doc.blocks,
        format=request.format,
        include_anchors=request.include_node_anchors,
    )
    render_hash = _compute_content_hash(rendered_text)

    _emit_audit(
        event_type="mcp.read_document_render",
        action="mcp.tool.call",
        actor=actor,
        metadata={
            "document_id": request.document_id,
            "version_id": resolved_version_id,
            "format": request.format,
            "len": len(rendered_text),
        },
    )

    return ReadDocumentRenderData(
        document_id=request.document_id,
        version_id=resolved_version_id,
        format=request.format,
        rendered_text=rendered_text,
        render_hash=render_hash,
        node_anchors=[NodeAnchor(**a) for a in anchors_dicts] if request.include_node_anchors else [],
    )


# --------------------------------------------------------------------------- #
# resolve_document_reference — 자연어 → document_id + version_ref
# --------------------------------------------------------------------------- #


def tool_resolve_document_reference(request, actor: ActorContext, conn):
    """resolve_document_reference 도구 — 5단계 해소.

    R3 보장: version_ref 는 'vN' 또는 'latest_published' — 'latest' 단독 절대 미반환.
    """
    from app.schemas.mcp import ResolveCandidate, ResolveDocumentReferenceData
    from app.services.document_resolver_service import resolve_reference

    _check_agent_write_blocked(actor)
    _check_tool_allowed(actor, "resolve_document_reference", conn=conn)

    # ScopeProfile ACL — 후보가 통과 문서만으로
    acl_extra = _resolve_acl_filter(actor, request.scope, request.access_context, conn)
    allowed_doc_ids: Optional[set] = None
    if acl_extra.get("sql") and acl_extra.get("params") is not None:
        allowed_doc_ids = _fetch_allowed_doc_ids(conn, acl_extra)

    recent = []
    if request.context and request.context.recent_document_ids:
        recent = list(request.context.recent_document_ids)

    result = resolve_reference(
        conn,
        request.reference,
        recent_document_ids=recent,
        preferred_doc_types=request.preferred_doc_types,
        max_candidates=request.max_candidates,
        confidence_threshold=request.confidence_threshold,
        allowed_doc_ids=allowed_doc_ids,
    )

    def _to_schema(c):
        return ResolveCandidate(
            document_id=c.document_id,
            version_ref=c.version_ref,
            title=c.title,
            confidence=c.confidence,
            match_kind=c.match_kind,
        )

    _emit_audit(
        event_type="mcp.resolve_document_reference",
        action="mcp.tool.call",
        actor=actor,
        metadata={
            "reference": request.reference[:100],
            "resolved": result.resolved,
            "candidates": len(result.candidates),
        },
    )

    return ResolveDocumentReferenceData(
        resolved=result.resolved,
        needs_disambiguation=result.needs_disambiguation,
        best_match=_to_schema(result.best_match) if result.best_match else None,
        candidates=[_to_schema(c) for c in result.candidates],
    )


# ===========================================================================
# S3 Phase 4 FG 4-6 (2026-04-28) — L2 write 도구 (save_draft)
# MCP 표면 최초 쓰기 도구. propose 만 — 사람 reviewer 승인 후 별도 trigger 로 머지.
# 4 사전 조건: idempotency / human approval / impact preview / 감사 로그 4종.
# ===========================================================================


def tool_save_draft(request, actor: ActorContext, conn):
    """save_draft 도구 — L2 propose 전용 (자동 머지 0).

    R3 / R4 보존. R1 (L4 차단) 그대로 — 본 도구는 L2 (가역).
    propose 만 — agent 가 self-approve 불가 (agent_proposal_service 가 검사).
    """
    from app.schemas.mcp import (
        DraftImpactPreview,
        SaveDraftData,
        SaveDraftRequest,
    )
    from app.services.agent_proposal_service import agent_proposal_service

    _check_agent_write_blocked(actor)
    _check_tool_allowed(actor, "save_draft", conn=conn)

    # ACL: 다른 scope 의 문서에 draft 못 만듦 (기존 문서 케이스만)
    if request.document_id:
        acl_extra = _resolve_acl_filter(actor, request.scope, request.access_context, conn)
        _ensure_document_allowed(conn, request.document_id, acl_extra)

    # FG 4-6 §2.1.3: impact preview 사전 계산
    impact_dict = agent_proposal_service.compute_draft_impact(
        conn,
        document_id=request.document_id,
        content_snapshot=request.content_snapshot,
    )
    impact = DraftImpactPreview(**impact_dict)

    # FG 4-6 §2.1.5: idempotent propose
    # agent_id 는 actor.agent_id (None 이면 actor.actor_id fallback)
    agent_id = actor.agent_id or actor.actor_id
    if agent_id is None:
        raise MCPError(
            MCPErrorCode.UNAUTHORIZED,
            "save_draft 는 agent_id 가 식별된 actor 만 사용 가능합니다.",
            403,
        )

    try:
        result = agent_proposal_service.propose_draft(
            conn,
            agent_id=agent_id,
            acting_on_behalf_of=actor.acting_on_behalf_of,
            document_id=request.document_id,
            document_type_id=request.document_type_id,
            title=request.title,
            content="",  # content_snapshot 우선
            content_snapshot=request.content_snapshot,
            metadata=request.metadata or {},
            reason=request.reason or "agent draft proposal (FG 4-6)",
            idempotency_key=request.idempotency_key,
        )
    except Exception as exc:
        from app.api.errors.exceptions import (
            ApiConflictError,
            ApiNotFoundError,
            ApiPermissionDeniedError,
        )
        if isinstance(exc, ApiNotFoundError):
            raise MCPError(MCPErrorCode.NOT_FOUND, str(exc), 404)
        if isinstance(exc, ApiPermissionDeniedError):
            raise MCPError(MCPErrorCode.UNAUTHORIZED, str(exc), 403)
        if isinstance(exc, ApiConflictError):
            raise MCPError(MCPErrorCode.INVALID_REQUEST, str(exc), 409)
        logger.error("mcp.save_draft failed: %s", exc)
        raise MCPError(MCPErrorCode.INTERNAL_ERROR, f"draft 제안 오류: {exc}", 500)

    # FG 4-6 §2.1.4: 감사 로그 — agent_proposal.requested
    _emit_audit(
        event_type="agent_proposal.requested",
        action="mcp.save_draft",
        actor=actor,
        metadata={
            "document_id": result.get("document_id"),
            "version_id": result.get("version_id"),
            "proposal_id": result.get("draft_id"),
            "idempotency_key": request.idempotency_key,
            "idempotent_replay": result.get("idempotent_replay", False),
            "impact": impact_dict,
        },
    )

    return SaveDraftData(
        proposal_id=result.get("draft_id") or "",
        status=result.get("status") or "proposed",
        document_id=result.get("document_id") or "",
        version_id=result.get("version_id") or "",
        impact=impact,
        requires_human_approval=True,
        audit_event="agent_proposal.requested",
        message=(
            "기존 draft 와 동일 (idempotent replay) — reviewer approval 대기."
            if result.get("idempotent_replay")
            else "draft 가 제안되었습니다. reviewer approval 후 적용됩니다."
        ),
    )
