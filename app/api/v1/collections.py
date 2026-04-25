"""
Collections 라우터 — /api/v1/collections — S3 Phase 2 FG 2-1.

엔드포인트:
  GET    /collections                           — owner 본인 목록
  POST   /collections                           — 생성
  GET    /collections/{id}                      — 단건 조회
  PATCH  /collections/{id}                      — 이름/설명 수정
  DELETE /collections/{id}                      — 삭제
  POST   /collections/{id}/documents            — 문서 추가
  DELETE /collections/{id}/documents/{doc_id}   — 문서 제거
  GET    /collections/{id}/documents            — 컬렉션 내 문서 id (Scope 필터)

절대 규칙:
  - 컬렉션은 ACL 에 영향을 주지 않는다 (순수 뷰).
  - 컬렉션에 추가되는 문서는 FG 2-0 viewer Scope ACL 을 통과해야 한다
    (Scope 밖 문서는 조용히 거부 — 존재 유출 방지).
  - owner 본인 또는 bypass role 만 편집/조회.
"""

from fastapi import APIRouter, Depends, Query, Request
from typing import Optional

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db import get_db
from app.schemas.collections import (
    CollectionAddDocumentsRequest,
    CollectionAddDocumentsResponse,
    CollectionCreateRequest,
    CollectionResponse,
    CollectionUpdateRequest,
)
from app.services.collections_service import collections_service

router = APIRouter()


def _to_response(coll) -> CollectionResponse:
    return CollectionResponse(
        id=coll.id,
        owner_id=coll.owner_id,
        name=coll.name,
        description=coll.description,
        created_at=coll.created_at,
        updated_at=coll.updated_at,
        document_count=coll.document_count,
    )


@router.get("", summary="컬렉션 목록", response_model=SuccessResponse)
def list_collections(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="collection.list",
        resource=ResourceRef(resource_type="collection"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        colls, total = collections_service.list_collections(
            conn, actor=actor, limit=limit, offset=offset,
        )
    return list_response(
        data=[_to_response(c).model_dump() for c in colls],
        total=total,
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post(
    "",
    status_code=201,
    summary="컬렉션 생성",
    response_model=SuccessResponse,
)
def create_collection(
    request: Request,
    body: CollectionCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="collection.create",
        resource=ResourceRef(resource_type="collection"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        coll = collections_service.create_collection(
            conn, actor=actor, name=body.name, description=body.description,
        )
    audit_emitter.emit_for_actor(
        event_type="collection.created",
        action="collection.create",
        actor=actor,
        resource_type="collection",
        resource_id=coll.id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return success_response(
        data=_to_response(coll).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.get(
    "/{collection_id}",
    summary="컬렉션 단건 조회",
    response_model=SuccessResponse,
)
def get_collection(
    collection_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="collection.read",
        resource=ResourceRef(resource_type="collection", resource_id=collection_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        coll = collections_service.get_collection(conn, collection_id, actor=actor)
    return success_response(
        data=_to_response(coll).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.patch(
    "/{collection_id}",
    summary="컬렉션 수정",
    response_model=SuccessResponse,
)
def update_collection(
    collection_id: str,
    request: Request,
    body: CollectionUpdateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="collection.update",
        resource=ResourceRef(resource_type="collection", resource_id=collection_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        if body.has_updates():
            coll = collections_service.update_collection(
                conn, collection_id,
                actor=actor,
                name=body.name,
                description=body.description,
            )
        else:
            coll = collections_service.get_collection(conn, collection_id, actor=actor)
    audit_emitter.emit_for_actor(
        event_type="collection.updated",
        action="collection.update",
        actor=actor,
        resource_type="collection",
        resource_id=collection_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return success_response(
        data=_to_response(coll).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.delete(
    "/{collection_id}",
    status_code=204,
    summary="컬렉션 삭제",
)
def delete_collection(
    collection_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    authorization_service.authorize(
        actor=actor,
        action="collection.delete",
        resource=ResourceRef(resource_type="collection", resource_id=collection_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        collections_service.delete_collection(conn, collection_id, actor=actor)
    audit_emitter.emit_for_actor(
        event_type="collection.deleted",
        action="collection.delete",
        actor=actor,
        resource_type="collection",
        resource_id=collection_id,
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post(
    "/{collection_id}/documents",
    summary="컬렉션에 문서 추가",
    response_model=SuccessResponse,
)
def add_documents(
    collection_id: str,
    request: Request,
    body: CollectionAddDocumentsRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="collection.add_documents",
        resource=ResourceRef(resource_type="collection", resource_id=collection_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        report = collections_service.add_documents(
            conn, collection_id,
            actor=actor,
            document_ids=body.document_ids,
        )
    audit_emitter.emit_for_actor(
        event_type="collection.documents_added",
        action="collection.add_documents",
        actor=actor,
        resource_type="collection",
        resource_id=collection_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return success_response(
        data=CollectionAddDocumentsResponse(**report).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.delete(
    "/{collection_id}/documents/{document_id}",
    status_code=204,
    summary="컬렉션에서 문서 제거",
)
def remove_document(
    collection_id: str,
    document_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    authorization_service.authorize(
        actor=actor,
        action="collection.remove_document",
        resource=ResourceRef(resource_type="collection", resource_id=collection_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        collections_service.remove_document(
            conn, collection_id, actor=actor, document_id=document_id,
        )
    audit_emitter.emit_for_actor(
        event_type="collection.document_removed",
        action="collection.remove_document",
        actor=actor,
        resource_type="collection",
        resource_id=collection_id,
        request_id=request_id,
        trace_id=trace_id,
    )


@router.get(
    "/{collection_id}/documents",
    summary="컬렉션 내 문서 id 목록 (viewer Scope 필터)",
    response_model=SuccessResponse,
)
def list_collection_documents(
    collection_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="collection.read",
        resource=ResourceRef(resource_type="collection", resource_id=collection_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        doc_ids = collections_service.list_document_ids(
            conn, collection_id, actor=actor,
        )
    return success_response(
        data={"collection_id": collection_id, "document_ids": doc_ids},
        request_id=request_id,
        trace_id=trace_id,
    )
