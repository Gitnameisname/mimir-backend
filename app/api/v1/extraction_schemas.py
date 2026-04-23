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
import re
from typing import List, Optional
from uuid import UUID

import psycopg2.errors
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.repositories.extraction_schema_repository import (
    ActorInfo,
    ExtractionSchemaAlreadyExistsError,
    ExtractionSchemaNotFoundError,
    ExtractionSchemaRepository,
)
from app.schemas.extraction import (
    CreateExtractionSchemaRequest,
    DeprecateExtractionSchemaRequest,
    ExtractionSchemaResponse,
    ExtractionSchemaVersionResponse,
    RollbackExtractionSchemaRequest,
    UpdateExtractionSchemaRequest,
    compute_fields_diff,
)

from fastapi import Depends, Request

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# P7-2 상수: 에러 코드 + 경로 정규화
# ---------------------------------------------------------------------------
#
# P7-1 은 FK 위반 시 detail 에 "긴 한국어 문자열" 만 내려줬고, 프론트는 그
# 문자열을 regex 로 스캔해 분기했다. 이는 서버 메시지 변경 순간 프론트 UX 가
# 무너지는 약한 결합이라 P7-1 잔존 한계 §1 로 남겼다. P7-2 는 이를 해소하기
# 위해 structured error payload(`{code, message, hint}`) 를 병행 도입한다.
#
# 규약:
#   - `code`     : UPPER_SNAKE_CASE. 클라이언트가 분기 조건으로 사용.
#   - `message`  : 한국어 사용자 메시지. i18n 확장 지점.
#   - `hint`     : 선택. `{"href": "...", "label": "..."}` 형태로 해결 경로를
#                  클라이언트가 버튼으로 렌더할 수 있게 한다.
#
# 이 코드 레지스트리는 라우터 로컬로 둔다(모듈간 결합 최소). 새 코드는 여기에
# 상수로 추가하고 프론트의 `docTypeNormalize.ts` 에 동일 상수를 미러링한다.

ERR_DOC_TYPE_NOT_FOUND = "DOC_TYPE_NOT_FOUND"


# P7-2-b: 경로 파라미터 `{doc_type}` 용 regex. P7-1 에서 body 쪽은 Pydantic
# validator 가 대문자 정규화를 보장하지만, URL 로 소문자가 들어오는 경로는
# 정규화 훅이 없었다. `_normalize_doc_type_path` 가 모든 GET/PUT/DELETE 핸들러
# 서두에서 같은 규칙으로 정규화하고, regex 를 통과하지 못하면 422 를 낸다.
_DOC_TYPE_PATH_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")


def _normalize_doc_type_path(raw: str) -> str:
    """`{doc_type}` 경로 파라미터를 서버 저장값(대문자)과 일치시킨다.

    - 앞뒤 공백 제거 + 대문자 변환.
    - 정규화 후에도 regex (`^[A-Z][A-Z0-9_-]*$`) 에 맞지 않으면 422.
    - 빈 문자열 / 공백만 입력도 422.

    S2 ⑥ (ACL 필터) 는 repository 레벨에서 `scope_profile_id` 로 강제되므로
    본 헬퍼는 값 포맷만 담당한다. 반환값은 regex 검증을 통과한 안전한
    문자열이며 이후 `extra_metadata` 로그/메시지에 echo 해도 injection 위험이
    없다 (P7-1 보안보고서 C2 와 같은 논리).
    """
    if raw is None:
        raise HTTPException(
            status_code=422,
            detail="doc_type 경로 파라미터가 비어 있음",
        )
    value = raw.strip().upper()
    if not _DOC_TYPE_PATH_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=(
                f"doc_type (='{raw}') 가 형식에 맞지 않음. "
                "영문자로 시작해 영문/숫자/하이픈/언더스코어만 허용됨."
            ),
        )
    return value


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
# GET /extraction-schemas  — 전체 목록 (최신 버전만)
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=SuccessResponse,
    summary="추출 스키마 목록 조회",
    description="최신 버전의 추출 스키마 목록을 조회한다. 기본적으로 소프트 삭제된 스키마는 제외한다.",
)
def list_extraction_schemas(
    request: Request,
    is_deprecated: Optional[bool] = Query(default=None),
    include_deleted: bool = Query(default=False),
    scope_profile_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
):
    parsed_scope: Optional[UUID] = None
    if scope_profile_id:
        try:
            parsed_scope = UUID(scope_profile_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="scope_profile_id가 유효한 UUID가 아님")

    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        schemas = repo.list_all(
            is_deprecated=is_deprecated,
            scope_profile_id=parsed_scope,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )

    return list_response(
        data=[ExtractionSchemaResponse.from_domain(s).model_dump() for s in schemas],
        total=len(schemas),
        page=(offset // limit) + 1,
        page_size=limit,
        request_id=request.headers.get("X-Request-ID"),
    )


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
    except ExtractionSchemaAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except psycopg2.errors.ForeignKeyViolation:
        # P7-1-a / P7-2-a: extraction_schemas.doc_type_code -> document_types.type_code 참조.
        # document_types 에 해당 코드가 없으면 psycopg2 가 ForeignKeyViolation 을
        # 던진다. 이걸 그대로 500 으로 내리면 사용자에게 "어디를 고쳐야 할지"
        # 단서가 없음 — 422 로 바꾸고 해결 경로(/admin/document-types) 를 안내한다.
        #
        # P7-2-a: detail 을 structured payload 로 업그레이드. `code` 는 프론트
        #   의 "정확한" 라우팅 키가 되고, `message` 는 하위호환 (기존 P7-1
        #   프론트의 regex 매칭이 계속 성공하도록 P7-1 canonical 문자열 포맷을
        #   유지). `hint` 는 액션 버튼 렌더 메타데이터.
        # (get_db 컨텍스트매니저가 트랜잭션 rollback 을 보장하므로 후속 호출은 안전.)
        logger.info(
            "extraction_schema create blocked: doc_type_code=%r not in document_types",
            body.doc_type_code,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": ERR_DOC_TYPE_NOT_FOUND,
                "message": (
                    f"DocumentType '{body.doc_type_code}' 이(가) 존재하지 않습니다. "
                    f"먼저 /admin/document-types 에서 동일한 코드를 생성한 뒤 다시 시도하세요."
                ),
                "hint": {
                    "href": "/admin/document-types",
                    "label": "문서 유형 관리 열기",
                },
            },
        )
    except Exception:
        logger.exception("extraction_schema create failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.created",
        action="extraction_schema.create",
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
    scope_profile_id: Optional[str] = Query(default=None),
    actor: ActorContext = Depends(resolve_current_actor),
):
    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    parsed_scope: Optional[UUID] = None
    if scope_profile_id:
        try:
            parsed_scope = UUID(scope_profile_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="scope_profile_id가 유효한 UUID가 아님")

    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        schema = repo.get_by_doc_type(
            doc_type,
            include_deprecated=include_deprecated,
            scope_profile_id=parsed_scope,
        )

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
    scope_profile_id: Optional[str] = Query(default=None),
    actor: ActorContext = Depends(resolve_current_actor),
):
    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    parsed_scope: Optional[UUID] = None
    if scope_profile_id:
        try:
            parsed_scope = UUID(scope_profile_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="scope_profile_id가 유효한 UUID가 아님")

    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        versions = repo.get_versions(
            doc_type,
            limit=limit,
            offset=offset,
            scope_profile_id=parsed_scope,
        )

    return list_response(
        data=[ExtractionSchemaVersionResponse.from_domain(v).model_dump() for v in versions],
        total=len(versions),
        page=(offset // limit) + 1,
        page_size=limit,
    )


# ---------------------------------------------------------------------------
# GET /extraction-schemas/{doc_type}/versions/diff  (P4-A)
# ---------------------------------------------------------------------------

@router.get(
    "/{doc_type}/versions/diff",
    response_model=SuccessResponse,
    summary="두 버전 간 fields 차이 계산 (서버 정본)",
    description=(
        "base_version → target_version 방향의 필드 변화를 added/removed/modified 로 "
        "반환한다. 두 버전이 모두 같은 scope 에 속할 때만 조회 가능하다 (S2 ⑥)."
    ),
)
def diff_extraction_schema_versions(
    response: Response,
    doc_type: str,
    base_version: int = Query(..., ge=1, description="비교 기준(이전) 버전"),
    target_version: int = Query(..., ge=1, description="비교 대상(이후) 버전"),
    scope_profile_id: Optional[str] = Query(default=None),
    actor: ActorContext = Depends(resolve_current_actor),
):
    # P6-3: diff 결과는 특정 actor 의 scope 필터를 통과한 "사적" 자원이므로
    # 공유 캐시/로컬 캐시 모두 저장 금지. `SecurityHeadersMiddleware` 가 기본
    # `no-store` 를 걸지만, 라우트 레벨에서 `private` 을 명시해 의도를 코드
    # 자체에 남긴다 (defense in depth). `must-revalidate` 는 `no-store` 와
    # 함께 써도 의미가 없지만, 일부 구형 프록시는 `private, no-store` 만으로
    # 부족해 `no-cache` 도 필요하다는 사례가 있어 세 지시어를 나란히 둔다.
    response.headers["Cache-Control"] = "private, no-store, no-cache"
    response.headers["Pragma"] = "no-cache"

    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    if base_version == target_version:
        raise HTTPException(status_code=422, detail="base_version 과 target_version 이 같음")

    parsed_scope: Optional[UUID] = None
    if scope_profile_id:
        try:
            parsed_scope = UUID(scope_profile_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="scope_profile_id가 유효한 UUID가 아님")

    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        base_v = repo.get_version(doc_type, base_version, scope_profile_id=parsed_scope)
        if not base_v:
            raise HTTPException(
                status_code=404,
                detail=f"base_version={base_version} 버전을 찾을 수 없음",
            )
        target_v = repo.get_version(doc_type, target_version, scope_profile_id=parsed_scope)
        if not target_v:
            raise HTTPException(
                status_code=404,
                detail=f"target_version={target_version} 버전을 찾을 수 없음",
            )

    base_fields = {k: fd.model_dump(mode="json") for k, fd in base_v.fields.items()}
    target_fields = {k: fd.model_dump(mode="json") for k, fd in target_v.fields.items()}

    diff = compute_fields_diff(
        base_fields,
        target_fields,
        doc_type_code=doc_type,
        base_version=base_version,
        target_version=target_version,
    )
    return success_response(data=diff.model_dump())


# ---------------------------------------------------------------------------
# POST /extraction-schemas/{doc_type}/rollback  (P4-B)
# ---------------------------------------------------------------------------

@router.post(
    "/{doc_type}/rollback",
    response_model=SuccessResponse,
    summary="특정 버전의 fields 로 되돌리기 (새 버전 생성)",
    description=(
        "target_version 의 fields 를 복사해 새로운 버전을 생성한다. "
        "기존 버전 이력은 immutable 이므로 삭제되지 않는다. "
        "폐기된 스키마는 롤백 불가. scope_profile_id 지정 시 해당 scope 에만 적용 (S2 ⑥)."
    ),
)
def rollback_extraction_schema(
    request: Request,
    doc_type: str,
    body: RollbackExtractionSchemaRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    parsed_scope: Optional[UUID] = None
    if body.scope_profile_id:
        try:
            parsed_scope = UUID(body.scope_profile_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="scope_profile_id가 유효한 UUID가 아님")

    try:
        with get_db() as conn:
            repo = ExtractionSchemaRepository(conn)
            schema = repo.rollback_to_version(
                doc_type,
                target_version=body.target_version,
                actor_info=_actor_info(actor),
                change_summary=body.change_summary,
                scope_profile_id=parsed_scope,
            )
    except ExtractionSchemaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        # 폐기된 스키마 / target_version 범위 오류 / 빈 fields → 422
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception("extraction_schema rollback failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.rolled_back",
        action="extraction_schema.rollback",
        resource_type="extraction_schema",
        resource_id=str(schema.id),
        new_state={
            "doc_type_code": schema.doc_type_code,
            "version": schema.version,
            "rolled_back_from_version": body.target_version,
        },
    )

    return success_response(
        data=ExtractionSchemaResponse.from_domain(schema).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
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
    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    try:
        with get_db() as conn:
            repo = ExtractionSchemaRepository(conn)
            schema = repo.update(
                doc_type,
                fields=body.fields,
                actor_info=_actor_info(actor),
                change_summary=body.change_summary,
            )
    except ExtractionSchemaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        logger.exception("extraction_schema update failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.updated",
        action="extraction_schema.update",
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
    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    with get_db() as conn:
        repo = ExtractionSchemaRepository(conn)
        deleted = repo.delete(doc_type, actor_info=_actor_info(actor))

    if not deleted:
        raise HTTPException(status_code=404, detail=f"doc_type_code={doc_type!r}에 대한 추출 스키마 없음")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.deleted",
        action="extraction_schema.delete",
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
    doc_type = _normalize_doc_type_path(doc_type)  # P7-2-b

    try:
        with get_db() as conn:
            repo = ExtractionSchemaRepository(conn)
            schema = repo.deprecate(doc_type, reason=body.reason, actor_info=_actor_info(actor))
    except ExtractionSchemaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        logger.exception("extraction_schema deprecate failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_schema.deprecated",
        action="extraction_schema.deprecate",
        resource_type="extraction_schema",
        resource_id=str(schema.id),
        new_state={"deprecation_reason": body.reason},
    )

    return success_response(
        data=ExtractionSchemaResponse.from_domain(schema).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
    )
