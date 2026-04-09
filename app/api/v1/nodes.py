"""
Nodes router — /api/v1/versions/{version_id}/nodes

노드 리소스 API (버전의 하위 리소스).

  GET /api/v1/versions/{version_id}/nodes            : 노드 목록 (flat list)
  GET /api/v1/versions/{version_id}/nodes/{node_id}  : 노드 단건

v1/router.py에서 prefix="/versions"로 마운트됨.
실제 path: /api/v1/versions/{version_id}/nodes[/{node_id}]

플랫폼 규약 적용:
  - Task I-2: success_response / list_response (envelope)
  - Task I-3: ApiNotFoundError (공통 오류 체계)
  - Task I-4: request.state.context → request_id / trace_id
  - Task I-5: resolve_current_actor → actor 추출, authorization_service → authz hook
  - Task I-8: NodesService → 실제 조회 구현

flat structured 응답:
  - parent_id + order_index 기반
  - 중첩 JSON 강제 안 함
  - 이후 view=tree projection 확장 가능
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.services.nodes_service import nodes_service

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /versions/{version_id}/nodes — 노드 목록
# ---------------------------------------------------------------------------


@router.get(
    "/{version_id}/nodes",
    summary="노드 목록 조회",
    description=(
        "특정 버전의 노드 목록을 flat list로 반환한다.\n\n"
        "- 존재하지 않는 version_id이면 404 `resource_not_found`.\n"
        "- 응답은 `parent_id` + `order_index` 기반 flat structured 형식.\n"
        "- 클라이언트에서 parent_id를 이용해 트리를 재구성할 수 있다.\n"
        "- AI/RAG citation: 각 node의 `id`로 직접 참조 가능."
    ),
    response_model=SuccessResponse,
)
def list_nodes(
    version_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="node.list",
        resource=ResourceRef(resource_type="node", parent_id=version_id),
        require_authenticated=False,
    )

    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        node_list = nodes_service.list_nodes(conn, version_id)

    return list_response(
        data=[n.model_dump() for n in node_list],
        request_id=request_id,
        trace_id=trace_id,
        page=1,
        page_size=len(node_list),
        total=len(node_list),
        has_next=False,
    )


# ---------------------------------------------------------------------------
# GET /versions/{version_id}/nodes/{node_id} — 노드 단건 조회
# ---------------------------------------------------------------------------


@router.get(
    "/{version_id}/nodes/{node_id}",
    summary="노드 단건 조회",
    description=(
        "특정 버전의 특정 노드를 조회한다.\n\n"
        "- version-node 관계 검증: 다른 버전 소속 노드 접근 불가.\n"
        "- 존재하지 않거나 관계 불일치 시 404 `resource_not_found`.\n"
        "- AI/RAG deep-linking 및 citation reference 기반 경로."
    ),
    response_model=SuccessResponse,
)
def get_node(
    version_id: str,
    node_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="node.read",
        resource=ResourceRef(
            resource_type="node",
            resource_id=node_id,
            parent_id=version_id,
        ),
        require_authenticated=False,
    )

    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        node = nodes_service.get_node(conn, version_id, node_id)

    return success_response(
        data=node.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )
