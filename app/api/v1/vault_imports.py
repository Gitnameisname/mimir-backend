"""Vault Imports 라우터 — S3 Phase 2 FG 2-6.

엔드포인트:
    POST   /api/v1/vault-imports              multipart upload + scope_profile_id
    GET    /api/v1/vault-imports              owner 본인 목록
    GET    /api/v1/vault-imports/{id}         단건 (owner 또는 admin)
    POST   /api/v1/vault-imports/{id}/cancel  running → cancelled

ACL:
    - 모든 엔드포인트가 인증 필요
    - GET 단건 / cancel 은 owner 본인 또는 admin
    - POST upload 시 scope_profile_id 강제 — 변환된 documents 의 scope 정본
"""
from __future__ import annotations

import os
import tempfile

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.errors.exceptions import ApiAuthenticationError, ApiNotFoundError
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.models.vault_import import VaultImport
from app.repositories.vault_imports_repository import vault_imports_repository
from app.schemas.vault_imports import VaultImportResponse
from app.services import vault_import_config as cfg
from app.services.vault_import_service import process_import
from app.utils.actor import ADMIN_ROLES

router = APIRouter()


def _to_response(v: VaultImport) -> VaultImportResponse:
    return VaultImportResponse(
        id=v.id,
        uploaded_filename=v.uploaded_filename,
        bytes_original=v.bytes_original,
        bytes_extracted=v.bytes_extracted,
        file_count=v.file_count,
        status=v.status,  # type: ignore[arg-type]
        scope_profile_id=v.scope_profile_id,
        started_at=v.started_at,
        finished_at=v.finished_at,
        report=v.report,
        created_at=v.created_at,
    )


def _require_authenticated(actor: ActorContext) -> str:
    if not actor.resolved_id:
        raise ApiAuthenticationError("로그인이 필요합니다")
    return actor.resolved_id


def _can_access(actor: ActorContext, owner_id: str) -> bool:
    """owner 본인 또는 admin 만 vault import 단건 접근."""
    if actor.resolved_id == owner_id:
        return True
    return bool(actor.role and actor.role in ADMIN_ROLES)


# ---------------------------------------------------------------------------
# POST /vault-imports — 업로드
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="옵시디언 vault zip 업로드 + 비동기 import 시작",
    response_model=SuccessResponse,
    status_code=201,
)
async def upload_vault_import(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    scope_profile_id: str = Form(...),
    apply_pii_mask: bool = Form(False),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)

    if not file.filename:
        raise HTTPException(status_code=400, detail="파일이 없습니다")

    # 임시 파일 저장 (로컬 디스크). finally 블록에서 process_import 가 정리.
    fd, tmp_path = tempfile.mkstemp(prefix="vault_import_", suffix=".zip")
    bytes_written = 0
    try:
        # 사이즈 한도 검증하며 streaming write
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await file.read(1024 * 64)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > cfg.MAX_ZIP_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"zip 크기 상한 {cfg.MAX_ZIP_BYTES:,} 바이트 초과",
                    )
                f.write(chunk)
    except Exception:
        # 업로드 단계 실패 — 임시 파일 즉시 삭제
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    # vault_imports row 생성 (pending)
    with get_db() as conn:
        record = vault_imports_repository.create(
            conn,
            owner_id=actor_id,
            uploaded_filename=file.filename,
            scope_profile_id=scope_profile_id,
            bytes_original=bytes_written,
        )

    # 백그라운드 처리 시작 — process_import 가 status running → succeeded/failed
    background_tasks.add_task(
        process_import,
        import_id=record.id,
        file_path=tmp_path,
        apply_pii_mask=apply_pii_mask,
        delete_after=True,
    )

    return success_response(
        data=_to_response(record).model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /vault-imports — 목록
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="vault import 목록 (owner 본인)",
    response_model=SuccessResponse,
)
def list_vault_imports(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        items, total = vault_imports_repository.list_by_owner(
            conn, owner_id=actor_id, page=page, page_size=page_size,
        )

    return list_response(
        data=[_to_response(v).model_dump(mode="json") for v in items],
        request_id=request_id,
        trace_id=trace_id,
        page=page, page_size=page_size, total=total,
    )


# ---------------------------------------------------------------------------
# GET /vault-imports/{id} — 단건
# ---------------------------------------------------------------------------

@router.get(
    "/{import_id}",
    summary="vault import 단건 (owner 또는 admin)",
    response_model=SuccessResponse,
)
def get_vault_import(
    import_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        record = vault_imports_repository.get_by_id(conn, import_id)
    if record is None:
        raise ApiNotFoundError(f"Vault import '{import_id}' not found")
    if not _can_access(actor, record.owner_id):
        # 존재 유출 차단 — 다른 owner 의 import 도 404
        raise ApiNotFoundError(f"Vault import '{import_id}' not found")

    return success_response(
        data=_to_response(record).model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /vault-imports/{id}/cancel — running → cancelled
# ---------------------------------------------------------------------------

@router.post(
    "/{import_id}/cancel",
    summary="진행 중 vault import 취소 (owner 본인만)",
    response_model=SuccessResponse,
)
def cancel_vault_import(
    import_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        existing = vault_imports_repository.get_by_id(conn, import_id)
        if existing is None or existing.owner_id != actor_id:
            raise ApiNotFoundError(f"Vault import '{import_id}' not found")
        if existing.status not in ("pending", "running"):
            raise HTTPException(
                status_code=409,
                detail=f"cancel 불가 — 현재 상태 '{existing.status}'",
            )
        # owner_id 강제 — 다른 사용자가 import_id 알아도 cancel 불가
        updated = vault_imports_repository.update_status(
            conn, import_id=import_id, status="cancelled", owner_id=actor_id,
        )

    if updated is None:
        raise ApiNotFoundError(f"Vault import '{import_id}' not found")

    return success_response(
        data=_to_response(updated).model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )
