"""Document Graph 라우터 — S3 Phase 2 FG 2-4.

엔드포인트:
    GET /api/v1/documents/graph?limit=&include_tag_nodes=&include_collection_nodes=
                              &collection=&folder=&tag=
        - viewer Scope 안 documents + (옵션) 메타노드 + 엣지

ACL:
    - documents 는 `_resolve_viewer_scope_profile_ids(actor)` 로 viewer scope 강제
    - 메타노드 (tag / collection) 는 visible documents 의 메타만 자연 필터
    - viewer 가 자기 scope 외 document 는 그래프에서 절대 노출 안 됨 (S3 ⑥ 뷰 ≠ 권한)

라우터 등록 순서:
    `/graph` 가 documents 라우터의 `/{document_id}` 보다 먼저 매칭되어야 함 — FG 2-3
    `document_links` 라우터와 동일 패턴. `app/api/v1/router.py` 에서 documents 보다 앞에 include.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, success_response
from app.db import get_db
from app.schemas.document_graph import GraphEdgeOut, GraphNodeOut, GraphResponseOut
from app.services.documents_service import _resolve_viewer_scope_profile_ids
from app.services.graph_service import DEFAULT_LIMIT, MAX_LIMIT, build_graph

router = APIRouter()


@router.get(
    "/graph",
    summary="문서 그래프 데이터 (FG 2-4)",
    description=(
        "viewer Scope 안 documents 와 그 메타데이터를 그래프 elements 로 반환한다.\n\n"
        "**노드 상한**: 기본 500, 최대 2000. truncated=true 면 잘림.\n\n"
        "**옵션 필터**: `?collection=<id>` / `?folder=<id>` / `?tag=<name>`\n\n"
        "**메타노드**: 기본 false. `?include_tag_nodes=true` / `?include_collection_nodes=true` 로 활성화.\n\n"
        "**ACL**: viewer scope 외 document 는 응답에 절대 포함되지 않음."
    ),
    response_model=SuccessResponse,
    tags=["wikilinks", "graph"],
)
def get_document_graph(
    request: Request,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    include_tag_nodes: bool = Query(default=False),
    include_collection_nodes: bool = Query(default=False),
    collection: Optional[str] = Query(default=None, alias="collection"),
    folder: Optional[str] = Query(default=None, alias="folder"),
    tag: Optional[str] = Query(default=None, alias="tag", min_length=1, max_length=64),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.list",
        resource=ResourceRef(resource_type="document"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    scope_ids = _resolve_viewer_scope_profile_ids(actor)

    with get_db() as conn:
        graph = build_graph(
            conn,
            viewer_scope_profile_ids=scope_ids,
            limit=limit,
            collection_id=collection,
            folder_id=folder,
            tag_name_normalized=tag,
            include_tag_nodes=include_tag_nodes,
            include_collection_nodes=include_collection_nodes,
        )

    response = GraphResponseOut(
        nodes=[
            GraphNodeOut(
                id=n.id,
                type=n.type,
                title=n.title,
                document_type=n.document_type,
            )
            for n in graph.nodes
        ],
        edges=[
            GraphEdgeOut(source=e.source, target=e.target, type=e.type)
            for e in graph.edges
        ],
        truncated=graph.truncated,
        total_documents=graph.total_documents,
    )
    return success_response(
        data=response.model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )
