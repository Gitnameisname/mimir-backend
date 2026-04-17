"""
Extraction Schema CRUD API 라우터 — Phase 8 FG8.1

엔드포인트:
  POST   /extraction-schemas              — 스키마 생성
  GET    /extraction-schemas/{doc_type}   — 최신 스키마 조회
  PUT    /extraction-schemas/{doc_type}   — 스키마 업데이트 (새 버전)
  DELETE /extraction-schemas/{doc_type}   — 소프트 삭제
  GET    /extraction-schemas/{doc_type}/versions — 버전 이력 조회
  PATCH  /extraction-schemas/{doc_type}/deprecate — 폐기 표시

S2 원칙:
  ⑤ actor_type 감사 로그 기록
  ⑥ scope_profile_id ACL 슬롯 (현재: 저장만; 서비스 레이어에서 확장 가능)
  ⑦ 폐쇄망 동등성
"""
from __future__ import annotations

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.repositories.extraction_schema_repository import ActorInfo, ExtractionSchemaRepository
from app.schemas.extraction import (
    CreateExtractionSchemaRequest,
    DeprecateExtractionSchemaRequest,
    ExtractionSchemaResponse,
    ExtractionSchemaVersionResponse,
    UpdateExtractionSchemaRequest,
)

from fastapi import Depends, Request

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _actor_info(actor: ActorContext) -> ActorInfo:
    actor_id = actor.actor_id or "anonymous"
    actor_type = actor.actor_type.value if actor.actor_type else "user"
    if actor_type not in ("user", "agent"):
        actor_type = "user"
    return ActorInfo(actor_id=actor_id, actor_type=actor_type)


# ---------------------------------------------------------------------------
# POST /extraction-schemas
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=SuccessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="추출 스키마 생성",
    description="DocumentType별 추출 대상 스키마를 생성한다. 동일 doc_type_code에 활성 스키마가 이미 있으면 409.",
)
def create_extraction_schema(
    request: Request,
    body: CreateExtractionSchemaRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    scope_profile_id: Optional[UUID] = None
    if body.scope_profile_id:
        try:
            scope_profile_id = UUID(body.scope_profile_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="scope_profile_id가 유효한 UUID가 아님")

    try:
        with get_db() as conn:
            repo = ExtractionSchemaRepository(conn)
            schema = repo.create(
                doc_type_code=body.doc_type_code,
                fields=body.fields,
                actor_info=_actor_info(actor),
                scope_profile_id=scope_profile_id,
                extra_metadata=body.extra_metadata,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("extraction_schema create failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.created",
        resource_type="extraction_schema",
        resource_id=str(schema.id),
        new_state={"doc_type_code": schema.doc_type_code, "version": schema.version},
    )

    return success_response(
        data=ExtractionSchemaResponse.from_domain(schema).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# GET /extraction-schemas/{doc_type}
# ---------------------------------------------------------------------------

@router.get(
    "/{doc_type}",
    response_model=SuccessResponse,
    summary="최신 추출 스키마 조회",
)
def get_extraction_schema(
    doc_type: str,
    include_deprecated: bool = Query(default=False),
    actor: ActorContext = Depends(resolve_current_actor),
):
    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        schema = repo.get_by_doc_type(doc_type, include_deprecated=include_deprecated)

    if not schema:
        raise HTTPException(status_code=404, detail=f"doc_type_code={doc_type!r}에 대한 추출 스키마 없음")

    return success_response(data=ExtractionSchemaResponse.from_domain(schema).model_dump())


# ---------------------------------------------------------------------------
# GET /extraction-schemas/{doc_type}/versions
# ---------------------------------------------------------------------------

@router.get(
    "/{doc_type}/versions",
    response_model=SuccessResponse,
    summary="버전 이력 조회",
)
def get_extraction_schema_versions(
    doc_type: str,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
):
    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        versions = repo.get_versions(doc_type, limit=limit, offset=offset)

    return list_response(
        data=[ExtractionSchemaVersionResponse.from_domain(v).model_dump() for v in versions],
        total=len(versions),
        page=(offset // limit) + 1,
        page_size=limit,
    )


# ---------------------------------------------------------------------------
# PUT /extraction-schemas/{doc_type}
# ---------------------------------------------------------------------------

@router.put(
    "/{doc_type}",
    response_model=SuccessResponse,
    summary="추출 스키마 업데이트 (새 버전 생성)",
)
def update_extraction_schema(
    request: Request,
    doc_type: str,
    body: UpdateExtractionSchemaRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    try:
        with get_db() as conn:
            repo = ExtractionSchemaRepository(conn)
            schema = repo.update(
                doc_type,
                fields=body.fields,
                actor_info=_actor_info(actor),
                change_summary=body.change_summary,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("extraction_schema update failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.updated",
        resource_type="extraction_schema",
        resource_id=str(schema.id),
        new_state={"doc_type_code": schema.doc_type_code, "version": schema.version},
    )

    return success_response(
        data=ExtractionSchemaResponse.from_domain(schema).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# DELETE /extraction-schemas/{doc_type}
# ---------------------------------------------------------------------------

@router.delete(
    "/{doc_type}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="추출 스키마 소프트 삭제",
)
def delete_extraction_schema(
    request: Request,
    doc_type: str,
    actor: ActorContext = Depends(resolve_current_actor),
):
    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        deleted = repo.delete(doc_type, actor_info=_actor_info(actor))

    if not deleted:
        raise HTTPException(status_code=404, detail=f"doc_type_code={doc_type!r}에 대한 추출 스키마 없음")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.deleted",
        resource_type="extraction_schema",
        resource_id=doc_type,
    )


# ---------------------------------------------------------------------------
# PATCH /extraction-schemas/{doc_type}/deprecate
# ---------------------------------------------------------------------------

@router.patch(
    "/{doc_type}/deprecate",
    response_model=SuccessResponse,
    summary="추출 스키마 폐기 표시",
)
def deprecate_extraction_schema(
    request: Request,
    doc_type: str,
    body: DeprecateExtractionSchemaRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    try:
        with get_db() as conn:
            repo = ExtractionSchemaRepository(conn)
            schema = repo.deprecate(doc_type, reason=body.reason, actor_info=_actor_info(actor))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("extraction_schema deprecate failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.deprecated",
        resource_type="extraction_schema",
        resource_id=str(schema.id),
        new_state={"deprecation_reason": body.reason},
    )

    return success_response(
        data=ExtractionSchemaResponse.from_domain(schema).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
    )
