"""
Documents router — /api/v1/documents

플랫폼 규약 적용 현황:
  - Task I-2: success_response / list_response (envelope)
  - Task I-3: ApiNotFoundError / ApiValidationError (공통 오류 체계)
  - Task I-4: request.state.context → request_id / trace_id
  - Task I-5: resolve_current_actor → actor 추출, authorization_service → authz hook
  - Task I-6: make_list_query_dependency → list query parser/validator
  - Task I-7: DocumentsService → 실제 CRUD 구현
  - Task I-8: VersionsService → versions 하위 리소스 구현
  - Task I-9: IdempotencyService → POST write endpoint idempotency hook
  - Task I-10: audit_emitter → write 성공 시 감사 이벤트 emit

router 역할 (thin router 원칙):
  - request parsing & dependency injection
  - authorization hook 호출
  - idempotency hook 호출 (write 경로)
  - service 호출 (비즈니스 로직은 service에)
  - response shaping (공통 envelope 적용)
  - audit candidate emit (write 성공 후)
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.query import FilterFieldSpec, ListQuerySpec, ParsedListQuery, make_list_query_dependency
from app.api.responses import SuccessResponse, list_response, paginated_list_response, success_response
from app.db import get_db
from app.schemas.documents import DocumentCreateRequest, DocumentResponse, DocumentUpdateRequest
from app.schemas.render import RenderDocument
from app.schemas.versions import DraftNodeSaveRequest, DraftSaveRequest, PublishRequest, RestoreRequest, VersionCreateRequest
from app.audit.emitter import audit_emitter
from app.services.documents_service import documents_service
from app.services.draft_service import draft_service
from app.services.render_service import render_service
from app.services.versions_service import versions_service

router = APIRouter()

# ---------------------------------------------------------------------------
# Resource-specific list query spec
# ---------------------------------------------------------------------------

_DOCUMENTS_SPEC = ListQuerySpec(
    allowed_sort_fields=["created_at", "updated_at", "title", "status"],
    allowed_filter_fields=[
        FilterFieldSpec(
            name="status",
            allowed_values=["draft", "published", "archived", "deprecated"],
        ),
        FilterFieldSpec(name="document_type"),
        FilterFieldSpec(name="owner_id", type="uuid"),
    ],
)

_VERSIONS_SPEC = ListQuerySpec(
    allowed_sort_fields=["created_at", "version_number"],
    allowed_filter_fields=[
        FilterFieldSpec(
            name="status",
            allowed_values=["draft", "published", "archived"],
        ),
    ],
)


# ---------------------------------------------------------------------------
# GET /documents — 문서 목록
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="문서 목록 조회",
    description=(
        "문서 목록을 반환한다.\n\n"
        "**Pagination**: `page` + `page_size` (기본값 1 / 20, 최대 page_size=100)\n\n"
        "**Sort**: `sort=created_at,-updated_at` 형식. "
        "허용 필드: `created_at`, `updated_at`, `title`, `status`\n\n"
        "**Filter**: `status=draft`, `document_type=policy`, `owner_id=<uuid>`"
    ),
    response_model=SuccessResponse,
)
def list_documents(
    request: Request,
    query: ParsedListQuery = Depends(make_list_query_dependency(_DOCUMENTS_SPEC)),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.list",
        resource=ResourceRef(resource_type="document"),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        docs, total = documents_service.list_documents(conn, query)

    return paginated_list_response(
        data=[doc.model_dump() for doc in docs],
        query=query,
        total=total,
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /documents — 문서 생성
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=201,
    summary="문서 생성",
    description=(
        "새 문서를 생성한다.\n\n"
        "- `document_type`은 create 후 변경 불가(immutable).\n"
        "- `status` 미입력 시 `draft`로 생성.\n"
        "- `metadata`는 확장용 key-value 구조 (JSONB, 최대 64KB).\n\n"
        "**Idempotency**: `X-Idempotency-Key` 헤더를 지원할 예정 (Task I-9)."
    ),
    response_model=SuccessResponse,
)
def create_document(
    request: Request,
    body: DocumentCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_idempotency_key: Optional[str] = Header(
        default=None,
        alias="X-Idempotency-Key",
        description="멱등성 키 (Task I-9에서 실제 처리 예정)",
    ),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.create",
        resource=ResourceRef(resource_type="document"),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    # Task I-9: idempotency hook
    from app.services.idempotency_service import idempotency_service
    replay_response = idempotency_service.check_and_replay(
        key=x_idempotency_key,
        actor_id=actor_id,
        action="document.create",
        request_body=body.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )
    if replay_response is not None:
        return replay_response

    with get_db() as conn:
        doc = documents_service.create_document(conn, body, actor_id=actor_id)

    response = success_response(
        data=doc.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )

    # Task I-9: finalize idempotency record
    idempotency_service.finalize(
        key=x_idempotency_key,
        actor_id=actor_id,
        action="document.create",
        resource_id=doc.id,
        response=response,
        request_id=request_id,
        trace_id=trace_id,
    )

    audit_emitter.emit_for_actor(
        event_type="document.created",
        action="document.create",
        actor=actor,
        resource_type="document",
        resource_id=doc.id,
        request_id=request_id,
        trace_id=trace_id,
    )

    return response


# ---------------------------------------------------------------------------
# GET /documents/{document_id} — 문서 단건 조회
# ---------------------------------------------------------------------------


@router.get(
    "/{document_id}",
    summary="문서 단건 조회",
    description=(
        "특정 문서를 조회한다.\n\n"
        "- 존재하지 않으면 404 `resource_not_found`.\n"
        "- versions/nodes 세부 정보는 포함하지 않음 (Task I-8에서 확장)."
    ),
    response_model=SuccessResponse,
)
def get_document(
    document_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.read",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        doc = documents_service.get_document(conn, document_id)

    return success_response(
        data=doc.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# PATCH /documents/{document_id} — 문서 부분 수정
# ---------------------------------------------------------------------------


@router.patch(
    "/{document_id}",
    summary="문서 수정",
    description=(
        "문서를 부분 수정한다 (PATCH semantics).\n\n"
        "- 명시된 필드만 수정. `null`/미포함 필드는 유지.\n"
        "- `document_type`은 immutable — 요청에 포함해도 무시.\n"
        "- `metadata`: 전체 replace 정책 (shallow merge 아님).\n"
        "- 수정할 필드가 없으면 현재 상태 그대로 반환.\n\n"
        "**Idempotency**: `X-Idempotency-Key` 헤더를 지원할 예정 (Task I-9)."
    ),
    response_model=SuccessResponse,
)
def update_document(
    document_id: str,
    request: Request,
    body: DocumentUpdateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_idempotency_key: Optional[str] = Header(
        default=None,
        alias="X-Idempotency-Key",
        description="멱등성 키 (Task I-9에서 실제 처리 예정)",
    ),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.update",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    with get_db() as conn:
        doc = documents_service.update_document(
            conn,
            document_id,
            body,
            actor_id=actor_id,
        )

    audit_emitter.emit_for_actor(
        event_type="document.updated",
        action="document.update",
        actor=actor,
        resource_type="document",
        resource_id=document_id,
        request_id=request_id,
        trace_id=trace_id,
    )

    return success_response(
        data=doc.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/versions — 버전 목록
# ---------------------------------------------------------------------------


@router.get(
    "/{document_id}/versions",
    summary="문서 버전 목록 조회",
    description=(
        "특정 문서의 버전 목록을 반환한다.\n\n"
        "**Sort**: `sort=created_at,-version_number` 형식. "
        "허용 필드: `created_at`, `version_number`\n\n"
        "- 문서가 존재하지 않으면 404 `resource_not_found`.\n"
        "- nodes는 포함하지 않음. `GET /versions/{id}/nodes` 로 별도 조회."
    ),
    response_model=SuccessResponse,
    tags=["versions"],
)
def list_document_versions(
    document_id: str,
    request: Request,
    query: ParsedListQuery = Depends(make_list_query_dependency(_VERSIONS_SPEC)),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="version.list",
        resource=ResourceRef(resource_type="version", parent_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)

    actor_role = actor.role if hasattr(actor, "role") else None

    with get_db() as conn:
        versions, total = draft_service.list_versions(
            conn, document_id, query, actor_role=actor_role
        )

    return paginated_list_response(
        data=[v.model_dump() for v in versions],
        query=query,
        total=total,
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /documents/{document_id}/versions — 버전 생성
# ---------------------------------------------------------------------------


@router.post(
    "/{document_id}/versions",
    status_code=201,
    summary="버전 생성",
    description=(
        "특정 문서에 새 버전(구조 스냅샷)을 생성한다.\n\n"
        "- `nodes`에 이 버전의 구조 단위를 함께 전달할 수 있다.\n"
        "- `version_number`는 자동 부여 (문서별 1-based 순차 증가).\n"
        "- 문서가 존재하지 않으면 404 `resource_not_found`.\n\n"
        "**Idempotency**: `X-Idempotency-Key` 헤더 지원 (Task I-9)."
    ),
    response_model=SuccessResponse,
    tags=["versions"],
)
def create_document_version(
    document_id: str,
    request: Request,
    body: VersionCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_idempotency_key: Optional[str] = Header(
        default=None,
        alias="X-Idempotency-Key",
        description="멱등성 키 (Task I-9에서 실제 처리)",
    ),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="version.create",
        resource=ResourceRef(resource_type="version", parent_id=document_id),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    # Task I-9: idempotency hook 연결 (x_idempotency_key 처리)
    from app.services.idempotency_service import idempotency_service
    replay_response = idempotency_service.check_and_replay(
        key=x_idempotency_key,
        actor_id=actor_id,
        action="version.create",
        request_body=body.model_dump(),
        path_params={"document_id": document_id},
        request_id=request_id,
        trace_id=trace_id,
    )
    if replay_response is not None:
        return replay_response

    with get_db() as conn:
        version = versions_service.create_version(
            conn, document_id, body, actor_id=actor_id
        )

    response = success_response(
        data=version.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )

    # Task I-9: 성공 후 idempotency record finalize
    idempotency_service.finalize(
        key=x_idempotency_key,
        actor_id=actor_id,
        action="version.create",
        resource_id=version.id,
        response=response,
        request_id=request_id,
        trace_id=trace_id,
    )

    audit_emitter.emit_for_actor(
        event_type="version.created",
        action="version.create",
        actor=actor,
        resource_type="version",
        resource_id=version.id,
        request_id=request_id,
        trace_id=trace_id,
        metadata={"document_id": document_id, "version_number": version.version_number},
    )

    return response


# ===========================================================================
# Phase 4: Draft / Publish / Restore / Version Detail / Render 엔드포인트
# ===========================================================================


# ---------------------------------------------------------------------------
# PUT /documents/{document_id}/draft — Draft 저장 (생성 또는 전체 교체)
# ---------------------------------------------------------------------------


@router.put(
    "/{document_id}/draft",
    status_code=200,
    summary="Draft 저장",
    description=(
        "문서의 현재 Draft를 전체 교체하거나 새 Draft를 생성한다.\n\n"
        "- Draft가 없으면 새로 생성 (version_number 자동 증가).\n"
        "- Draft가 있으면 content_snapshot을 포함한 내용 전체를 교체한다.\n"
        "- `content_snapshot`: type='document' 루트를 포함하는 전체 본문 트리.\n\n"
        "**권한**: editor 이상"
    ),
    response_model=SuccessResponse,
    tags=["draft"],
)
def save_draft(
    document_id: str,
    request: Request,
    body: DraftSaveRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="draft.save",
        resource=ResourceRef(resource_type="version", parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    with get_db() as conn:
        version = draft_service.save_draft(conn, document_id, body, actor_id=actor_id)

    audit_emitter.emit_for_actor(
        event_type="draft.updated",
        action="draft.save",
        actor=actor,
        resource_type="version",
        resource_id=version.id,
        request_id=request_id,
        trace_id=trace_id,
        metadata={"document_id": document_id, "version_number": version.version_number},
    )

    return success_response(data=version.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# PATCH /documents/{document_id}/versions/{version_id}/draft — 에디터 노드 저장
# ---------------------------------------------------------------------------


@router.patch(
    "/{document_id}/versions/{version_id}/draft",
    status_code=200,
    summary="Draft 노드 저장 (에디터)",
    description=(
        "에디터에서 편집한 노드 목록 + 제목을 Draft 버전에 저장한다.\n\n"
        "- `nodes`: 현재 버전의 전체 노드 목록으로 교체 (기존 노드 모두 삭제 후 삽입).\n"
        "- `title`: 지정 시 `title_snapshot` 및 `documents.title`을 동기화한다.\n"
        "- version_id가 현재 Draft 버전이 아니면 409.\n"
        "- 워크플로 상태가 편집 불가(IN_REVIEW/APPROVED/PUBLISHED)이면 409.\n\n"
        "**권한**: editor 이상"
    ),
    response_model=SuccessResponse,
    tags=["draft"],
)
def save_draft_nodes(
    document_id: str,
    version_id: str,
    request: Request,
    body: DraftNodeSaveRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="draft.save",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    with get_db() as conn:
        version = draft_service.save_draft_nodes(
            conn, document_id, version_id, body, actor_id=actor_id
        )

    audit_emitter.emit_for_actor(
        event_type="draft.nodes_saved",
        action="draft.save",
        actor=actor,
        resource_type="version",
        resource_id=version.id,
        request_id=request_id,
        trace_id=trace_id,
        metadata={
            "document_id": document_id,
            "version_number": version.version_number,
            "node_count": len(body.nodes),
        },
    )

    return success_response(data=version.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# DELETE /documents/{document_id}/draft — Draft 폐기
# ---------------------------------------------------------------------------


@router.delete(
    "/{document_id}/draft",
    status_code=200,
    summary="Draft 폐기",
    description=(
        "현재 활성 Draft를 폐기한다.\n\n"
        "- Draft 버전 status를 `discarded`로 변경하고 포인터를 해제한다.\n"
        "- 활성 Draft가 없으면 409 반환.\n\n"
        "**권한**: editor 이상"
    ),
    response_model=SuccessResponse,
    tags=["draft"],
)
def discard_draft(
    document_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="draft.discard",
        resource=ResourceRef(resource_type="version", parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    with get_db() as conn:
        draft_service.discard_draft(conn, document_id, actor_id=actor_id)

    audit_emitter.emit_for_actor(
        event_type="draft.discarded",
        action="draft.discard",
        actor=actor,
        resource_type="document",
        resource_id=document_id,
        request_id=request_id,
        trace_id=trace_id,
    )

    return success_response(
        data={"message": "Draft discarded successfully"},
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /documents/{document_id}/publish — Draft → Published 발행
# ---------------------------------------------------------------------------


@router.post(
    "/{document_id}/publish",
    status_code=200,
    summary="문서 발행",
    description=(
        "현재 활성 Draft를 Published 상태로 전환한다.\n\n"
        "- 기존 Published 버전은 `superseded` 상태로 변경된다.\n"
        "- 활성 Draft가 없으면 409 반환.\n\n"
        "**권한**: publisher 이상"
    ),
    response_model=SuccessResponse,
    tags=["draft"],
)
def publish_document(
    document_id: str,
    request: Request,
    body: PublishRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.publish",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id

    with get_db() as conn:
        version = draft_service.publish(conn, document_id, body, actor_id=actor_id)

    audit_emitter.emit_for_actor(
        event_type="document.published",
        action="document.publish",
        actor=actor,
        resource_type="version",
        resource_id=version.id,
        request_id=request_id,
        trace_id=trace_id,
        metadata={"document_id": document_id, "version_number": version.version_number},
        previous_state="draft",
        new_state="published",
    )

    return success_response(data=version.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/versions — 버전 목록 (Phase 4 교체)
# 기존 list_document_versions는 유지하되, draft_service.list_versions로 교체
# ---------------------------------------------------------------------------

# (기존 엔드포인트는 그대로 유지, draft_service가 is_current_* 플래그 포함 반환)


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/versions/latest — 최신(현재 published) 버전 조회
# ---------------------------------------------------------------------------


@router.get(
    "/{document_id}/versions/latest",
    summary="최신 버전 조회",
    description=(
        "문서의 현재 Published 버전을 반환한다.\n\n"
        "- Published 버전이 없으면 404 반환."
    ),
    response_model=SuccessResponse,
    tags=["versions"],
)
def get_latest_version(
    document_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    from app.api.errors.exceptions import ApiNotFoundError
    from app.repositories.versions_repository import versions_repository

    authorization_service.authorize(
        actor=actor,
        action="version.read",
        resource=ResourceRef(resource_type="version", parent_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        # draft 우선, 없으면 published 순으로 반환
        version = (
            versions_repository.get_active_draft(conn, document_id)
            or versions_repository.get_current_published(conn, document_id)
        )

        if version is None:
            raise ApiNotFoundError("No version found for this document")

        actor_role = actor.role if hasattr(actor, "role") else None
        detail = draft_service.get_version_detail(
            conn, document_id, version.id,
            actor_role=actor_role,
            include_content=False,
        )

    return success_response(data=detail.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/versions/{version_id} — 버전 상세 조회
# ---------------------------------------------------------------------------


@router.get(
    "/{document_id}/versions/{version_id}",
    summary="버전 상세 조회",
    description=(
        "특정 버전의 상세 정보를 반환한다.\n\n"
        "- content_snapshot, lineage, publish_info, actions 포함.\n"
        "- `include_content=false` 쿼리 파라미터로 content_snapshot 제외 가능.\n"
        "- `is_current_draft` / `is_current_published` 플래그 포함.\n"
        "- `actions.can_restore` 로 복원 가능 여부 확인 가능."
    ),
    response_model=SuccessResponse,
    tags=["versions"],
)
def get_version_detail(
    document_id: str,
    version_id: str,
    request: Request,
    include_content: bool = True,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="version.read",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)
    actor_role = actor.role if hasattr(actor, "role") else None

    with get_db() as conn:
        detail = draft_service.get_version_detail(
            conn, document_id, version_id,
            actor_role=actor_role,
            include_content=include_content,
        )

    return success_response(data=detail.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# POST /documents/{document_id}/versions/{version_id}/restore — 버전 복원
# ---------------------------------------------------------------------------


@router.post(
    "/{document_id}/versions/{version_id}/restore",
    status_code=201,
    summary="버전 복원",
    description=(
        "과거 버전을 기준으로 새 Draft를 생성한다.\n\n"
        "- 복원 대상: `published` 또는 `superseded` 상태 버전만 허용.\n"
        "- 기존 활성 Draft가 있으면 409 반환 (먼저 폐기 필요).\n"
        "- 복원 결과 새 Draft에 `restored_from_version_id` 기록.\n\n"
        "**권한**: publisher 이상"
    ),
    response_model=SuccessResponse,
    tags=["versions"],
)
def restore_version(
    document_id: str,
    version_id: str,
    request: Request,
    body: RestoreRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="version.restore",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    actor_id = actor.resolved_id
    actor_role = actor.role if hasattr(actor, "role") else "publisher"  # stub: 권한 실제 연동 전 publisher로 간주

    with get_db() as conn:
        new_draft = draft_service.restore(
            conn, document_id, version_id, body,
            actor_id=actor_id,
            actor_role=actor_role,
        )

    audit_emitter.emit_for_actor(
        event_type="version.restored",
        action="version.restore",
        actor=actor,
        resource_type="version",
        resource_id=new_draft.id,
        request_id=request_id,
        trace_id=trace_id,
        metadata={"document_id": document_id, "new_version_number": new_draft.version_number},
        new_state="draft",
        target_version_id=version_id,
    )

    return success_response(
        data=new_draft.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/render — 현재 문서 렌더링
# ---------------------------------------------------------------------------


@router.get(
    "/{document_id}/render",
    summary="현재 문서 렌더링",
    description=(
        "현재 문서를 렌더링 ViewModel로 반환한다.\n\n"
        "- `view=published` (기본): current_published 기준 렌더링.\n"
        "- `view=draft`: current_draft 기준 렌더링 (editor+ 권한 필요).\n"
        "- 해당 버전이 없으면 404 반환."
    ),
    response_model=SuccessResponse[RenderDocument],
    tags=["render"],
)
def render_document(
    document_id: str,
    request: Request,
    view: str = "published",
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.render",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)

    from app.api.errors.exceptions import ApiNotFoundError
    from app.repositories.versions_repository import versions_repository

    with get_db() as conn:
        doc = documents_service.get_document(conn, document_id)

        if view == "draft":
            if not doc.current_draft_version_id:
                raise ApiNotFoundError("No active draft for this document")
            version = versions_repository.get_by_id(conn, doc.current_draft_version_id)
        else:
            if not doc.current_published_version_id:
                raise ApiNotFoundError("No published version for this document")
            version = versions_repository.get_by_id(conn, doc.current_published_version_id)

    if version is None:
        raise ApiNotFoundError("Version not found")

    render_result = render_service.render_version(
        version,
        current_draft_id=doc.current_draft_version_id,
        current_published_id=doc.current_published_version_id,
    )

    return success_response(data=render_result.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/versions/{version_id}/render — 특정 버전 렌더링
# ---------------------------------------------------------------------------


@router.get(
    "/{document_id}/versions/{version_id}/render",
    summary="특정 버전 렌더링",
    description=(
        "특정 버전을 렌더링 ViewModel로 반환한다.\n\n"
        "- 버전이 존재하지 않거나 해당 문서에 속하지 않으면 404 반환.\n"
        "- 이력 탐색/복원 확인 용도로 사용한다."
    ),
    response_model=SuccessResponse[RenderDocument],
    tags=["render"],
)
def render_version_endpoint(
    document_id: str,
    version_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="version.render",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)

    from app.api.errors.exceptions import ApiNotFoundError
    from app.repositories.documents_repository import documents_repository
    from app.repositories.versions_repository import versions_repository

    with get_db() as conn:
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        version = versions_repository.get_by_document_and_version_id(
            conn, document_id, version_id
        )
        if version is None:
            raise ApiNotFoundError(
                f"Version '{version_id}' not found in document '{document_id}'"
            )

    render_result = render_service.render_version(
        version,
        current_draft_id=doc.current_draft_version_id,
        current_published_id=doc.current_published_version_id,
    )

    return success_response(data=render_result.model_dump(), request_id=request_id, trace_id=trace_id)
