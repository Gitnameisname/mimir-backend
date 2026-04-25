"""
Folders 라우터 — /api/v1/folders — S3 Phase 2 FG 2-1.

엔드포인트:
  GET    /folders                   — owner 본인의 전체 폴더 트리 (path 순)
  POST   /folders                   — 생성 (parent_id null = 루트)
  GET    /folders/{id}              — 단건 조회
  PATCH  /folders/{id}              — 이름 변경 (path 도 재계산)
  POST   /folders/{id}/move         — 부모 변경 (순환 참조 방지)
  DELETE /folders/{id}              — 하위/문서 없을 때만 삭제

문서-폴더 연결은 `/documents/{id}/folder` 경로에 별도로 정의
(documents.py 에서 주입).

절대 규칙:
  - 폴더는 뷰 레이어. 폴더 이동이 문서 권한을 바꾸지 않음
  - owner 격리 — 다른 owner 의 폴더는 접근 불가 (get_folder 에서 owner 검증)
"""

from fastapi import APIRouter, Depends, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db import get_db
from app.schemas.folders import (
    FolderCreateRequest,
    FolderMoveRequest,
    FolderRenameRequest,
    FolderResponse,
)
from app.services.folders_service import folders_service

router = APIRouter()


def _to_response(folder) -> FolderResponse:
    return FolderResponse(
        id=folder.id,
        owner_id=folder.owner_id,
        parent_id=folder.parent_id,
        name=folder.name,
        path=folder.path,
        depth=folder.depth,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.get("", summary="폴더 트리 전체", response_model=SuccessResponse)
def list_folders(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="folder.list",
        resource=ResourceRef(resource_type="folder"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        folders = folders_service.list_folders(conn, actor=actor)
    return list_response(
        data=[_to_response(f).model_dump() for f in folders],
        total=len(folders),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post("", status_code=201, summary="폴더 생성", response_model=SuccessResponse)
def create_folder(
    request: Request,
    body: FolderCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="folder.create",
        resource=ResourceRef(resource_type="folder"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        folder = folders_service.create_folder(
            conn, actor=actor, parent_id=body.parent_id, name=body.name,
        )
    audit_emitter.emit_for_actor(
        event_type="folder.created",
        action="folder.create",
        actor=actor,
        resource_type="folder",
        resource_id=folder.id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return success_response(
        data=_to_response(folder).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.get("/{folder_id}", summary="폴더 단건 조회", response_model=SuccessResponse)
def get_folder(
    folder_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="folder.read",
        resource=ResourceRef(resource_type="folder", resource_id=folder_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        folder = folders_service.get_folder(conn, folder_id, actor=actor)
    return success_response(
        data=_to_response(folder).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.patch(
    "/{folder_id}",
    summary="폴더 이름 변경 (path 재계산)",
    response_model=SuccessResponse,
)
def rename_folder(
    folder_id: str,
    request: Request,
    body: FolderRenameRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="folder.update",
        resource=ResourceRef(resource_type="folder", resource_id=folder_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        folder = folders_service.rename_folder(
            conn, folder_id, actor=actor, new_name=body.name,
        )
    audit_emitter.emit_for_actor(
        event_type="folder.renamed",
        action="folder.update",
        actor=actor,
        resource_type="folder",
        resource_id=folder_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return success_response(
        data=_to_response(folder).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post(
    "/{folder_id}/move",
    summary="폴더 이동 (부모 변경 + 하위 path 재계산)",
    response_model=SuccessResponse,
)
def move_folder(
    folder_id: str,
    request: Request,
    body: FolderMoveRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="folder.move",
        resource=ResourceRef(resource_type="folder", resource_id=folder_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        folder = folders_service.move_folder(
            conn, folder_id, actor=actor, new_parent_id=body.new_parent_id,
        )
    audit_emitter.emit_for_actor(
        event_type="folder.moved",
        action="folder.move",
        actor=actor,
        resource_type="folder",
        resource_id=folder_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return success_response(
        data=_to_response(folder).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.delete(
    "/{folder_id}",
    status_code=204,
    summary="폴더 삭제 (비어있을 때만)",
)
def delete_folder(
    folder_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    authorization_service.authorize(
        actor=actor,
        action="folder.delete",
        resource=ResourceRef(resource_type="folder", resource_id=folder_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        folders_service.delete_folder(conn, folder_id, actor=actor)
    audit_emitter.emit_for_actor(
        event_type="folder.deleted",
        action="folder.delete",
        actor=actor,
        resource_type="folder",
        resource_id=folder_id,
        request_id=request_id,
        trace_id=trace_id,
    )
