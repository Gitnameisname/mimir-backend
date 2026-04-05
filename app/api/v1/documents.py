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
from app.api.query import FilterFieldSpec, ListQuerySpec, ParsedListQuery, make_list_query_dependency
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.schemas.documents import DocumentCreateRequest, DocumentResponse, DocumentUpdateRequest
from app.schemas.versions import VersionCreateRequest
from app.services.documents_service import documents_service
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


def _ctx(request: Request) -> tuple[Optional[str], Optional[str]]:
    """request context에서 request_id, trace_id를 추출한다."""
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None
    return ctx.request_id, ctx.trace_id


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
        require_authenticated=False,  # TODO: enforcement 활성화 시 True로 전환
    )

    request_id, trace_id = _ctx(request)

    with get_db() as conn:
        docs, total = documents_service.list_documents(conn, query)

    page = query.page if query.page else 1
    page_size = query.page_size if query.page_size else 20
    has_next = (page * page_size) < total

    return list_response(
        data=[doc.model_dump() for doc in docs],
        request_id=request_id,
        trace_id=trace_id,
        page=page,
        page_size=page_size,
        total=total,
        has_next=has_next,
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
        require_authenticated=False,
    )

    request_id, trace_id = _ctx(request)
    actor_id = actor.actor_id if actor.is_authenticated else None

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

    # Task I-10: audit candidate emit
    from app.audit.emitter import audit_emitter
    audit_emitter.emit(
        event_type="document.created",
        action="document.create",
        actor_id=actor_id,
        resource_type="document",
        resource_id=doc.id,
        result="success",
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
        require_authenticated=False,  # TODO: enforcement 활성화 시 True로 전환
    )

    request_id, trace_id = _ctx(request)

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
        require_authenticated=False,
    )

    request_id, trace_id = _ctx(request)
    actor_id = actor.actor_id if actor.is_authenticated else None

    with get_db() as conn:
        doc = documents_service.update_document(
            conn,
            document_id,
            body,
            actor_id=actor_id,
        )

    # Task I-10: audit candidate emit
    from app.audit.emitter import audit_emitter
    audit_emitter.emit(
        event_type="document.updated",
        action="document.update",
        actor_id=actor_id,
        resource_type="document",
        resource_id=document_id,
        result="success",
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
    request_id, trace_id = _ctx(request)

    with get_db() as conn:
        versions, total = versions_service.list_versions(conn, document_id, query)

    page = query.page if query.page else 1
    page_size = query.page_size if query.page_size else 20
    has_next = (page * page_size) < total

    return list_response(
        data=[v.model_dump() for v in versions],
        request_id=request_id,
        trace_id=trace_id,
        page=page,
        page_size=page_size,
        total=total,
        has_next=has_next,
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
        require_authenticated=False,
    )

    request_id, trace_id = _ctx(request)
    actor_id = actor.actor_id if actor.is_authenticated else None

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

    # Task I-10: audit candidate emit
    from app.audit.emitter import audit_emitter
    audit_emitter.emit(
        event_type="version.created",
        action="version.create",
        actor_id=actor_id,
        resource_type="version",
        resource_id=version.id,
        result="success",
        request_id=request_id,
        trace_id=trace_id,
        metadata={"document_id": document_id, "version_number": version.version_number},
    )

    return response
