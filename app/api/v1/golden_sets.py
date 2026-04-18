"""
Golden Set CRUD API 라우터 — Phase 7 FG7.1 + FG7.1 버전 관리.

엔드포인트:
  POST   /golden-sets                            — GoldenSet 생성
  GET    /golden-sets                            — 목록 조회
  GET    /golden-sets/{id}                       — 상세 조회 (items 포함)
  PUT    /golden-sets/{id}                       — 메타데이터 수정
  DELETE /golden-sets/{id}                       — Soft delete

  POST   /golden-sets/{id}/items                 — Q&A 항목 추가
  GET    /golden-sets/{id}/items                 — 항목 목록 조회
  PUT    /golden-sets/{id}/items/{item_id}       — 항목 수정
  DELETE /golden-sets/{id}/items/{item_id}       — 항목 soft delete

  GET    /golden-sets/{id}/versions              — 버전 이력
  GET    /golden-sets/{id}/versions/{v}          — 특정 버전 스냅샷
  GET    /golden-sets/{id}/versions/{fv}/diff/{tv} — 버전 diff

S2 원칙 ⑤: actor_type 감사 로그 기록
S2 원칙 ⑥: scope_id 기반 ACL 필터 필수
S2 원칙 ⑦: 폐쇄망 환경 지원 (외부 API 미사용)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import json

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.models.golden_set import (
    GoldenItemCreateRequest,
    GoldenItemResponse,
    GoldenItemUpdateRequest,
    GoldenSetCreateRequest,
    GoldenSetDetailResponse,
    GoldenSetResponse,
    GoldenSetUpdateRequest,
    GoldenSetVersionDiff,
    GoldenSetVersionInfo,
)
from app.models.golden_set_import_export import (
    GoldenSetImportRequest,
    GoldenSetImportResult,
)
from app.repositories.golden_set_repository import (
    GoldenItemRepository,
    GoldenSetRepository,
)
from app.services.golden_set_import_export_service import GoldenSetImportExportService

logger = logging.getLogger(__name__)

router = APIRouter()

_WRITE_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN", "AUTHOR", "REVIEWER", "APPROVER"})


# ---------------------------------------------------------------------------
# Helper: scope_id 추출 (S2 ⑥)
# ---------------------------------------------------------------------------

def _require_scope(actor: ActorContext) -> str:
    """현재 actor의 scope_id를 추출한다. 없으면 403."""
    sid = getattr(actor, "scope_profile_id", None)
    if not sid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scope Profile이 바인딩되지 않은 계정입니다. (S2 ⑥)",
        )
    return str(sid)


def _require_auth(actor: ActorContext) -> None:
    if not actor.is_authenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증 필요")


def _require_write(actor: ActorContext) -> None:
    _require_auth(actor)
    if actor.role not in _WRITE_ROLES:
        raise ApiPermissionDeniedError("쓰기 권한이 없습니다.")


def _actor_type_str(actor: ActorContext) -> str:
    return actor.actor_type.value if actor.actor_type else "user"


def _item_to_response(item) -> GoldenItemResponse:
    return GoldenItemResponse(
        id=item.id,
        golden_set_id=item.golden_set_id,
        version=item.version,
        question=item.question,
        expected_answer=item.expected_answer,
        expected_source_docs=item.expected_source_docs,
        expected_citations=item.expected_citations,
        notes=item.notes,
        created_at=item.created_at,
        created_by=item.created_by,
        updated_at=item.updated_at,
        updated_by=item.updated_by,
    )


def _set_to_response(gs) -> GoldenSetResponse:
    return GoldenSetResponse(
        id=gs.id,
        scope_id=gs.scope_id,
        name=gs.name,
        description=gs.description,
        domain=gs.domain.value if hasattr(gs.domain, "value") else gs.domain,
        status=gs.status.value if hasattr(gs.status, "value") else gs.status,
        version=gs.version,
        item_count=getattr(gs, "item_count", None),
        extra_metadata=gs.extra_metadata,
        created_at=gs.created_at,
        created_by=gs.created_by,
        updated_at=gs.updated_at,
        updated_by=gs.updated_by,
        is_deleted=gs.is_deleted,
    )


def _set_to_detail(gs) -> GoldenSetDetailResponse:
    base = _set_to_response(gs)
    return GoldenSetDetailResponse(
        **base.model_dump(),
        items=[_item_to_response(i) for i in (gs.items or [])],
    )


# ---------------------------------------------------------------------------
# GoldenSet CRUD
# ---------------------------------------------------------------------------

@router.post("", response_model=SuccessResponse, status_code=201)
def create_golden_set(
    request: GoldenSetCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_write(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"
    actor_type = _actor_type_str(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        gs = repo.create(scope_id=scope_id, request=request, created_by=actor_id)

    audit_emitter.emit(
        event_type="golden_set.created",
        action="golden_set.create",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="golden_set",
        resource_id=gs.id,
        result="success",
        metadata={"name": gs.name, "domain": gs.domain},
    )
    return success_response(data=_set_to_response(gs))


@router.get("", response_model=SuccessResponse)
def list_golden_sets(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    domain: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        sets, total = repo.list_by_scope(
            scope_id, offset=offset, limit=limit, domain=domain, status=status
        )

    page = (offset // limit) + 1
    return list_response(
        data=[_set_to_response(gs) for gs in sets],
        page=page,
        page_size=limit,
        total=total,
        has_next=(offset + limit) < total,
    )


@router.get("/{golden_set_id}", response_model=SuccessResponse)
def get_golden_set(
    golden_set_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        gs = repo.get_by_id(golden_set_id, scope_id, include_items=True)

    if not gs:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")
    return success_response(data=_set_to_detail(gs))


@router.put("/{golden_set_id}", response_model=SuccessResponse)
def update_golden_set(
    golden_set_id: str,
    request: GoldenSetUpdateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_write(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"
    actor_type = _actor_type_str(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        updated = repo.update(golden_set_id, scope_id, request, updated_by=actor_id)

    if not updated:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")

    audit_emitter.emit(
        event_type="golden_set.updated",
        action="golden_set.update",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="golden_set",
        resource_id=golden_set_id,
        result="success",
        metadata=request.model_dump(exclude_none=True),
    )
    return success_response(data=_set_to_response(updated))


@router.delete("/{golden_set_id}", status_code=204)
def delete_golden_set(
    golden_set_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    _require_write(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        ok = repo.soft_delete(golden_set_id, scope_id)

    if not ok:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")

    audit_emitter.emit(
        event_type="golden_set.deleted",
        action="golden_set.delete",
        actor_id=actor_id,
        actor_type=_actor_type_str(actor),
        resource_type="golden_set",
        resource_id=golden_set_id,
        result="success",
    )


# ---------------------------------------------------------------------------
# GoldenItem CRUD
# ---------------------------------------------------------------------------

@router.post("/{golden_set_id}/items", response_model=SuccessResponse, status_code=201)
def add_golden_item(
    golden_set_id: str,
    request: GoldenItemCreateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_write(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"

    with get_db() as conn:
        repo = GoldenItemRepository(conn)
        item = repo.create_item(
            golden_set_id=golden_set_id, scope_id=scope_id,
            request=request, created_by=actor_id,
        )

    if not item:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")

    audit_emitter.emit(
        event_type="golden_item.added",
        action="golden_item.create",
        actor_id=actor_id,
        actor_type=_actor_type_str(actor),
        resource_type="golden_item",
        resource_id=item.id,
        result="success",
        metadata={"golden_set_id": golden_set_id, "question": item.question[:100]},
    )
    return success_response(data=_item_to_response(item))


@router.get("/{golden_set_id}/items", response_model=SuccessResponse)
def list_golden_items(
    golden_set_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        repo = GoldenItemRepository(conn)
        items = repo.list_items(golden_set_id, scope_id)

    if items is None:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")

    paged = items[offset: offset + limit]
    page = (offset // limit) + 1
    return list_response(
        data=[_item_to_response(i) for i in paged],
        page=page,
        page_size=limit,
        total=len(items),
        has_next=(offset + limit) < len(items),
    )


@router.put("/{golden_set_id}/items/{item_id}", response_model=SuccessResponse)
def update_golden_item(
    golden_set_id: str,
    item_id: str,
    request: GoldenItemUpdateRequest,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_write(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"

    with get_db() as conn:
        repo = GoldenItemRepository(conn)
        updated = repo.update_item(item_id, scope_id, request, updated_by=actor_id)

    if not updated:
        raise HTTPException(status_code=404, detail="GoldenItem을 찾을 수 없습니다.")

    audit_emitter.emit(
        event_type="golden_item.modified",
        action="golden_item.update",
        actor_id=actor_id,
        actor_type=_actor_type_str(actor),
        resource_type="golden_item",
        resource_id=item_id,
        result="success",
        metadata={"golden_set_id": golden_set_id},
    )
    return success_response(data=_item_to_response(updated))


@router.delete("/{golden_set_id}/items/{item_id}", status_code=204)
def delete_golden_item(
    golden_set_id: str,
    item_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    _require_write(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"

    with get_db() as conn:
        repo = GoldenItemRepository(conn)
        ok = repo.soft_delete_item(item_id, scope_id, deleted_by=actor_id)

    if not ok:
        raise HTTPException(status_code=404, detail="GoldenItem을 찾을 수 없습니다.")

    audit_emitter.emit(
        event_type="golden_item.deleted",
        action="golden_item.delete",
        actor_id=actor_id,
        actor_type=_actor_type_str(actor),
        resource_type="golden_item",
        resource_id=item_id,
        result="success",
        metadata={"golden_set_id": golden_set_id},
    )


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

@router.get("/{golden_set_id}/versions", response_model=SuccessResponse)
def get_version_history(
    golden_set_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        history = repo.get_version_history(golden_set_id, scope_id)

    if not history:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")
    return success_response(data=[v.model_dump() for v in history])


@router.get("/{golden_set_id}/versions/{version}", response_model=SuccessResponse)
def get_version_snapshot(
    golden_set_id: str,
    version: int,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        snapshot = repo.get_version_snapshot(golden_set_id, scope_id, version)

    if not snapshot:
        raise HTTPException(status_code=404, detail="해당 버전을 찾을 수 없습니다.")
    return success_response(data=snapshot)


@router.get("/{golden_set_id}/versions/{from_v}/diff/{to_v}", response_model=SuccessResponse)
def get_version_diff(
    golden_set_id: str,
    from_v: int,
    to_v: int,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        repo = GoldenSetRepository(conn)
        snap_from = repo.get_version_snapshot(golden_set_id, scope_id, from_v)
        snap_to = repo.get_version_snapshot(golden_set_id, scope_id, to_v)

    if not snap_from or not snap_to:
        raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

    from_ids = {item["id"] for item in snap_from.get("items", [])}
    to_ids = {item["id"] for item in snap_to.get("items", [])}

    diff = GoldenSetVersionDiff(
        from_version=from_v,
        to_version=to_v,
        items_added=sorted(to_ids - from_ids),
        items_modified=[],  # 상세 필드 비교는 FG7.2에서 확장
        items_deleted=sorted(from_ids - to_ids),
        modified_at=datetime.fromisoformat(snap_to["created_at"]),
    )
    return success_response(data=diff.model_dump())


# ---------------------------------------------------------------------------
# Import / Export
# ---------------------------------------------------------------------------

_IMPORT_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN", "AUTHOR"})
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.get("/{golden_set_id}/export", response_model=SuccessResponse)
def export_golden_set(
    golden_set_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    """GoldenSet 전체를 JSON 형식으로 export."""
    _require_auth(actor)
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"

    with get_db() as conn:
        svc = GoldenSetImportExportService(conn)
        exported = svc.export(golden_set_id, scope_id)

    if not exported:
        raise HTTPException(status_code=404, detail="GoldenSet을 찾을 수 없습니다.")

    audit_emitter.emit(
        event_type="golden_set.exported",
        action="golden_set.export",
        actor_id=actor_id,
        actor_type=_actor_type_str(actor),
        resource_type="golden_set",
        resource_id=golden_set_id,
        result="success",
        metadata={"item_count": len(exported.items)},
    )
    return success_response(data=exported.model_dump())


@router.post("/{golden_set_id}/import", response_model=SuccessResponse)
def import_golden_set(
    golden_set_id: str,
    file: UploadFile = File(...),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    """JSON 파일을 업로드하여 GoldenItem 대량 import.

    권한: ORG_ADMIN / SUPER_ADMIN / AUTHOR 이상.
    중복 question 감지 시 400 반환.
    allow_partial=True: 일부 실패해도 나머지 import 진행.
    """
    _require_auth(actor)
    if actor.role not in _IMPORT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="import 권한이 없습니다. (ORG_ADMIN / SUPER_ADMIN / AUTHOR 필요)",
        )
    scope_id = _require_scope(actor)
    actor_id = actor.actor_id or "anonymous"

    # 파일 타입 및 확장자 검증 (SEC5-BE-001: 허용 MIME만 수락)
    _ALLOWED_MIME = {"application/json", "text/json", "text/plain"}
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in _ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="JSON 파일만 업로드할 수 있습니다.")
    filename = file.filename or ""
    if filename and not filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="파일 확장자는 .json이어야 합니다.")

    # 파일 읽기 및 크기 제한
    raw = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="파일 크기가 10MB를 초과합니다.")

    # JSON 파싱
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 파싱 오류: {exc}") from exc

    # 스키마 검증
    try:
        import_req = GoldenSetImportRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"스키마 검증 오류: {exc}") from exc

    # 중복 question 사전 검사 (allow_partial=False로 한 번 더 확인)
    from app.models.golden_set_import_export import ImportValidator
    ok, errs = ImportValidator.validate(import_req)
    if not ok:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    with get_db() as conn:
        svc = GoldenSetImportExportService(conn)
        _, result = svc.import_items(
            golden_set_id, scope_id, import_req, actor_id, allow_partial=True
        )

    audit_emitter.emit(
        event_type="golden_set.imported",
        action="golden_set.import",
        actor_id=actor_id,
        actor_type=_actor_type_str(actor),
        resource_type="golden_set",
        resource_id=golden_set_id,
        result="success",
        metadata={
            "total": result.total_items,
            "success": result.successful_items,
            "fail": result.failed_items,
        },
    )
    return success_response(data=result.model_dump())


@router.post("/{golden_set_id}/verify-round-trip", response_model=SuccessResponse)
def verify_round_trip(
    golden_set_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    """Export → reimport 스키마 재검증 (DB 변경 없음)."""
    _require_auth(actor)
    scope_id = _require_scope(actor)

    with get_db() as conn:
        svc = GoldenSetImportExportService(conn)
        is_ok, errors = svc.verify_round_trip(golden_set_id, scope_id)

    if not is_ok and errors and errors[0] == "GoldenSet을 찾을 수 없습니다.":
        raise HTTPException(status_code=404, detail=errors[0])

    return success_response(data={"is_consistent": is_ok, "errors": errors})
