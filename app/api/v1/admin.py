"""
Admin router — /api/v1/admin

관리자 전용 API. Phase 7 구현 (P0 쓰기 API 추가).

인증: resolve_current_actor() + RBAC (admin.read / admin.write)
     GET 요청 → ORG_ADMIN 이상, 쓰기 요청 → SUPER_ADMIN 전용.

엔드포인트 (GET):
  - GET /admin/dashboard/metrics|health|errors|recent-audit-logs
  - GET /admin/users, /admin/users/{user_id}
  - GET /admin/organizations, /admin/organizations/{org_id}
  - GET /admin/roles, /admin/roles/{role_id}
  - GET /admin/audit-logs, /admin/audit-logs/{event_id}
  - GET /admin/document-types, /admin/document-types/{type_code}
  - GET /admin/jobs, /admin/jobs/summary, /admin/jobs/{job_id}
  - GET /admin/indexing/jobs, /admin/indexing/summary
  - GET /admin/api-keys

엔드포인트 (WRITE — P0 추가):
  - POST   /admin/users
  - PATCH  /admin/users/{user_id}
  - DELETE /admin/users/{user_id}
  - POST   /admin/users/{user_id}/org-roles
  - DELETE /admin/users/{user_id}/org-roles/{org_id}
  - POST   /admin/organizations
  - PATCH  /admin/organizations/{org_id}
  - DELETE /admin/organizations/{org_id}
  - POST   /admin/roles
  - PATCH  /admin/roles/{role_id}
  - DELETE /admin/roles/{role_id}
  - POST   /admin/document-types
  - PATCH  /admin/document-types/{type_code}
  - DELETE /admin/document-types/{type_code}
"""

import hashlib
import logging
import re
import secrets as _secrets
from typing import Any, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.api.auth import ResourceRef, authorization_service, get_permission_matrix, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import list_response, success_response
from app.db import get_db
from app.repositories.users_repository import (
    organizations_repository,
    roles_repository,
    users_repository,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Admin 인증 의존성 — RBAC 기반
# ---------------------------------------------------------------------------

def require_admin_access(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    """RBAC 기반 관리자 접근 권한 확인.

    - GET  요청 → admin.read  (ORG_ADMIN, SUPER_ADMIN)
    - 쓰기 요청 → admin.write (SUPER_ADMIN)

    require_authenticated=True: 미인증 요청은 401, 권한 없으면 403.
    """
    action = (
        "admin.write"
        if request.method in ("POST", "PATCH", "DELETE", "PUT")
        else "admin.read"
    )
    authorization_service.authorize(
        actor=actor,
        action=action,
        resource=ResourceRef(resource_type="admin"),
        require_authenticated=True,
    )


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------

@router.get("/dashboard/metrics", summary="핵심 지표 카드")
def get_dashboard_metrics(_=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            # 사용자 지표
            cur.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status='ACTIVE') AS active FROM users")
            user_row = cur.fetchone()

            # 문서 지표
            cur.execute("SELECT COUNT(*) AS total FROM documents WHERE status != 'archived'")
            doc_row = cur.fetchone()

            # 백그라운드 작업 지표
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status IN ('PENDING','RUNNING')) AS running,
                    COUNT(*) FILTER (WHERE status = 'FAILED') AS failed
                FROM background_jobs
                WHERE created_at > NOW() - INTERVAL '24 hours'
            """)
            job_row = cur.fetchone()

            # 감사 이벤트 지표 (24시간)
            cur.execute("""
                SELECT COUNT(*) AS total FROM audit_events
                WHERE occurred_at > NOW() - INTERVAL '24 hours'
            """)
            audit_row = cur.fetchone()

            # Phase 10: 벡터화 지표
            vec_row = None
            try:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE is_current = TRUE) AS current_chunks,
                        COUNT(*) FILTER (WHERE is_current = TRUE AND embedding IS NOT NULL) AS embedded_chunks,
                        COUNT(*) FILTER (WHERE is_current = TRUE AND embedding IS NULL) AS pending_chunks,
                        COUNT(DISTINCT document_id) FILTER (WHERE is_current = TRUE) AS vectorized_docs
                    FROM document_chunks
                """)
                vec_row = cur.fetchone()
            except Exception as _vec_exc:
                logger.debug("벡터화 지표 조회 스킵 (테이블 미존재 또는 오류): %s", _vec_exc)

    return success_response(data={
        "users": {
            "total": user_row["total"] if user_row else 0,
            "active": user_row["active"] if user_row else 0,
        },
        "documents": {
            "total": doc_row["total"] if doc_row else 0,
        },
        "jobs": {
            "running": job_row["running"] if job_row else 0,
            "failed": job_row["failed"] if job_row else 0,
        },
        "audit_events_24h": audit_row["total"] if audit_row else 0,
        "vectorization": {
            "current_chunks": vec_row["current_chunks"] if vec_row else 0,
            "embedded_chunks": vec_row["embedded_chunks"] if vec_row else 0,
            "pending_chunks": vec_row["pending_chunks"] if vec_row else 0,
            "vectorized_docs": vec_row["vectorized_docs"] if vec_row else 0,
        },
    })


@router.get("/dashboard/health", summary="컴포넌트 상태")
def get_dashboard_health(_=Depends(require_admin_access)):
    # DB 연결 확인
    db_status = "HEALTHY"
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception:
        db_status = "DOWN"

    return success_response(data={
        "components": [
            {"name": "API Server", "status": "HEALTHY"},
            {"name": "Database", "status": db_status},
            {"name": "Background Job Queue", "status": "UNKNOWN"},
            {"name": "Indexing Pipeline", "status": "UNKNOWN"},
        ]
    })


@router.get("/dashboard/errors", summary="최근 오류 요약")
def get_dashboard_errors(
    limit: int = Query(default=10, ge=1, le=50),
    _=Depends(require_admin_access),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, job_type AS error_type, resource_type, resource_name,
                       status, error_code, created_at
                FROM background_jobs
                WHERE status = 'FAILED'
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "error_type": r["error_type"],
            "resource_type": r["resource_type"],
            "resource_name": r["resource_name"],
            "status": r["status"],
            "error_code": r["error_code"],
            "occurred_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return success_response(data=items)


@router.get("/dashboard/recent-audit-logs", summary="최근 감사 이벤트 요약")
def get_dashboard_recent_audit_logs(
    limit: int = Query(default=10, ge=1, le=50),
    _=Depends(require_admin_access),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, event_type, occurred_at, actor_user_id, actor_role,
                       action_result, document_id
                FROM audit_events
                ORDER BY occurred_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "event_type": r["event_type"],
            "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            "actor_id": r["actor_user_id"],
            "actor_role": r["actor_role"],
            "result": r["action_result"],
            "severity": _audit_severity(r["event_type"]),
        }
        for r in rows
    ]
    return success_response(data=items)


# ---------------------------------------------------------------------------
# 사용자 관리
# ---------------------------------------------------------------------------

@router.get("/users", summary="사용자 목록")
def list_users(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    offset = (page - 1) * limit
    conditions = []
    params: list = []

    if search:
        conditions.append("(display_name ILIKE %s OR email ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    if status:
        conditions.append("status = %s")
        params.append(status)
    if role:
        conditions.append("role_name = %s")
        params.append(role)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM users {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT id, email, display_name, status, role_name,
                       last_login_at, created_at
                FROM users {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "email": r["email"],
            "display_name": r["display_name"],
            "status": r["status"],
            "role_name": r["role_name"],
            "last_login_at": r["last_login_at"].isoformat() if r["last_login_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return list_response(data=items, page=page, page_size=limit, total=total)


@router.get("/users/{user_id}", summary="사용자 상세")
def get_user(user_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

            # 조직-역할 매핑
            cur.execute(
                """
                SELECT uor.id, uor.role_name, uor.created_at,
                       o.id AS org_id, o.name AS org_name
                FROM user_org_roles uor
                JOIN organizations o ON o.id = uor.org_id
                WHERE uor.user_id = %s
                """,
                (user_id,),
            )
            mappings = cur.fetchall()

            # 최근 감사 이벤트
            cur.execute(
                """
                SELECT id, event_type, occurred_at, action_result
                FROM audit_events
                WHERE actor_user_id = %s
                ORDER BY occurred_at DESC
                LIMIT 5
                """,
                (user_id,),
            )
            audit_rows = cur.fetchall()

    return success_response(data={
        "id": str(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "status": row["status"],
        "role_name": row["role_name"],
        "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "org_roles": [
            {
                "mapping_id": str(m["id"]),
                "org_id": str(m["org_id"]),
                "org_name": m["org_name"],
                "role_name": m["role_name"],
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            }
            for m in mappings
        ],
        "recent_audit_events": [
            {
                "id": str(a["id"]),
                "event_type": a["event_type"],
                "occurred_at": a["occurred_at"].isoformat() if a["occurred_at"] else None,
                "result": a["action_result"],
            }
            for a in audit_rows
        ],
    })


# ---------------------------------------------------------------------------
# 조직 관리
# ---------------------------------------------------------------------------

@router.get("/organizations", summary="조직 목록")
def list_organizations(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    offset = (page - 1) * limit
    conditions = []
    params: list = []
    if search:
        conditions.append("name ILIKE %s")
        params.append(f"%{search}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM organizations {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT o.id, o.name, o.description, o.status, o.created_at,
                       COUNT(uor.user_id) AS user_count
                FROM organizations o
                LEFT JOIN user_org_roles uor ON uor.org_id = o.id
                {where}
                GROUP BY o.id
                ORDER BY o.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "description": r["description"],
            "status": r["status"],
            "user_count": r["user_count"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return list_response(data=items, page=page, page_size=limit, total=total)


@router.get("/organizations/{org_id}", summary="조직 상세")
def get_organization(org_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM organizations WHERE id = %s", (org_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다.")

            cur.execute(
                """
                SELECT uor.id, uor.role_name, uor.created_at,
                       u.id AS user_id, u.display_name, u.email, u.status AS user_status
                FROM user_org_roles uor
                JOIN users u ON u.id = uor.user_id
                WHERE uor.org_id = %s
                ORDER BY uor.created_at
                """,
                (org_id,),
            )
            members = cur.fetchall()

    return success_response(data={
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "members": [
            {
                "mapping_id": str(m["id"]),
                "user_id": str(m["user_id"]),
                "display_name": m["display_name"],
                "email": m["email"],
                "status": m["user_status"],
                "role_name": m["role_name"],
                "joined_at": m["created_at"].isoformat() if m["created_at"] else None,
            }
            for m in members
        ],
    })


# ---------------------------------------------------------------------------
# 역할 관리
# ---------------------------------------------------------------------------

@router.get("/roles", summary="역할 목록")
def list_roles(_=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id, r.name, r.description, r.is_system, r.created_at,
                       COUNT(u.id) AS user_count
                FROM roles r
                LEFT JOIN users u ON u.role_name = r.name
                GROUP BY r.id
                ORDER BY r.is_system DESC, r.name
            """)
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "description": r["description"],
            "is_system": r["is_system"],
            "user_count": r["user_count"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return list_response(data=items, total=len(items))


@router.get("/roles/permissions/matrix", summary="역할-권한 매트릭스")
def get_role_permission_matrix(_=Depends(require_admin_access)):
    """모든 action에 대한 역할별 허용 여부 매트릭스를 반환한다.

    UI의 '권한 매트릭스' 뷰에서 사용하며,
    `_PERMISSION_MATRIX`(authorization.py)를 JSON-safe하게 직렬화한다.
    """
    matrix = get_permission_matrix()
    roles = ["VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"]
    # action별로 그룹화 (예: "document.read" → group="document", action="read")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for action, allowed in sorted(matrix.items()):
        if "." in action:
            group, verb = action.split(".", 1)
        else:
            group, verb = "other", action
        grouped.setdefault(group, []).append({
            "action": action,
            "verb": verb,
            "allowed_roles": allowed,
        })
    return success_response(data={
        "roles": roles,
        "groups": [{"name": g, "items": items} for g, items in grouped.items()],
    })


@router.get("/roles/{role_id}", summary="역할 상세")
def get_role(role_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM roles WHERE id = %s", (role_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="역할을 찾을 수 없습니다.")

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role_name = %s",
                (row["name"],),
            )
            user_count = cur.fetchone()["cnt"]

    return success_response(data={
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "is_system": row["is_system"],
        "user_count": user_count,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    })


# ---------------------------------------------------------------------------
# 감사 로그
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?Z?$")


def _validate_date_param(value: Optional[str], param_name: str) -> None:
    """VULN-016: date 파라미터가 ISO 8601 형식인지 검증한다."""
    if value and not _ISO_DATE_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format for '{param_name}'. Use ISO 8601 (e.g. 2024-01-01 or 2024-01-01T00:00:00).",
        )


@router.get("/audit-logs", summary="감사 로그 목록")
def list_audit_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    from_dt: Optional[str] = Query(default=None, alias="from"),
    to_dt: Optional[str] = Query(default=None, alias="to"),
    actor_id: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    result: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    _validate_date_param(from_dt, "from")
    _validate_date_param(to_dt, "to")

    offset = (page - 1) * limit
    conditions = []
    params: list = []

    if from_dt:
        conditions.append("occurred_at >= %s")
        params.append(from_dt)
    if to_dt:
        conditions.append("occurred_at <= %s")
        params.append(to_dt)
    if actor_id:
        conditions.append("actor_user_id = %s")
        params.append(actor_id)
    if event_type:
        conditions.append("event_type = %s")
        params.append(event_type)
    if result:
        conditions.append("action_result = %s")
        params.append(result)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM audit_events {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT id, event_type, occurred_at, actor_user_id, actor_role,
                       document_id, version_id, previous_state, new_state,
                       action_result, reason, request_id
                FROM audit_events {where}
                ORDER BY occurred_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "event_type": r["event_type"],
            "severity": _audit_severity(r["event_type"]),
            "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            "actor_id": r["actor_user_id"],
            "actor_role": r["actor_role"],
            "resource_type": "Document" if r["document_id"] else None,
            "resource_id": str(r["document_id"]) if r["document_id"] else None,
            "result": r["action_result"],
            "before_state": r["previous_state"],
            "after_state": r["new_state"],
            "reason": r["reason"],
        }
        for r in rows
    ]
    return list_response(data=items, page=page, page_size=limit, total=total)


@router.get("/audit-logs/{event_id}", summary="감사 로그 상세")
def get_audit_log(event_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM audit_events WHERE id = %s", (event_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="감사 이벤트를 찾을 수 없습니다.")

    return success_response(data={
        "id": str(row["id"]),
        "event_type": row["event_type"],
        "severity": _audit_severity(row["event_type"]),
        "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
        "actor_id": row["actor_user_id"],
        "actor_role": row["actor_role"],
        "resource_type": "Document" if row["document_id"] else None,
        "resource_id": str(row["document_id"]) if row["document_id"] else None,
        "version_id": str(row["version_id"]) if row["version_id"] else None,
        "result": row["action_result"],
        "before_state": row["previous_state"],
        "after_state": row["new_state"],
        "reason": row["reason"],
        "request_id": row["request_id"],
    })


# ---------------------------------------------------------------------------
# DocumentType 관리
# ---------------------------------------------------------------------------

@router.get("/document-types", summary="DocumentType 목록")
def list_document_types(
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    from app.plugins.base import DocumentTypeRegistry
    registry = DocumentTypeRegistry.instance()
    builtin_types = set(registry.list_type_names())

    conditions = []
    params: list = []
    if status:
        conditions.append("dt.status = %s")
        params.append(status)
    if search:
        conditions.append("(dt.type_code ILIKE %s OR dt.display_name ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT dt.type_code, dt.display_name, dt.description, dt.status,
                       dt.created_at, dt.updated_at,
                       jsonb_array_length(dt.schema_fields) AS field_count,
                       COUNT(d.id) AS document_count
                FROM document_types dt
                LEFT JOIN documents d ON d.document_type = dt.type_code
                {where}
                GROUP BY dt.type_code
                ORDER BY dt.type_code
                """,
                params,
            )
            rows = cur.fetchall()

    # Phase 12: 내장 플러그인 타입은 DB에 없어도 목록에 포함
    db_type_codes = {r["type_code"] for r in rows}
    extra_items = []
    for type_name in builtin_types:
        if type_name not in db_type_codes:
            p = registry.get(type_name)
            extra_items.append({
                "type_code": type_name,
                "display_name": p.get_display_name(),
                "description": p.get_description(),
                "status": "ACTIVE",
                "schema_field_count": 0,
                "document_count": 0,
                "is_builtin": True,
                "plugin_registered": True,
                "created_at": None,
            })

    items = [
        {
            "type_code": r["type_code"],
            "display_name": r["display_name"],
            "description": r["description"],
            "status": r["status"],
            "schema_field_count": r["field_count"] or 0,
            "document_count": r["document_count"],
            "is_builtin": r["type_code"] in builtin_types,
            "plugin_registered": r["type_code"] in builtin_types,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ] + extra_items

    items.sort(key=lambda x: x["type_code"])
    return list_response(data=items, total=len(items))


@router.get("/document-types/{type_code}", summary="DocumentType 상세")
def get_document_type(type_code: str, _=Depends(require_admin_access)):
    from app.plugins.base import DocumentTypeRegistry
    registry = DocumentTypeRegistry.instance()

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM document_types WHERE type_code = %s", (type_code,))
            row = cur.fetchone()

            # Phase 12: DB 레코드 없는 내장 플러그인 타입도 상세 반환
            if not row:
                if not registry.is_builtin(type_code):
                    raise HTTPException(status_code=404, detail="DocumentType을 찾을 수 없습니다.")
                plugin = registry.get(type_code)
                return success_response(data={
                    "type_code": type_code,
                    "display_name": plugin.get_display_name(),
                    "description": plugin.get_description(),
                    "status": "ACTIVE",
                    "schema_fields": [],
                    "plugin_config": {},
                    "document_count": 0,
                    "active_document_count": 0,
                    "created_at": None,
                    "updated_at": None,
                    "is_builtin": True,
                })

            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status != 'archived') AS active
                FROM documents WHERE document_type = %s
                """,
                (type_code,),
            )
            doc_counts = cur.fetchone()

    return success_response(data={
        "type_code": row["type_code"],
        "display_name": row["display_name"],
        "description": row["description"],
        "status": row["status"],
        "schema_fields": row["schema_fields"],
        "plugin_config": row["plugin_config"],
        "document_count": doc_counts["total"] if doc_counts else 0,
        "active_document_count": doc_counts["active"] if doc_counts else 0,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "is_builtin": registry.is_builtin(type_code),
    })


# ---------------------------------------------------------------------------
# 백그라운드 작업
# ---------------------------------------------------------------------------

@router.get("/jobs", summary="백그라운드 작업 목록")
def list_jobs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(default=None),
    job_type: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    offset = (page - 1) * limit
    conditions = []
    params: list = []
    if status:
        conditions.append("status = %s")
        params.append(status)
    if job_type:
        conditions.append("job_type = %s")
        params.append(job_type)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM background_jobs {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT id, job_type, resource_type, resource_id, resource_name,
                       status, progress, requester_id, requester_name,
                       error_code, started_at, ended_at, created_at
                FROM background_jobs {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    # 요약 통계
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                    COUNT(*) FILTER (WHERE status='RUNNING') AS running,
                    COUNT(*) FILTER (WHERE status='COMPLETED') AS completed,
                    COUNT(*) FILTER (WHERE status='FAILED') AS failed,
                    COUNT(*) FILTER (WHERE status='CANCELLED') AS cancelled
                FROM background_jobs
                WHERE created_at > NOW() - INTERVAL '24 hours'
            """)
            summary = cur.fetchone()

    items = [_format_job(r) for r in rows]
    return list_response(
        data=items,
        page=page,
        page_size=limit,
        total=total,
    )


@router.get("/jobs/summary", summary="작업 현황 요약")
def get_jobs_summary(_=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                    COUNT(*) FILTER (WHERE status='RUNNING') AS running,
                    COUNT(*) FILTER (WHERE status='COMPLETED') AS completed,
                    COUNT(*) FILTER (WHERE status='FAILED') AS failed,
                    COUNT(*) FILTER (WHERE status='CANCELLED') AS cancelled
                FROM background_jobs
                WHERE created_at > NOW() - INTERVAL '24 hours'
            """)
            row = cur.fetchone()
    return success_response(data=dict(row) if row else {})


@router.get("/jobs/{job_id}", summary="작업 상세")
def get_job(job_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM background_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return success_response(data=_format_job(row))


# ---------------------------------------------------------------------------
# 인덱싱 상태 (Phase 7 MVP: background_jobs 테이블 활용)
# ---------------------------------------------------------------------------

@router.get("/indexing/jobs", summary="인덱싱 작업 목록")
def list_indexing_jobs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    offset = (page - 1) * limit
    conditions = ["job_type LIKE 'INDEX%'"]
    params: list = []
    if status:
        conditions.append("status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM background_jobs {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT * FROM background_jobs {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [_format_job(r) for r in rows]
    return list_response(data=items, page=page, page_size=limit, total=total)


@router.get("/indexing/summary", summary="인덱싱 현황 요약")
def get_indexing_summary(_=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                    COUNT(*) FILTER (WHERE status='RUNNING') AS running,
                    COUNT(*) FILTER (WHERE status='COMPLETED') AS completed,
                    COUNT(*) FILTER (WHERE status='FAILED') AS failed,
                    COUNT(*) FILTER (WHERE status='SKIPPED') AS skipped
                FROM background_jobs
                WHERE job_type LIKE 'INDEX%'
            """)
            row = cur.fetchone()
    return success_response(data=dict(row) if row else {})


# ---------------------------------------------------------------------------
# API 키 관리
# ---------------------------------------------------------------------------

@router.get("/api-keys", summary="API 키 목록")
def list_api_keys(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    _=Depends(require_admin_access),
):
    offset = (page - 1) * limit
    conditions = []
    params: list = []
    if status:
        conditions.append("status = %s")
        params.append(status)
    if search:
        conditions.append("(name ILIKE %s OR issuer_name ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM api_keys {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT id, name, description, key_prefix, scope, status,
                       issuer_name, last_used_at, last_used_ip, use_count,
                       expires_at, created_at
                FROM api_keys {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "description": r["description"],
            "key_prefix": r["key_prefix"],
            "scope": r["scope"],
            "status": r["status"],
            "issuer_name": r["issuer_name"],
            "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            "last_used_ip": r["last_used_ip"],
            "use_count": r["use_count"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return list_response(data=items, page=page, page_size=limit, total=total)


# ---------------------------------------------------------------------------
# 내부 유틸
# ---------------------------------------------------------------------------

_CRITICAL_EVENTS = {
    "PERMISSION_CHANGED", "API_KEY_REVOKED", "DOCUMENT_TYPE_DEACTIVATED",
    "JOB_FORCE_STOPPED", "ADMIN_ACCOUNT_MODIFIED",
}
_HIGH_EVENTS = {
    "USER_DEACTIVATED", "USER_ROLE_CHANGED", "ROLE_CHANGED",
    "DOCUMENT_FORCE_DELETED", "API_KEY_ISSUED",
}


def _audit_severity(event_type: Optional[str]) -> str:
    if not event_type:
        return "NORMAL"
    if event_type in _CRITICAL_EVENTS:
        return "CRITICAL"
    if event_type in _HIGH_EVENTS:
        return "HIGH"
    return "NORMAL"


def _format_job(r) -> dict:
    started_at = r["started_at"].isoformat() if r.get("started_at") else None
    ended_at = r["ended_at"].isoformat() if r.get("ended_at") else None
    duration_sec = None
    if r.get("started_at") and r.get("ended_at"):
        duration_sec = int((r["ended_at"] - r["started_at"]).total_seconds())

    return {
        "id": str(r["id"]),
        "job_type": r["job_type"],
        "resource_type": r.get("resource_type"),
        "resource_id": r.get("resource_id"),
        "resource_name": r.get("resource_name"),
        "status": r["status"],
        "progress": r.get("progress", 0),
        "requester_id": r.get("requester_id"),
        "requester_name": r.get("requester_name"),
        "error_code": r.get("error_code"),
        "error_message": r.get("error_message"),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": duration_sec,
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
    }


# ===========================================================================
# P0 쓰기 API — 사용자 관리 (POST/PATCH/DELETE)
# ===========================================================================

_VALID_ROLES = {"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}
_VALID_STATUSES = {"ACTIVE", "INACTIVE", "SUSPENDED", "PENDING"}


class CreateUserBody(BaseModel):
    email: str
    display_name: str
    role_name: str = "VIEWER"
    status: str = "ACTIVE"


class UpdateUserBody(BaseModel):
    display_name: Optional[str] = None
    role_name: Optional[str] = None
    status: Optional[str] = None


@router.post("/users", summary="사용자 생성", status_code=201)
def create_user(body: CreateUserBody, _=Depends(require_admin_access)):
    if body.role_name not in _VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"유효하지 않은 역할입니다: {body.role_name}")
    if body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"유효하지 않은 상태입니다: {body.status}")

    with get_db() as conn:
        # 이메일 중복 확인
        existing = users_repository.get_by_email(conn, body.email)
        if existing:
            raise HTTPException(status_code=409, detail="이미 존재하는 이메일입니다.")
        user = users_repository.create(
            conn,
            email=body.email,
            display_name=body.display_name,
            role_name=body.role_name,
            status=body.status,
        )

    return success_response(data={
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role_name": user.role_name,
        "status": user.status,
        "created_at": user.created_at.isoformat(),
    })


@router.patch("/users/{user_id}", summary="사용자 수정")
def update_user(user_id: str, body: UpdateUserBody, _=Depends(require_admin_access)):
    if body.role_name and body.role_name not in _VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"유효하지 않은 역할입니다: {body.role_name}")
    if body.status and body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"유효하지 않은 상태입니다: {body.status}")

    with get_db() as conn:
        user = users_repository.update(
            conn, user_id,
            display_name=body.display_name,
            role_name=body.role_name,
            status=body.status,
        )
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    return success_response(data={
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role_name": user.role_name,
        "status": user.status,
        "updated_at": user.updated_at.isoformat(),
    })


@router.post("/users/{user_id}/activate", summary="대기 사용자 승인 활성화")
def activate_user(user_id: str, request: Request, actor: ActorContext = Depends(resolve_current_actor)):
    """PENDING 상태 사용자를 ACTIVE로 전환 (이메일 인증 비활성화 시 관리자 수동 승인)."""
    from app.audit.emitter import audit_emitter
    from app.api.context import get_request_ids
    req_id, _ = get_request_ids(request)

    with get_db() as conn:
        user = users_repository.get_by_id(conn, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
        if user.status != "PENDING":
            raise HTTPException(status_code=409, detail=f"대기 상태가 아닙니다. 현재 상태: {user.status}")
        updated = users_repository.update(conn, user_id, status="ACTIVE")

    audit_emitter.emit(
        event_type="user.activated",
        action="admin.activate_user",
        actor_id=actor.actor_id,
        resource_type="user",
        resource_id=user_id,
        result="success",
        request_id=req_id,
        metadata={"actor_type": actor.actor_type},
    )
    return success_response(data={
        "id": updated.id,
        "email": updated.email,
        "status": updated.status,
    })


@router.delete("/users/{user_id}", summary="사용자 삭제", status_code=204)
def delete_user(user_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        deleted = users_repository.delete(conn, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")


# --- 조직 역할 매핑 ---

class AssignOrgRoleBody(BaseModel):
    org_id: str
    role_name: str


@router.post("/users/{user_id}/org-roles", summary="사용자 조직 역할 부여", status_code=201)
def assign_user_org_role(user_id: str, body: AssignOrgRoleBody, _=Depends(require_admin_access)):
    if body.role_name not in _VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"유효하지 않은 역할입니다: {body.role_name}")

    with get_db() as conn:
        if not users_repository.get_by_id(conn, user_id):
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
        if not organizations_repository.get_by_id(conn, body.org_id):
            raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다.")
        mapping = users_repository.assign_org_role(
            conn, user_id=user_id, org_id=body.org_id, role_name=body.role_name,
        )

    return success_response(data={
        "id": mapping.id,
        "user_id": mapping.user_id,
        "org_id": mapping.org_id,
        "role_name": mapping.role_name,
        "created_at": mapping.created_at.isoformat(),
    })


@router.delete("/users/{user_id}/org-roles/{org_id}", summary="사용자 조직 역할 제거", status_code=204)
def remove_user_org_role(user_id: str, org_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        removed = users_repository.remove_org_role(conn, user_id=user_id, org_id=org_id)
    if not removed:
        raise HTTPException(status_code=404, detail="매핑을 찾을 수 없습니다.")


# ===========================================================================
# P0 쓰기 API — 조직 관리 (POST/PATCH/DELETE)
# ===========================================================================

class CreateOrganizationBody(BaseModel):
    name: str
    description: Optional[str] = None
    status: str = "ACTIVE"


class UpdateOrganizationBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


@router.post("/organizations", summary="조직 생성", status_code=201)
def create_organization(body: CreateOrganizationBody, _=Depends(require_admin_access)):
    with get_db() as conn:
        org = organizations_repository.create(
            conn, name=body.name, description=body.description, status=body.status,
        )

    return success_response(data={
        "id": org.id,
        "name": org.name,
        "description": org.description,
        "status": org.status,
        "created_at": org.created_at.isoformat(),
    })


@router.patch("/organizations/{org_id}", summary="조직 수정")
def update_organization(org_id: str, body: UpdateOrganizationBody, _=Depends(require_admin_access)):
    with get_db() as conn:
        org = organizations_repository.update(
            conn, org_id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    if not org:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다.")

    return success_response(data={
        "id": org.id,
        "name": org.name,
        "description": org.description,
        "status": org.status,
        "updated_at": org.updated_at.isoformat(),
    })


@router.delete("/organizations/{org_id}", summary="조직 삭제", status_code=204)
def delete_organization(org_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        deleted = organizations_repository.delete(conn, org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다.")


# ===========================================================================
# P0 쓰기 API — 역할 관리 (POST/PATCH/DELETE)
# ===========================================================================

class CreateRoleBody(BaseModel):
    name: str
    description: Optional[str] = None


class UpdateRoleBody(BaseModel):
    description: Optional[str] = None


@router.post("/roles", summary="역할 생성", status_code=201)
def create_role(body: CreateRoleBody, _=Depends(require_admin_access)):
    with get_db() as conn:
        existing = roles_repository.get_by_name(conn, body.name)
        if existing:
            raise HTTPException(status_code=409, detail="이미 존재하는 역할명입니다.")
        role = roles_repository.create(conn, name=body.name, description=body.description)

    return success_response(data={
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "is_system": role.is_system,
        "created_at": role.created_at.isoformat(),
    })


@router.patch("/roles/{role_id}", summary="역할 수정 (시스템 역할 제외)")
def update_role(role_id: str, body: UpdateRoleBody, _=Depends(require_admin_access)):
    with get_db() as conn:
        role = roles_repository.update(conn, role_id, description=body.description)
    if not role:
        raise HTTPException(status_code=404, detail="역할을 찾을 수 없거나 시스템 역할은 수정할 수 없습니다.")

    return success_response(data={
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "is_system": role.is_system,
    })


@router.delete("/roles/{role_id}", summary="역할 삭제 (시스템 역할 제외)", status_code=204)
def delete_role(role_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        deleted = roles_repository.delete(conn, role_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="역할을 찾을 수 없거나 시스템 역할은 삭제할 수 없습니다.")


# ===========================================================================
# P0 쓰기 API — DocumentType 관리 (POST/PATCH/DELETE)
# ===========================================================================

class CreateDocumentTypeBody(BaseModel):
    type_code: str
    display_name: str
    description: Optional[str] = None
    schema_fields: list[dict[str, Any]] = []
    plugin_config: dict[str, Any] = {}


class UpdateDocumentTypeBody(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    schema_fields: Optional[list[dict[str, Any]]] = None
    plugin_config: Optional[dict[str, Any]] = None
    retrieval_config: Optional[dict[str, Any]] = None  # S2 FG2.2: Retriever/Reranker 설정


@router.post("/document-types", summary="DocumentType 생성", status_code=201)
def create_document_type(body: CreateDocumentTypeBody, _=Depends(require_admin_access)):
    import json

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM document_types WHERE type_code = %s", (body.type_code,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="이미 존재하는 type_code입니다.")

            cur.execute(
                """
                INSERT INTO document_types (type_code, display_name, description, schema_fields, plugin_config)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                RETURNING *
                """,
                (
                    body.type_code,
                    body.display_name,
                    body.description,
                    json.dumps(body.schema_fields),
                    json.dumps(body.plugin_config),
                ),
            )
            row = cur.fetchone()

    return success_response(data={
        "type_code": row["type_code"],
        "display_name": row["display_name"],
        "description": row["description"],
        "status": row["status"],
        "schema_fields": row["schema_fields"],
        "plugin_config": row["plugin_config"],
        "created_at": row["created_at"].isoformat(),
    })


@router.patch("/document-types/{type_code}", summary="DocumentType 수정")
def update_document_type(
    type_code: str,
    body: UpdateDocumentTypeBody,
    _=Depends(require_admin_access),
):
    import json

    fields: list[str] = []
    params: list[Any] = []

    if body.display_name is not None:
        fields.append("display_name = %s")
        params.append(body.display_name)
    if body.description is not None:
        fields.append("description = %s")
        params.append(body.description)
    if body.status is not None:
        if body.status not in {"ACTIVE", "INACTIVE"}:
            raise HTTPException(status_code=422, detail="status는 ACTIVE 또는 INACTIVE이어야 합니다.")
        fields.append("status = %s")
        params.append(body.status)
    if body.schema_fields is not None:
        fields.append("schema_fields = %s::jsonb")
        params.append(json.dumps(body.schema_fields))
    if body.plugin_config is not None:
        fields.append("plugin_config = %s::jsonb")
        params.append(json.dumps(body.plugin_config))
    if body.retrieval_config is not None:
        from app.schemas.retrieval_config import RetrievalConfig
        try:
            validated = RetrievalConfig.model_validate(body.retrieval_config)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"retrieval_config가 유효하지 않습니다: {exc}")
        fields.append("retrieval_config = %s::jsonb")
        params.append(json.dumps(validated.model_dump()))

    if not fields:
        raise HTTPException(status_code=422, detail="수정할 필드가 없습니다.")

    fields.append("updated_at = NOW()")
    params.append(type_code)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE document_types SET {', '.join(fields)} WHERE type_code = %s RETURNING *",
                params,
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="DocumentType을 찾을 수 없습니다.")

    return success_response(data={
        "type_code": row["type_code"],
        "display_name": row["display_name"],
        "description": row["description"],
        "status": row["status"],
        "schema_fields": row["schema_fields"],
        "plugin_config": row["plugin_config"],
        "retrieval_config": row["retrieval_config"],
        "updated_at": row["updated_at"].isoformat(),
    })


@router.delete("/document-types/{type_code}", summary="DocumentType 비활성화", status_code=204)
def deactivate_document_type(type_code: str, _=Depends(require_admin_access)):
    """실제 삭제 대신 status를 INACTIVE로 변경한다 (참조 무결성 보호)."""
    # Phase 12: 내장 플러그인 타입은 삭제 불가
    from app.plugins.base import DocumentTypeRegistry
    if DocumentTypeRegistry.instance().is_builtin(type_code):
        raise HTTPException(
            status_code=422,
            detail=f"'{type_code}'는 내장 플러그인 타입으로 삭제할 수 없습니다."
        )
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_types SET status = 'INACTIVE', updated_at = NOW() WHERE type_code = %s",
                (type_code,),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="DocumentType을 찾을 수 없습니다.")


# ===========================================================================
# Phase 12 — DocumentType 플러그인 설정 관리 API
# ===========================================================================

class UpdateDocTypePluginConfigBody(BaseModel):
    """플러그인 설정 업데이트 요청 본문."""
    chunking_config: Optional[dict[str, Any]] = None
    rag_config: Optional[dict[str, Any]] = None
    search_config: Optional[dict[str, Any]] = None
    metadata_schema: Optional[dict[str, Any]] = None
    editor_config: Optional[dict[str, Any]] = None
    renderer_config: Optional[dict[str, Any]] = None
    workflow_config: Optional[dict[str, Any]] = None


@router.get("/document-types/{type_code}/plugin", summary="플러그인 현황 조회")
def get_document_type_plugin(type_code: str, _=Depends(require_admin_access)):
    """DocumentType 플러그인 현황과 유효 설정을 반환한다."""
    # P12-SEC-05: type_code 형식 검증
    _validate_type_code_format(type_code)

    from app.plugins.base import DocumentTypeRegistry
    registry = DocumentTypeRegistry.instance()
    is_builtin = registry.is_builtin(type_code)

    # 플러그인 기본값 조회
    plugin = registry.get(type_code)
    chunking_cfg = plugin.chunking_plugin().get_config()
    ctx_cfg = plugin.rag_plugin().get_context_config()
    search_boost = plugin.search_plugin().get_boost_config()
    metadata_schema = plugin.metadata_schema_plugin().get_schema()
    metadata_ui_schema = plugin.metadata_schema_plugin().get_ui_schema()
    workflow = {
        "requires_approval": plugin.workflow_plugin().requires_approval(),
        "review_roles": plugin.workflow_plugin().get_review_roles(),
    }
    editor = {
        "allowed_node_types": plugin.editor_plugin().get_allowed_node_types(),
        "default_structure": plugin.editor_plugin().get_default_structure(),
    }

    # DB 오버라이드 설정 조회
    db_override: dict = {}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT plugin_config FROM document_types WHERE type_code = %s",
                (type_code,),
            )
            row = cur.fetchone()
            if row and row.get("plugin_config"):
                db_override = row["plugin_config"]

    return success_response(data={
        "type_code": type_code,
        "is_builtin": is_builtin,
        "effective_config": {
            "chunking": {
                "strategy": chunking_cfg.strategy,
                "max_chunk_tokens": chunking_cfg.max_chunk_tokens,
                "min_chunk_tokens": chunking_cfg.min_chunk_tokens,
                "overlap_tokens": chunking_cfg.overlap_tokens,
                "include_parent_context": chunking_cfg.include_parent_context,
                "parent_context_depth": chunking_cfg.parent_context_depth,
                "index_version_policy": chunking_cfg.index_version_policy,
                "exclude_node_types": chunking_cfg.exclude_node_types,
                "merge_strategy": chunking_cfg.merge_strategy,
            },
            "rag": ctx_cfg,
            "search_boost": search_boost,
            "metadata_schema": metadata_schema,
            "metadata_ui_schema": metadata_ui_schema,
            "workflow": workflow,
            "editor": editor,
        },
        "db_override": db_override,
        "display_name": plugin.get_display_name(),
        "description": plugin.get_description(),
    })


_TYPE_CODE_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]*$')


def _validate_type_code_format(type_code: str) -> None:
    """P12-SEC-05: type_code 경로 파라미터 형식 검증."""
    if not _TYPE_CODE_PATTERN.match(type_code):
        raise HTTPException(
            status_code=422,
            detail="type_code는 영문 대문자, 숫자, 밑줄만 허용됩니다."
        )


def _validate_chunking_config(cfg: dict) -> None:
    """P12-SEC-02: chunking_config 숫자 범위 검증."""
    max_tokens = cfg.get("max_chunk_tokens")
    min_tokens = cfg.get("min_chunk_tokens")
    overlap = cfg.get("overlap_tokens")
    depth = cfg.get("parent_context_depth")

    errors = []
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            errors.append("max_chunk_tokens는 1 이상의 정수여야 합니다.")
        elif max_tokens > 32768:
            errors.append("max_chunk_tokens는 32768 이하여야 합니다.")
    if min_tokens is not None:
        if not isinstance(min_tokens, int) or min_tokens < 0:
            errors.append("min_chunk_tokens는 0 이상의 정수여야 합니다.")
    if overlap is not None:
        if not isinstance(overlap, int) or overlap < 0:
            errors.append("overlap_tokens는 0 이상의 정수여야 합니다.")
    if depth is not None:
        if not isinstance(depth, int) or depth < 0:
            errors.append("parent_context_depth는 0 이상의 정수여야 합니다.")
        elif depth > 10:
            errors.append("parent_context_depth는 10 이하여야 합니다.")
    if max_tokens and min_tokens and max_tokens <= min_tokens:
        errors.append("max_chunk_tokens는 min_chunk_tokens보다 커야 합니다.")
    if overlap and max_tokens and overlap >= max_tokens:
        errors.append("overlap_tokens는 max_chunk_tokens보다 작아야 합니다.")

    if errors:
        raise HTTPException(status_code=422, detail=" / ".join(errors))


@router.put("/document-types/{type_code}/plugin", summary="플러그인 설정 업데이트")
def update_document_type_plugin_config(
    type_code: str,
    body: UpdateDocTypePluginConfigBody,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    """DocumentType 플러그인 설정을 DB에 저장한다 (내장 타입은 오버라이드).

    변경 시 감사 이벤트를 기록한다.
    """
    import json as json_lib
    from app.audit.emitter import audit_emitter

    # P12-SEC-05: type_code 형식 검증
    _validate_type_code_format(type_code)

    changed_fields = []
    plugin_config_update: dict[str, Any] = {}

    if body.chunking_config is not None:
        # P12-SEC-02: 숫자 범위 검증
        _validate_chunking_config(body.chunking_config)
        plugin_config_update["chunking_config"] = body.chunking_config
        changed_fields.append("chunking_config")

    if body.rag_config is not None:
        plugin_config_update["rag_config"] = body.rag_config
        changed_fields.append("rag_config")

    if body.search_config is not None:
        plugin_config_update["search_config"] = body.search_config
        changed_fields.append("search_config")

    if body.metadata_schema is not None:
        # JSON Schema 유효성 검사 (P12-SEC-03: 예외 상세 미노출)
        try:
            import jsonschema
            jsonschema.Draft7Validator.check_schema(body.metadata_schema)
        except ImportError:
            pass  # jsonschema 미설치 시 검증 건너뜀
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="유효하지 않은 JSON Schema (Draft-07) 형식입니다. 스키마 구조를 확인하세요."
            )
        plugin_config_update["metadata_schema"] = body.metadata_schema
        changed_fields.append("metadata_schema")

    if body.editor_config is not None:
        plugin_config_update["editor_config"] = body.editor_config
        changed_fields.append("editor_config")

    if body.renderer_config is not None:
        plugin_config_update["renderer_config"] = body.renderer_config
        changed_fields.append("renderer_config")

    if body.workflow_config is not None:
        plugin_config_update["workflow_config"] = body.workflow_config
        changed_fields.append("workflow_config")

    if not changed_fields:
        raise HTTPException(status_code=422, detail="변경할 설정이 없습니다.")

    with get_db() as conn:
        with conn.cursor() as cur:
            # 기존 plugin_config 조회
            cur.execute(
                "SELECT plugin_config FROM document_types WHERE type_code = %s",
                (type_code,),
            )
            row = cur.fetchone()

            if row is None:
                # DB에 없는 경우 — 내장 플러그인의 경우 DB 레코드 생성
                from app.plugins.base import DocumentTypeRegistry
                plugin = DocumentTypeRegistry.instance().get(type_code)
                cur.execute(
                    """
                    INSERT INTO document_types (type_code, display_name, description, plugin_config)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (type_code) DO UPDATE
                      SET plugin_config = document_types.plugin_config || EXCLUDED.plugin_config,
                          updated_at = NOW()
                    """,
                    (
                        type_code,
                        plugin.get_display_name(),
                        plugin.get_description(),
                        json_lib.dumps(plugin_config_update),
                    ),
                )
            else:
                # 기존 plugin_config와 병합 (P12-SEC-04: COALESCE로 NULL 전파 방어)
                cur.execute(
                    """
                    UPDATE document_types
                    SET plugin_config = COALESCE(plugin_config, '{}') || %s::jsonb,
                        updated_at = NOW()
                    WHERE type_code = %s
                    """,
                    (json_lib.dumps(plugin_config_update), type_code),
                )

    # 감사 이벤트 기록
    try:
        audit_emitter.emit(
            event_type="DOCUMENT_TYPE_PLUGIN_CONFIG_CHANGED",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            metadata={
                "type_code": type_code,
                "changed_fields": changed_fields,
            },
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data={
        "type_code": type_code,
        "updated_fields": changed_fields,
    })


@router.get("/document-types/{type_code}/plugin/schema", summary="Metadata Schema 조회")
def get_metadata_schema(type_code: str, _=Depends(require_admin_access)):
    """타입의 metadata JSON Schema와 UI Schema를 반환한다."""
    from app.plugins.base import DocumentTypeRegistry
    plugin = DocumentTypeRegistry.instance().get(type_code)
    return success_response(data={
        "type_code": type_code,
        "schema": plugin.metadata_schema_plugin().get_schema(),
        "ui_schema": plugin.metadata_schema_plugin().get_ui_schema(),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Phase 14-11: 시스템 설정 관리 API
# ═══════════════════════════════════════════════════════════════════════════

# ---- 카테고리 한글 라벨 (UI 응답에 포함) ----
_SETTINGS_CATEGORY_LABELS = {
    "auth": "인증",
    "system": "시스템",
    "notification": "알림",
    "security": "보안",
}

# ---- 캐시 키 / TTL ----
_SETTINGS_CACHE_KEY_ALL = "system_settings:all:v1"
_SETTINGS_CACHE_TTL = 300  # 5 minutes


def _invalidate_settings_cache() -> None:
    """system_settings 캐시를 무효화한다 (변경 시 호출)."""
    try:
        from app.cache import get_valkey
        get_valkey().delete(_SETTINGS_CACHE_KEY_ALL)
    except Exception as exc:
        logger.debug("settings 캐시 무효화 실패 (무시): %s", exc)


def _setting_to_response(setting: dict[str, Any]) -> dict[str, Any]:
    """단건 응답 형식으로 변환 (id/category 제외, key/value/description/updated_at 포함)."""
    updated_at = setting.get("updated_at")
    return {
        "key": setting["key"],
        "value": setting["value"],
        "description": setting.get("description"),
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@router.get("/settings", summary="시스템 설정 전체 조회 (카테고리별 그룹)")
def get_all_settings(_=Depends(require_admin_access)):
    """시스템 설정을 카테고리별 그룹으로 반환한다.

    Valkey 캐시 5분 TTL 적용 (관리자 화면 새로고침 시 DB 부하 완화).
    """
    import json as json_lib

    # 캐시 조회
    try:
        from app.cache import get_valkey
        cached = get_valkey().get(_SETTINGS_CACHE_KEY_ALL)
        if cached:
            return success_response(data=json_lib.loads(cached))
    except Exception as exc:
        logger.debug("settings 캐시 조회 실패 (DB로 폴백): %s", exc)

    from app.repositories.settings_repository import settings_repository

    with get_db() as conn:
        rows = settings_repository.list_all(conn)

    # 카테고리별 그룹화
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(_setting_to_response(row))

    # 응답 구조: { categories: [{name, label, items: [...]}] }
    categories = []
    for cat in sorted(grouped.keys(), key=lambda c: list(_SETTINGS_CATEGORY_LABELS.keys()).index(c) if c in _SETTINGS_CATEGORY_LABELS else 999):
        categories.append({
            "name": cat,
            "label": _SETTINGS_CATEGORY_LABELS.get(cat, cat),
            "items": grouped[cat],
        })

    payload = {"categories": categories}

    # 캐시 저장
    try:
        from app.cache import get_valkey
        get_valkey().setex(
            _SETTINGS_CACHE_KEY_ALL,
            _SETTINGS_CACHE_TTL,
            json_lib.dumps(payload, default=str),
        )
    except Exception as exc:
        logger.debug("settings 캐시 저장 실패 (무시): %s", exc)

    return success_response(data=payload)


@router.get("/settings/{category}", summary="카테고리별 설정 조회")
def get_settings_by_category(category: str, _=Depends(require_admin_access)):
    """단일 카테고리의 설정 항목을 반환한다."""
    from app.repositories.settings_repository import settings_repository

    # 카테고리 형식 검증 (영문 소문자/숫자/언더스코어만)
    if not re.match(r"^[a-z][a-z0-9_]{0,99}$", category):
        raise HTTPException(status_code=422, detail="유효하지 않은 카테고리 형식입니다.")

    with get_db() as conn:
        rows = settings_repository.list_by_category(conn, category)

    if not rows:
        raise HTTPException(status_code=404, detail=f"카테고리 '{category}'를 찾을 수 없습니다.")

    return success_response(data={
        "category": category,
        "label": _SETTINGS_CATEGORY_LABELS.get(category, category),
        "items": [_setting_to_response(r) for r in rows],
    })


class UpdateSettingBody(BaseModel):
    value: Any  # JSONB이므로 임의 타입 허용 (단, 기존 값과 동일 타입이어야 함)


@router.patch("/settings/{category}/{key}", summary="설정 값 변경 (SUPER_ADMIN)")
def update_setting(
    category: str,
    key: str,
    body: UpdateSettingBody,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),  # PATCH → admin.write → SUPER_ADMIN 전용
):
    """설정 값을 변경한다.

    검증:
      - 카테고리/키 존재 확인 (404)
      - 새 값 타입이 기존 값 타입과 동일해야 함 (422)
      - 잠재적 위험 키(maintenance_mode 등)도 동일 검증으로 처리

    감사 이벤트: 이전 값 → 새 값 기록 (SETTING_CHANGED).
    """
    import json as json_lib
    from app.audit.emitter import audit_emitter
    from app.repositories.settings_repository import settings_repository

    # 카테고리/키 형식 검증 (SQL injection 방어 보강 — 파라미터 바인딩과 이중 안전망)
    if not re.match(r"^[a-z][a-z0-9_]{0,99}$", category):
        raise HTTPException(status_code=422, detail="유효하지 않은 카테고리 형식입니다.")
    if not re.match(r"^[a-z][a-z0-9_]{0,254}$", key):
        raise HTTPException(status_code=422, detail="유효하지 않은 키 형식입니다.")

    with get_db() as conn:
        existing = settings_repository.get_one(conn, category, key)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"설정 '{category}.{key}'를 찾을 수 없습니다.",
            )

        old_value = existing["value"]
        new_value = body.value

        # 타입 일치 검증 (bool은 int의 서브클래스이므로 별도 처리)
        def _type_signature(v: Any) -> str:
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, int):
                return "int"
            if isinstance(v, float):
                return "float"
            if isinstance(v, str):
                return "str"
            if isinstance(v, list):
                return "list"
            if isinstance(v, dict):
                return "dict"
            if v is None:
                return "null"
            return type(v).__name__

        old_type = _type_signature(old_value)
        new_type = _type_signature(new_value)
        if old_type != new_type:
            raise HTTPException(
                status_code=422,
                detail=f"값 타입이 일치하지 않습니다 (기존: {old_type}, 신규: {new_type}).",
            )

        # 변경 없음
        if old_value == new_value:
            return success_response(data=_setting_to_response(existing))

        # 업데이트
        updated = settings_repository.update_value(
            conn, category, key, new_value, actor.actor_id
        )
        if updated is None:
            # 동시성 — 타 트랜잭션이 삭제 (시드만 있는 환경에서는 발생 어려움)
            raise HTTPException(status_code=404, detail="업데이트 대상이 사라졌습니다.")

    # 캐시 무효화
    _invalidate_settings_cache()

    # 감사 이벤트 (실패해도 응답은 정상)
    try:
        audit_emitter.emit(
            event_type="SETTING_CHANGED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="system_setting",
            resource_id=updated["id"],
            result="success",
            metadata={
                "category": category,
                "key": key,
                "old_value": old_value,
                "new_value": new_value,
            },
            previous_state=json_lib.dumps(old_value, default=str)[:500],
            new_state=json_lib.dumps(new_value, default=str)[:500],
        )
    except Exception as exc:
        logger.warning("설정 변경 감사 이벤트 기록 실패: %s", exc)

    return success_response(data=_setting_to_response(updated))


# ═══════════════════════════════════════════════════════════════════════════
# Phase 14-12: 모니터링 메트릭 API
# ═══════════════════════════════════════════════════════════════════════════

# 지원 period → (interval_seconds, bucket_count)
_MONITORING_PERIODS = {
    "1h": (60, 60),       # 1분 버킷 × 60
    "6h": (600, 36),      # 10분 버킷 × 36
    "24h": (3600, 24),    # 1시간 버킷 × 24
    "7d": (21600, 28),    # 6시간 버킷 × 28
}


def _validate_period(period: str) -> tuple[int, int]:
    if period not in _MONITORING_PERIODS:
        raise HTTPException(
            status_code=422,
            detail=f"지원하지 않는 period입니다. (허용: {', '.join(_MONITORING_PERIODS.keys())})",
        )
    return _MONITORING_PERIODS[period]


@router.get("/monitoring/response-times", summary="API 응답 시간 추이 (P50/P95/P99)")
def get_response_time_trend(
    period: str = Query(default="24h"),
    _=Depends(require_admin_access),
):
    """background_jobs의 실행 시간(ms)을 버킷별 P50/P95/P99로 집계한다.

    완료된 (ended_at IS NOT NULL) 작업만 대상으로 한다.
    """
    interval_seconds, bucket_count = _validate_period(period)
    total_seconds = interval_seconds * bucket_count

    with get_db() as conn:
        with conn.cursor() as cur:
            # 시간 버킷 시리즈 생성 + LATERAL JOIN으로 percentile 계산
            cur.execute(
                """
                WITH series AS (
                  SELECT generate_series(
                    date_trunc('minute', NOW()) - make_interval(secs => %s),
                    date_trunc('minute', NOW()),
                    make_interval(secs => %s)
                  ) AS bucket_start
                ),
                samples AS (
                  SELECT
                    to_timestamp(
                      floor(extract(epoch FROM ended_at) / %s) * %s
                    ) AT TIME ZONE 'UTC' AS bucket,
                    EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000 AS duration_ms
                  FROM background_jobs
                  WHERE ended_at IS NOT NULL
                    AND started_at IS NOT NULL
                    AND ended_at > NOW() - make_interval(secs => %s)
                ),
                aggregated AS (
                  SELECT
                    bucket,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99,
                    COUNT(*) AS sample_count
                  FROM samples
                  GROUP BY bucket
                )
                SELECT
                  s.bucket_start AS timestamp,
                  COALESCE(a.p50, 0)::int AS p50,
                  COALESCE(a.p95, 0)::int AS p95,
                  COALESCE(a.p99, 0)::int AS p99,
                  COALESCE(a.sample_count, 0)::int AS sample_count
                FROM series s
                LEFT JOIN aggregated a
                  ON date_trunc('second', s.bucket_start) = date_trunc('second', a.bucket)
                ORDER BY s.bucket_start
                """,
                (
                    total_seconds, interval_seconds,
                    interval_seconds, interval_seconds,
                    total_seconds,
                ),
            )
            rows = cur.fetchall()

    data = [
        {
            "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
            "p50": int(r["p50"] or 0),
            "p95": int(r["p95"] or 0),
            "p99": int(r["p99"] or 0),
            "sample_count": int(r["sample_count"] or 0),
        }
        for r in rows
    ]

    return success_response(data={
        "period": period,
        "interval_seconds": interval_seconds,
        "data": data,
    })


@router.get("/monitoring/error-trends", summary="에러 추이 (4xx/5xx 분류)")
def get_error_trend(
    period: str = Query(default="24h"),
    _=Depends(require_admin_access),
):
    """audit_events.action_result + background_jobs.status='FAILED'를 시간대별로 집계한다.

    분류:
      - 4xx (client_error): action_result IN ('denied', 'conflict')
      - 5xx (server_error): action_result = 'failure' OR jobs.status = 'FAILED'
    """
    interval_seconds, bucket_count = _validate_period(period)
    total_seconds = interval_seconds * bucket_count

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH series AS (
                  SELECT generate_series(
                    date_trunc('minute', NOW()) - make_interval(secs => %s),
                    date_trunc('minute', NOW()),
                    make_interval(secs => %s)
                  ) AS bucket_start
                ),
                events AS (
                  SELECT
                    to_timestamp(floor(extract(epoch FROM occurred_at) / %s) * %s) AT TIME ZONE 'UTC' AS bucket,
                    SUM(CASE WHEN action_result IN ('denied','conflict') THEN 1 ELSE 0 END) AS c4xx,
                    SUM(CASE WHEN action_result = 'failure' THEN 1 ELSE 0 END) AS c5xx
                  FROM audit_events
                  WHERE occurred_at > NOW() - make_interval(secs => %s)
                  GROUP BY bucket
                ),
                jobs AS (
                  SELECT
                    to_timestamp(floor(extract(epoch FROM created_at) / %s) * %s) AT TIME ZONE 'UTC' AS bucket,
                    COUNT(*) AS jobs_failed
                  FROM background_jobs
                  WHERE status = 'FAILED'
                    AND created_at > NOW() - make_interval(secs => %s)
                  GROUP BY bucket
                )
                SELECT
                  s.bucket_start AS timestamp,
                  COALESCE(e.c4xx, 0)::int AS client_errors,
                  (COALESCE(e.c5xx, 0) + COALESCE(j.jobs_failed, 0))::int AS server_errors
                FROM series s
                LEFT JOIN events e ON date_trunc('second', s.bucket_start) = date_trunc('second', e.bucket)
                LEFT JOIN jobs j   ON date_trunc('second', s.bucket_start) = date_trunc('second', j.bucket)
                ORDER BY s.bucket_start
                """,
                (
                    total_seconds, interval_seconds,
                    interval_seconds, interval_seconds, total_seconds,
                    interval_seconds, interval_seconds, total_seconds,
                ),
            )
            rows = cur.fetchall()

    data = [
        {
            "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
            "client_errors": int(r["client_errors"] or 0),
            "server_errors": int(r["server_errors"] or 0),
        }
        for r in rows
    ]

    return success_response(data={
        "period": period,
        "interval_seconds": interval_seconds,
        "data": data,
    })


@router.get("/monitoring/components", summary="시스템 구성 요소 상세 상태")
def get_component_status(_=Depends(require_admin_access)):
    """DB / Valkey / Vector DB / Job Runner 상태와 응답 시간을 반환한다."""
    import time

    components = []

    # 1. PostgreSQL
    db_status, db_latency_ms = "DOWN", None
    db_meta: dict[str, Any] = {}
    try:
        t0 = time.perf_counter()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
                # 활성 연결 수
                cur.execute("SELECT count(*)::int AS c FROM pg_stat_activity WHERE state = 'active'")
                db_meta["active_connections"] = (cur.fetchone() or {}).get("c", 0)
        db_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        db_status = "HEALTHY"
    except Exception as exc:
        db_meta["error"] = str(exc)[:200]
    components.append({
        "name": "PostgreSQL",
        "status": db_status,
        "latency_ms": db_latency_ms,
        "metadata": db_meta,
    })

    # 2. Valkey
    valkey_status, valkey_latency_ms = "DOWN", None
    valkey_meta: dict[str, Any] = {}
    try:
        from app.cache import get_valkey
        t0 = time.perf_counter()
        client = get_valkey()
        if client.ping():
            valkey_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            valkey_status = "HEALTHY"
            try:
                info = client.info("memory")
                valkey_meta["used_memory_human"] = info.get("used_memory_human")
            except Exception as exc:
                logger.debug("Valkey memory info 조회 실패: %s", exc)
    except Exception as exc:
        valkey_meta["error"] = str(exc)[:200]
    components.append({
        "name": "Valkey",
        "status": valkey_status,
        "latency_ms": valkey_latency_ms,
        "metadata": valkey_meta,
    })

    # 3. Vector DB (pgvector — DB와 동일 인스턴스, 인덱스 행 수만 보고)
    vec_status, vec_latency_ms = "UNKNOWN", None
    vec_meta: dict[str, Any] = {}
    try:
        t0 = time.perf_counter()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE is_current = TRUE AND embedding IS NOT NULL)::int AS embedded_count
                    FROM document_chunks
                """)
                row = cur.fetchone()
                vec_meta["embedded_chunks"] = row["embedded_count"] if row else 0
        vec_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        vec_status = "HEALTHY"
    except Exception as exc:
        vec_meta["error"] = str(exc)[:200]
    components.append({
        "name": "Vector DB (pgvector)",
        "status": vec_status,
        "latency_ms": vec_latency_ms,
        "metadata": vec_meta,
    })

    # 4. Job Runner (background_jobs PENDING/RUNNING 카운트)
    job_status, job_latency_ms = "DOWN", None
    job_meta: dict[str, Any] = {}
    try:
        t0 = time.perf_counter()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE status IN ('PENDING','RUNNING'))::int AS pending,
                        COUNT(*) FILTER (WHERE status = 'FAILED' AND created_at > NOW() - INTERVAL '24 hours')::int AS failed_24h
                    FROM background_jobs
                """)
                row = cur.fetchone()
                if row:
                    job_meta["pending"] = row["pending"]
                    job_meta["failed_24h"] = row["failed_24h"]
        job_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        job_status = "HEALTHY"
    except Exception as exc:
        job_meta["error"] = str(exc)[:200]
    components.append({
        "name": "Job Runner",
        "status": job_status,
        "latency_ms": job_latency_ms,
        "metadata": job_meta,
    })

    return success_response(data={"components": components})


# ═══════════════════════════════════════════════════════════════════════════
# Phase 14-13: 알림 관리 API
# ═══════════════════════════════════════════════════════════════════════════

_ALERT_SEVERITIES = {"info", "warning", "critical"}
_ALERT_CHANNELS = {"email", "webhook"}
_ALERT_STATUSES = {"firing", "resolved"}
_ALERT_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "ne"}


class AlertConditionBody(BaseModel):
    operator: str
    threshold: float
    duration_seconds: Optional[int] = None


class CreateAlertRuleBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    metric_name: str = Field(..., min_length=1, max_length=255)
    condition: AlertConditionBody
    severity: str
    channels: list[str] = Field(default_factory=list)
    channel_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class UpdateAlertRuleBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    metric_name: Optional[str] = Field(None, min_length=1, max_length=255)
    condition: Optional[AlertConditionBody] = None
    severity: Optional[str] = None
    channels: Optional[list[str]] = None
    channel_config: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


def _validate_rule_payload(
    *,
    metric_name: Optional[str],
    condition: Optional[AlertConditionBody],
    severity: Optional[str],
    channels: Optional[list[str]],
) -> None:
    """규칙 필드 화이트리스트 검증 (400/422)."""
    from app.services.alert_evaluator import _METRIC_LABELS  # 내부 화이트리스트

    if metric_name is not None and metric_name not in _METRIC_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"지원하지 않는 메트릭입니다: {metric_name}",
        )
    if condition is not None:
        if condition.operator not in _ALERT_OPERATORS:
            raise HTTPException(
                status_code=422,
                detail=f"지원하지 않는 연산자입니다: {condition.operator}",
            )
        try:
            float(condition.threshold)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="threshold 는 숫자여야 합니다.")
    if severity is not None and severity not in _ALERT_SEVERITIES:
        raise HTTPException(
            status_code=422,
            detail=f"지원하지 않는 심각도입니다. (허용: {', '.join(sorted(_ALERT_SEVERITIES))})",
        )
    if channels is not None:
        for ch in channels:
            if ch not in _ALERT_CHANNELS:
                raise HTTPException(
                    status_code=422,
                    detail=f"지원하지 않는 채널입니다: {ch}",
                )


@router.get("/alerts/metrics", summary="지원되는 메트릭 목록")
def list_alert_metrics(_=Depends(require_admin_access)):
    from app.services.alert_evaluator import list_supported_metrics
    return success_response(data={"metrics": list_supported_metrics()})


@router.get("/alerts/rules", summary="알림 규칙 목록")
def list_alert_rules(
    enabled_only: bool = Query(default=False),
    _=Depends(require_admin_access),
):
    from app.repositories.alert_repository import alert_repository
    with get_db() as conn:
        rules = alert_repository.list_rules(conn, enabled_only=enabled_only)
    return success_response(data=rules)


@router.post("/alerts/rules", summary="알림 규칙 생성")
def create_alert_rule(
    body: CreateAlertRuleBody,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.alert_repository import alert_repository

    _validate_rule_payload(
        metric_name=body.metric_name,
        condition=body.condition,
        severity=body.severity,
        channels=body.channels,
    )

    with get_db() as conn:
        rule = alert_repository.create_rule(
            conn,
            name=body.name,
            description=body.description,
            metric_name=body.metric_name,
            condition=body.condition.model_dump(),
            severity=body.severity,
            channels=body.channels,
            channel_config=body.channel_config,
            enabled=body.enabled,
            created_by=actor.actor_id,
        )
        conn.commit()

    try:
        audit_emitter.emit(
            event_type="ALERT_RULE_CREATED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="alert_rule",
            resource_id=rule["id"],
            result="success",
            metadata={"name": rule["name"], "severity": rule["severity"]},
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data=rule)


@router.get("/alerts/rules/{rule_id}", summary="알림 규칙 상세")
def get_alert_rule(
    rule_id: str,
    _=Depends(require_admin_access),
):
    from app.repositories.alert_repository import alert_repository
    # UUID 형식 검증 (SQL injection 2중 방어)
    if not re.match(r"^[0-9a-f\-]{36}$", rule_id, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="유효하지 않은 rule_id 형식입니다.")
    with get_db() as conn:
        rule = alert_repository.get_rule(conn, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="알림 규칙을 찾을 수 없습니다.")
    return success_response(data=rule)


@router.patch("/alerts/rules/{rule_id}", summary="알림 규칙 수정")
def update_alert_rule(
    rule_id: str,
    body: UpdateAlertRuleBody,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.alert_repository import alert_repository

    if not re.match(r"^[0-9a-f\-]{36}$", rule_id, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="유효하지 않은 rule_id 형식입니다.")

    _validate_rule_payload(
        metric_name=body.metric_name,
        condition=body.condition,
        severity=body.severity,
        channels=body.channels,
    )

    fields: dict[str, Any] = {}
    for k in ("name", "description", "metric_name", "severity",
              "channels", "channel_config", "enabled"):
        v = getattr(body, k)
        if v is not None:
            fields[k] = v
    if body.condition is not None:
        fields["condition"] = body.condition.model_dump()

    with get_db() as conn:
        existing = alert_repository.get_rule(conn, rule_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="알림 규칙을 찾을 수 없습니다.")
        updated = alert_repository.update_rule(conn, rule_id, fields)
        conn.commit()

    try:
        audit_emitter.emit(
            event_type="ALERT_RULE_UPDATED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="alert_rule",
            resource_id=rule_id,
            result="success",
            metadata={"changed_fields": list(fields.keys())},
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data=updated)


@router.delete("/alerts/rules/{rule_id}", summary="알림 규칙 삭제")
def delete_alert_rule(
    rule_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.alert_repository import alert_repository

    if not re.match(r"^[0-9a-f\-]{36}$", rule_id, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="유효하지 않은 rule_id 형식입니다.")

    with get_db() as conn:
        deleted = alert_repository.delete_rule(conn, rule_id)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="알림 규칙을 찾을 수 없습니다.")

    try:
        audit_emitter.emit(
            event_type="ALERT_RULE_DELETED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="alert_rule",
            resource_id=rule_id,
            result="success",
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data={"deleted": True})


@router.get("/alerts/history", summary="알림 이력 조회")
def list_alert_history(
    status: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    from_ts: Optional[str] = Query(default=None, alias="from"),
    to_ts: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=50, ge=1, le=500),
    _=Depends(require_admin_access),
):
    from app.repositories.alert_repository import alert_repository

    if status is not None and status not in _ALERT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"지원하지 않는 상태입니다. (허용: {', '.join(sorted(_ALERT_STATUSES))})",
        )
    if severity is not None and severity not in _ALERT_SEVERITIES:
        raise HTTPException(
            status_code=422,
            detail=f"지원하지 않는 심각도입니다. (허용: {', '.join(sorted(_ALERT_SEVERITIES))})",
        )

    offset = (page - 1) * page_size
    with get_db() as conn:
        items, total = alert_repository.list_history(
            conn,
            status=status,
            severity=severity,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=page_size,
            offset=offset,
        )
    return success_response(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("/alerts/history/{history_id}/acknowledge", summary="알림 확인")
def acknowledge_alert(
    history_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.alert_repository import alert_repository

    if not re.match(r"^[0-9a-f\-]{36}$", history_id, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="유효하지 않은 history_id 형식입니다.")

    with get_db() as conn:
        acked = alert_repository.acknowledge(conn, history_id, actor.actor_id)
        conn.commit()
    if acked is None:
        raise HTTPException(status_code=404, detail="알림 이력을 찾을 수 없거나 이미 확인되었습니다.")

    try:
        audit_emitter.emit(
            event_type="ALERT_ACKNOWLEDGED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="alert_history",
            resource_id=history_id,
            result="success",
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data=acked)


@router.post("/alerts/evaluate", summary="알림 규칙 즉시 평가 (수동)")
def evaluate_alerts_now(_=Depends(require_admin_access)):
    from app.services.alert_evaluator import alert_evaluator
    stats = alert_evaluator.evaluate_all()
    return success_response(data=stats)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 14-14: 배치 작업 스케줄 관리 API
# ═══════════════════════════════════════════════════════════════════════════
#
# 스케줄 정의(=job_schedules)는 ID 기반 (e.g. 'reindex_all'), 실행 기록은
# 기존 background_jobs 를 재사용한다 (job_type = schedule.id).
# 경로는 `/jobs/schedules/*` 네임스페이스로 분리 — 기존 `/jobs/{uuid}` 와 충돌 방지.

_SCHEDULE_ID_RE = re.compile(r"^[a-z0-9_]{1,100}$")


class UpdateJobScheduleBody(BaseModel):
    schedule: Optional[str] = Field(None, min_length=1, max_length=120)
    enabled: Optional[bool] = None


def _validate_schedule_id(schedule_id: str) -> None:
    if not _SCHEDULE_ID_RE.match(schedule_id):
        raise HTTPException(status_code=422, detail="유효하지 않은 스케줄 ID 형식입니다.")


def _schedule_with_runs(
    conn,
    schedule: dict[str, Any],
    *,
    include_runs: bool = True,
) -> dict[str, Any]:
    """스케줄 dict 에 cron 설명 및 최근 실행 이력(옵션)을 덧붙인다."""
    from app.services.cron_util import describe_ko, next_run
    from app.repositories.job_schedule_repository import job_schedule_repository

    result = dict(schedule)
    cron = schedule.get("schedule")
    if cron:
        try:
            result["schedule_description"] = describe_ko(cron)
        except Exception as exc:
            logger.debug("cron 설명 생성 실패: %s", exc)
            result["schedule_description"] = None
        if schedule.get("enabled") and not schedule.get("next_run_at"):
            try:
                result["next_run_at"] = next_run(cron)
            except Exception as exc:
                logger.debug("next_run 계산 실패: %s", exc)
    else:
        result["schedule_description"] = None

    # 현재 상태 ('idle'/'running'/'failed')
    running = job_schedule_repository.get_running_run(conn, schedule["id"])
    if running:
        result["status"] = "running"
        result["current_run_id"] = running["id"]
    elif schedule.get("last_run_result") == "failed":
        result["status"] = "failed"
    else:
        result["status"] = "idle"

    if include_runs:
        result["recent_runs"] = job_schedule_repository.list_recent_runs(
            conn, schedule["id"], limit=10
        )
    return result


@router.get("/jobs/schedules", summary="배치 작업 스케줄 목록")
def list_job_schedules(_=Depends(require_admin_access)):
    from app.repositories.job_schedule_repository import job_schedule_repository
    with get_db() as conn:
        schedules = job_schedule_repository.list_schedules(conn)
        items = [_schedule_with_runs(conn, s, include_runs=False) for s in schedules]
    return success_response(data=items)


@router.get("/jobs/schedules/{job_id}", summary="배치 작업 스케줄 상세")
def get_job_schedule(job_id: str, _=Depends(require_admin_access)):
    from app.repositories.job_schedule_repository import job_schedule_repository
    _validate_schedule_id(job_id)
    with get_db() as conn:
        sched = job_schedule_repository.get_schedule(conn, job_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
        result = _schedule_with_runs(conn, sched, include_runs=True)
    return success_response(data=result)


@router.post("/jobs/schedules/{job_id}/run", summary="배치 작업 수동 실행", status_code=202)
def run_job_schedule(
    job_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.job_schedule_repository import job_schedule_repository

    _validate_schedule_id(job_id)
    with get_db() as conn:
        sched = job_schedule_repository.get_schedule(conn, job_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
        running = job_schedule_repository.get_running_run(conn, job_id)
        if running:
            raise HTTPException(status_code=409, detail="이미 실행 중입니다.")
        run_id = job_schedule_repository.enqueue_manual_run(
            conn, job_id, requester_id=actor.actor_id
        )
        conn.commit()

    try:
        audit_emitter.emit(
            event_type="JOB_SCHEDULE_RUN",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="job_schedule",
            resource_id=job_id,
            result="success",
            metadata={"run_id": run_id},
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data={"message": "작업이 시작되었습니다", "run_id": run_id})


@router.patch("/jobs/schedules/{job_id}", summary="배치 작업 스케줄 수정")
def update_job_schedule(
    job_id: str,
    body: UpdateJobScheduleBody,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.job_schedule_repository import job_schedule_repository
    from app.services.cron_util import validate as cron_validate, next_run as cron_next

    _validate_schedule_id(job_id)

    fields: dict[str, Any] = {}
    if body.schedule is not None:
        try:
            cron_validate(body.schedule)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"유효하지 않은 cron: {e}")
        fields["schedule"] = body.schedule
        try:
            fields["next_run_at"] = cron_next(body.schedule)
        except Exception as exc:
            logger.debug("cron next_run 계산 실패: %s", exc)
            fields["next_run_at"] = None
    if body.enabled is not None:
        fields["enabled"] = body.enabled

    with get_db() as conn:
        existing = job_schedule_repository.get_schedule(conn, job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
        updated = job_schedule_repository.update_schedule(conn, job_id, fields)
        conn.commit()

    try:
        audit_emitter.emit(
            event_type="JOB_SCHEDULE_UPDATED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="job_schedule",
            resource_id=job_id,
            result="success",
            metadata={"changed_fields": list(fields.keys())},
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data=updated)


@router.post("/jobs/schedules/{job_id}/cancel", summary="배치 작업 취소")
def cancel_job_schedule(
    job_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter
    from app.repositories.job_schedule_repository import job_schedule_repository

    _validate_schedule_id(job_id)
    with get_db() as conn:
        sched = job_schedule_repository.get_schedule(conn, job_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
        cancelled_id = job_schedule_repository.mark_cancel_requested(conn, job_id)
        if cancelled_id is None:
            raise HTTPException(status_code=400, detail="실행 중인 작업이 없습니다.")
        conn.commit()

    try:
        audit_emitter.emit(
            event_type="JOB_SCHEDULE_CANCELLED",
            action="admin.write",
            actor_id=actor.actor_id,
            actor_role=actor.role,
            resource_type="job_schedule",
            resource_id=job_id,
            result="success",
            metadata={"cancelled_run_id": cancelled_id},
        )
    except Exception as exc:
        logger.warning("감사 이벤트 기록 실패: %s", exc)

    return success_response(data={"message": "작업 취소가 요청되었습니다", "run_id": cancelled_id})


@router.post("/jobs/schedules/cron/preview", summary="Cron 표현식 미리보기")
def preview_cron(body: dict[str, Any], _=Depends(require_admin_access)):
    """사용자 입력 cron 을 검증하고 한국어 설명 + 다음 실행 3회를 반환."""
    from app.services.cron_util import validate as cron_validate, describe_ko, next_run

    expr = body.get("schedule") if isinstance(body, dict) else None
    if not isinstance(expr, str):
        raise HTTPException(status_code=422, detail="schedule 필드가 필요합니다.")
    try:
        cron_validate(expr)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    description = describe_ko(expr)
    nexts: list[str] = []
    from datetime import datetime as _dt, timezone as _tz
    # Python 3.12+에서 datetime.utcnow() 는 deprecated — aware datetime 사용
    cursor = _dt.now(_tz.utc).replace(tzinfo=None)
    for _ in range(3):
        nxt = next_run(expr, cursor)
        if nxt is None:
            break
        nexts.append(nxt.isoformat() + "Z")
        cursor = nxt
    return success_response(data={"description": description, "next_runs": nexts})


# ===========================================================================
# Phase 14-15: API 키 CRUD 및 감사 로그 필터 메타데이터
# ===========================================================================
#
# 설계 원칙:
#   - API 키 전체 문자열은 생성 응답에서 단 한 번만 반환 (DB 에는 SHA-256 해시만 저장)
#   - 폐기는 soft-revoke: status = 'REVOKED' 로 표시 (과거 감사 흔적 보존)
#   - 감사 이벤트 (API_KEY_ISSUED / API_KEY_REVOKED) 발행
#   - 감사 로그 필터 드롭다운을 위한 이벤트 유형 카탈로그 엔드포인트 제공
# ===========================================================================

_API_KEY_BYTES = 32            # 256bit → base64url 기본 ~43자, `mk_` prefix 포함 46자
_API_KEY_NAME_MAX = 100
_API_KEY_DESC_MAX = 500
_VALID_API_KEY_SCOPES = {"READ_ONLY", "READ_WRITE", "admin.read", "admin.write"}
_VALID_API_KEY_EXPIRY_DAYS = {30, 90, 180, 365, 0}  # 0 == 무기한

_API_KEY_NAME_RE = re.compile(r"^[\w\- .]{1,100}$", re.UNICODE)


class CreateApiKeyBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=_API_KEY_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_API_KEY_DESC_MAX)
    scope: str = Field(default="READ_ONLY")
    expires_in_days: int = Field(default=90, ge=0, le=3650)


class RevokeApiKeyBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


def _generate_api_key() -> tuple[str, str, str]:
    """(full_key, prefix, hash) 생성. full_key 는 UI 에 단 한 번만 반환."""
    raw = _secrets.token_urlsafe(_API_KEY_BYTES)
    # `mk_` 접두어 + url-safe base64 본체 (영/숫자/`-`/`_`)
    full = f"mk_{raw}"
    prefix = full[:8]
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    return full, prefix, digest


def _validate_api_key_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="이름을 입력하세요.")
    if not _API_KEY_NAME_RE.match(name):
        raise HTTPException(status_code=422, detail="이름에 허용되지 않은 문자가 포함되어 있습니다.")
    return name


def _validate_api_key_scope(scope: str) -> str:
    if scope not in _VALID_API_KEY_SCOPES:
        raise HTTPException(status_code=422, detail=f"지원되지 않는 scope: {scope}")
    return scope


@router.post("/api-keys", summary="API 키 생성", status_code=201)
def create_api_key(
    body: CreateApiKeyBody,
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    """API 키를 생성한다. 응답에 포함된 full_key 는 **한 번만** 반환된다.

    - SHA-256 해시만 저장 (원본 복구 불가)
    - expires_in_days == 0 → 무기한
    - 감사 이벤트 API_KEY_ISSUED 발행
    """
    from app.audit.emitter import audit_emitter

    name = _validate_api_key_name(body.name)
    scope = _validate_api_key_scope(body.scope)
    description = (body.description or "").strip() or None

    full_key, prefix, digest = _generate_api_key()
    issuer_id = str(actor.actor_id) if actor.actor_id else None
    issuer_name = getattr(actor, "actor_name", None) if actor else None

    expires_at_sql = "NULL" if body.expires_in_days == 0 else "NOW() + (%s || ' days')::interval"

    with get_db() as conn:
        with conn.cursor() as cur:
            if body.expires_in_days == 0:
                cur.execute(
                    """
                    INSERT INTO api_keys (name, description, key_prefix, key_hash,
                                          scope, status, issuer_id, issuer_name)
                    VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, %s)
                    RETURNING id, name, description, key_prefix, scope, status,
                              issuer_name, last_used_at, last_used_ip, use_count,
                              expires_at, created_at
                    """,
                    (name, description, prefix, digest, scope, issuer_id, issuer_name),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO api_keys (name, description, key_prefix, key_hash,
                                          scope, status, issuer_id, issuer_name, expires_at)
                    VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, %s, {expires_at_sql})
                    RETURNING id, name, description, key_prefix, scope, status,
                              issuer_name, last_used_at, last_used_ip, use_count,
                              expires_at, created_at
                    """,
                    (name, description, prefix, digest, scope, issuer_id, issuer_name, str(body.expires_in_days)),
                )
            row = cur.fetchone()

    # 감사 이벤트 (실패 무시)
    try:
        audit_emitter.emit(
            event_type="API_KEY_ISSUED",
            action="api_key.create",
            actor_id=issuer_id,
            actor_role=getattr(actor, "role", None),
            resource_type="api_key",
            resource_id=str(row["id"]),
            result="success",
            metadata={"name": name, "scope": scope, "expires_in_days": body.expires_in_days},
        )
    except Exception:  # pragma: no cover
        logger.exception("API_KEY_ISSUED 감사 이벤트 발행 실패")

    return success_response(
        data={
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"],
            "key_prefix": row["key_prefix"],
            "scope": row["scope"],
            "status": row["status"],
            "issuer_name": row["issuer_name"],
            "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
            "last_used_ip": row["last_used_ip"],
            "use_count": row["use_count"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            # ⚠️ full_key 는 한 번만 반환 (UI 도 저장하지 않음)
            "full_key": full_key,
        }
    )


@router.post("/api-keys/{key_id}/revoke", summary="API 키 폐기")
def revoke_api_key(
    key_id: str,
    body: RevokeApiKeyBody = Body(default_factory=RevokeApiKeyBody),
    actor: ActorContext = Depends(resolve_current_actor),
    _=Depends(require_admin_access),
):
    from app.audit.emitter import audit_emitter

    # UUID 형식 검증
    try:
        import uuid as _uuid
        _uuid.UUID(key_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="잘못된 key_id 형식")

    reason = (body.reason or "").strip() or None

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE api_keys
                SET status = 'REVOKED', revoked_reason = %s
                WHERE id = %s AND status = 'ACTIVE'
                RETURNING id, name, key_prefix
                """,
                (reason, key_id),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="키를 찾을 수 없거나 이미 폐기됨")

    try:
        audit_emitter.emit(
            event_type="API_KEY_REVOKED",
            action="api_key.revoke",
            actor_id=str(actor.actor_id) if actor.actor_id else None,
            actor_role=getattr(actor, "role", None),
            resource_type="api_key",
            resource_id=str(row["id"]),
            result="success",
            metadata={"name": row["name"], "key_prefix": row["key_prefix"], "reason": reason},
        )
    except Exception:  # pragma: no cover
        logger.exception("API_KEY_REVOKED 감사 이벤트 발행 실패")

    return success_response(data={"id": str(row["id"]), "name": row["name"], "status": "REVOKED"})


# --- 감사 로그 메타데이터 ---

# 필터 드롭다운에 노출할 감사 이벤트 유형 카탈로그 (라벨은 한국어)
_AUDIT_EVENT_TYPES: list[tuple[str, str]] = [
    ("USER_LOGIN",                  "사용자 로그인"),
    ("USER_LOGIN_FAILED",           "로그인 실패"),
    ("USER_CREATED",                "사용자 생성"),
    ("USER_DEACTIVATED",            "사용자 비활성화"),
    ("USER_ROLE_CHANGED",           "사용자 역할 변경"),
    ("ROLE_CHANGED",                "역할 변경"),
    ("PERMISSION_CHANGED",          "권한 변경"),
    ("DOCUMENT_CREATED",            "문서 생성"),
    ("DOCUMENT_UPDATED",            "문서 수정"),
    ("DOCUMENT_PUBLISHED",          "문서 게시"),
    ("DOCUMENT_DELETED",            "문서 삭제"),
    ("DOCUMENT_FORCE_DELETED",      "문서 강제 삭제"),
    ("DOCUMENT_TYPE_CREATED",       "문서 유형 생성"),
    ("DOCUMENT_TYPE_DEACTIVATED",   "문서 유형 비활성화"),
    ("API_KEY_ISSUED",              "API 키 발급"),
    ("API_KEY_REVOKED",             "API 키 폐기"),
    ("JOB_SCHEDULE_RUN",            "배치 수동 실행"),
    ("JOB_SCHEDULE_UPDATED",        "배치 스케줄 변경"),
    ("JOB_SCHEDULE_CANCELLED",      "배치 취소"),
    ("JOB_FORCE_STOPPED",           "작업 강제 중지"),
    ("ALERT_RULE_CREATED",          "알림 규칙 생성"),
    ("ALERT_RULE_UPDATED",          "알림 규칙 수정"),
    ("ALERT_RULE_DELETED",          "알림 규칙 삭제"),
    ("SETTINGS_UPDATED",            "설정 변경"),
    ("ADMIN_ACCOUNT_MODIFIED",      "관리자 계정 변경"),
]


@router.get("/audit-logs/event-types", summary="감사 이벤트 유형 카탈로그")
def list_audit_event_types(_=Depends(require_admin_access)):
    """감사 로그 필터 드롭다운용 이벤트 유형 목록 (정적 카탈로그).

    실제 발생한 이벤트 유형만 노출하지 않고 전체 카탈로그를 반환 —
    운영 중 새로 나타날 유형에 대비.
    """
    return success_response(
        data={"items": [{"value": v, "label": label} for v, label in _AUDIT_EVENT_TYPES]}
    )


# ---------------------------------------------------------------------------
# 시스템 정보 — Tier 3 Admin Capabilities (Task 0-8 3-tier 분리)
# ---------------------------------------------------------------------------

@router.get(
    "/system/capabilities",
    summary="전체 시스템 정보 조회 (Admin 전용, Tier 3)",
    description=(
        "내부 구성 정보를 포함한 전체 플랫폼 기능 가용성을 반환한다.\n\n"
        "**Admin 권한 필요** (ORG_ADMIN, SUPER_ADMIN).\n\n"
        "보안 근거: pgvector_enabled, supported_providers, deployment_type 등\n"
        "내부 구성 정보는 공격 표면 분석에 악용될 수 있으므로 Admin으로 제한한다.\n\n"
        "응답은 **5분간 캐시**된다(`Cache-Control: private, max-age=300`)."
    ),
    tags=["admin"],
)
def get_admin_capabilities(
    response: Response,
    _=Depends(require_admin_access),
):
    """Tier 3 — Admin 전용 전체 시스템 정보.

    system.py의 _get_full_capabilities()를 그대로 반환한다.
    Tier 2와 달리 pgvector_enabled, supported_providers, deployment_type,
    closed_network 등 내부 구성 필드를 모두 포함한다.
    """
    from app.api.v1.system import _get_full_capabilities

    data = _get_full_capabilities()
    response.headers["Cache-Control"] = "private, max-age=300"
    return success_response(data=data)


# ===========================================================================
# S2 Phase 6 (FG6.1): LLM 프로바이더 CRUD
# ===========================================================================

def _provider_row(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "type": r["type"],
        "model_name": r["model_name"],
        "api_base_url": r["api_base_url"],
        "description": r["description"],
        "is_default": r["is_default"],
        "status": r["status"],
        "last_tested_at": r["last_tested_at"].isoformat() if r["last_tested_at"] else None,
        "last_test_result": r["last_test_result"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("/providers", summary="LLM 프로바이더 목록")
def list_providers(_=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM llm_providers ORDER BY type, is_default DESC, name"
            )
            rows = cur.fetchall()
    return success_response(data=[_provider_row(r) for r in rows])


def _validate_api_key(v: Optional[str]) -> Optional[str]:
    if v and not v.isascii():
        raise ValueError("API Key는 ASCII 문자만 허용됩니다")
    return v


class CreateProviderBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    type: str = Field(..., pattern="^(llm|embedding)$")
    model_name: str = Field(..., min_length=1, max_length=255)
    api_base_url: Optional[str] = Field(None, max_length=1024)
    api_key: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    is_default: bool = False

    @field_validator("api_key")
    @classmethod
    def check_api_key(cls, v: Optional[str]) -> Optional[str]:
        return _validate_api_key(v)


class UpdateProviderBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    model_name: Optional[str] = Field(None, min_length=1, max_length=255)
    api_base_url: Optional[str] = Field(None, max_length=1024)
    api_key: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = Field(None, pattern="^(active|inactive)$")

    @field_validator("api_key")
    @classmethod
    def check_api_key(cls, v: Optional[str]) -> Optional[str]:
        return _validate_api_key(v)


@router.post("/providers", summary="LLM 프로바이더 생성", status_code=201)
def create_provider(body: CreateProviderBody, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            if body.is_default:
                cur.execute(
                    "UPDATE llm_providers SET is_default = FALSE WHERE type = %s",
                    (body.type,),
                )
            cur.execute(
                """
                INSERT INTO llm_providers
                    (name, type, model_name, api_base_url, api_key, description, is_default)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (body.name, body.type, body.model_name, body.api_base_url,
                 body.api_key, body.description, body.is_default),
            )
            row = cur.fetchone()
    return success_response(data=_provider_row(row))


@router.patch("/providers/{provider_id}", summary="LLM 프로바이더 수정")
def update_provider(
    provider_id: str,
    body: UpdateProviderBody,
    _=Depends(require_admin_access),
):
    updates = {
        k: v for k, v in body.model_dump().items()
        if v is not None and not (k == "api_key" and v == "")
    }
    if not updates:
        raise HTTPException(status_code=422, detail="변경할 항목이 없습니다.")

    set_clauses = [f"{k} = %s" for k in updates] + ["updated_at = NOW()"]
    values = list(updates.values()) + [provider_id]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE llm_providers SET {', '.join(set_clauses)} WHERE id = %s RETURNING *",
                values,
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="프로바이더를 찾을 수 없습니다.")
    return success_response(data=_provider_row(row))


@router.delete("/providers/{provider_id}", summary="LLM 프로바이더 삭제", status_code=204)
def delete_provider(provider_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM llm_providers WHERE id = %s RETURNING id", (provider_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="프로바이더를 찾을 수 없습니다.")


class SetDefaultBody(BaseModel):
    type: Optional[str] = None


@router.post("/providers/{provider_id}/set-default", summary="기본 프로바이더 지정")
def set_default_provider(
    provider_id: str,
    body: SetDefaultBody = SetDefaultBody(),
    _=Depends(require_admin_access),
):
    type_: Optional[str] = body.type

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers WHERE id = %s", (provider_id,))
            provider = cur.fetchone()
            if not provider:
                raise HTTPException(status_code=404, detail="프로바이더를 찾을 수 없습니다.")
            ptype = type_ or provider["type"]
            cur.execute(
                "UPDATE llm_providers SET is_default = FALSE WHERE type = %s",
                (ptype,),
            )
            cur.execute(
                "UPDATE llm_providers SET is_default = TRUE, updated_at = NOW() WHERE id = %s RETURNING *",
                (provider_id,),
            )
            row = cur.fetchone()
    return success_response(data=_provider_row(row))


@router.post("/providers/{provider_id}/test", summary="프로바이더 연결 테스트")
def test_provider(provider_id: str, _=Depends(require_admin_access)):
    import time
    from datetime import datetime, timezone

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers WHERE id = %s", (provider_id,))
            provider = cur.fetchone()
            if not provider:
                raise HTTPException(status_code=404, detail="프로바이더를 찾을 수 없습니다.")

    # 실제 연결 테스트 (api_base_url이 있으면 /models 엔드포인트 GET, 없으면 mock)
    import httpx
    start = time.monotonic()
    success_flag = False
    error_msg: Optional[str] = None
    error_detail: Optional[str] = None
    http_status: Optional[int] = None

    try:
        base_url = provider["api_base_url"]
        if base_url:
            headers = {}
            if provider["api_key"]:
                headers["Authorization"] = f"Bearer {provider['api_key']}"
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(base_url.rstrip("/") + "/models", headers=headers)
            http_status = resp.status_code
            success_flag = resp.status_code < 400
            if not success_flag:
                _status_labels = {
                    400: "잘못된 요청",
                    401: "인증 실패 — API Key를 확인하세요",
                    403: "접근 권한 없음",
                    404: "엔드포인트를 찾을 수 없음 — API Base URL을 확인하세요",
                    429: "요청 횟수 초과 (Rate Limit)",
                    500: "서버 내부 오류",
                    502: "게이트웨이 오류",
                    503: "서비스 이용 불가",
                }
                error_msg = _status_labels.get(resp.status_code, f"HTTP {resp.status_code} 오류")
                # 응답 바디에서 오류 메시지 추출
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        error_detail = (
                            body.get("error", {}).get("message")
                            or body.get("message")
                            or body.get("detail")
                            or resp.text[:500]
                        )
                    else:
                        error_detail = resp.text[:500]
                except Exception as exc:
                    logger.debug("webhook 응답 파싱 실패: %s", exc)
                    error_detail = resp.text[:500] if resp.text else None
        else:
            success_flag = True  # URL 미설정 시 mock 성공 처리
    except UnicodeEncodeError:
        error_msg = "API Key에 사용할 수 없는 문자가 포함되어 있습니다"
        error_detail = "HTTP 헤더는 ASCII 문자만 허용합니다. API Key를 다시 확인하세요."
    except httpx.ConnectError as exc:
        error_msg = "연결 실패: 서버에 접근할 수 없습니다"
        error_detail = str(exc)
    except httpx.TimeoutException:
        error_msg = "연결 시간 초과 (10초)"
        error_detail = f"URL: {provider['api_base_url']}"
    except Exception as exc:
        error_msg = type(exc).__name__
        error_detail = str(exc)

    latency_ms = int((time.monotonic() - start) * 1000)
    result_str = "success" if success_flag else "error"
    tested_at = datetime.now(timezone.utc)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE llm_providers
                SET last_tested_at = %s, last_test_result = %s,
                    status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (tested_at, result_str,
                 "active" if success_flag else "error",
                 provider_id),
            )

    return success_response(data={
        "success": success_flag,
        "latency_ms": latency_ms if success_flag else None,
        "http_status": http_status,
        "error": error_msg,
        "error_detail": error_detail,
        "tested_at": tested_at.isoformat(),
    })


# ===========================================================================
# S2 Phase 6 (FG6.1): 프롬프트 버전 관리 CRUD
# ===========================================================================

def _prompt_version_row(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "prompt_id": str(r["prompt_id"]),
        "version_number": r["version_number"],
        "content": r["content"],
        "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat(),
        "is_active": r["is_active"],
    }


def _prompt_row(r: dict, versions: list | None = None) -> dict:
    row = {
        "id": str(r["id"]),
        "name": r["name"],
        "description": r["description"],
        "active_version": r.get("active_version_number"),
        "active_version_id": str(r["active_version_id"]) if r.get("active_version_id") else None,
        "ab_test_config": r["ab_test_config"],
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }
    if versions is not None:
        row["versions"] = versions
    return row


@router.get("/prompts", summary="프롬프트 목록")
def list_prompts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _=Depends(require_admin_access),
):
    offset = (page - 1) * page_size
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM prompts")
            total = cur.fetchone()["total"]
            cur.execute(
                """
                SELECT p.*, pv.version_number AS active_version_number
                FROM prompts p
                LEFT JOIN prompt_versions pv ON pv.id = p.active_version_id
                ORDER BY p.updated_at DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            rows = cur.fetchall()
    items = [_prompt_row(r) for r in rows]
    return list_response(data=items, page=page, page_size=page_size, total=total)


@router.get("/prompts/{prompt_id}", summary="프롬프트 상세 (버전 포함)")
def get_prompt(prompt_id: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, pv.version_number AS active_version_number
                FROM prompts p
                LEFT JOIN prompt_versions pv ON pv.id = p.active_version_id
                WHERE p.id = %s
                """,
                (prompt_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="프롬프트를 찾을 수 없습니다.")
            cur.execute(
                "SELECT * FROM prompt_versions WHERE prompt_id = %s ORDER BY version_number",
                (prompt_id,),
            )
            ver_rows = cur.fetchall()
    versions = [_prompt_version_row(v) for v in ver_rows]
    return success_response(data=_prompt_row(row, versions=versions))


class CreatePromptBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=500)
    content: str = Field(..., min_length=1)


@router.post("/prompts", summary="프롬프트 생성", status_code=201)
def create_prompt(
    body: CreatePromptBody,
    _=Depends(require_admin_access),
    actor: ActorContext = Depends(resolve_current_actor),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO prompts (name, description) VALUES (%s, %s) RETURNING *",
                (body.name, body.description),
            )
            prompt = cur.fetchone()
            prompt_id = prompt["id"]
            cur.execute(
                """
                INSERT INTO prompt_versions (prompt_id, version_number, content, created_by, is_active)
                VALUES (%s, 1, %s, %s, TRUE)
                RETURNING *
                """,
                (prompt_id, body.content, str(actor.actor_id) if actor.actor_id else None),
            )
            ver = cur.fetchone()
            cur.execute(
                "UPDATE prompts SET active_version_id = %s, updated_at = NOW() WHERE id = %s RETURNING *",
                (ver["id"], prompt_id),
            )
            prompt = cur.fetchone()
    prompt_dict = dict(prompt)
    prompt_dict["active_version_number"] = 1
    return success_response(data=_prompt_row(prompt_dict, versions=[_prompt_version_row(ver)]))


class NewVersionBody(BaseModel):
    content: str = Field(..., min_length=1)


@router.post("/prompts/{prompt_id}/versions", summary="새 프롬프트 버전 생성")
def create_prompt_version(
    prompt_id: str,
    body: NewVersionBody,
    _=Depends(require_admin_access),
    actor: ActorContext = Depends(resolve_current_actor),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM prompts WHERE id = %s", (prompt_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="프롬프트를 찾을 수 없습니다.")
            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_ver FROM prompt_versions WHERE prompt_id = %s",
                (prompt_id,),
            )
            next_ver = cur.fetchone()["next_ver"]
            cur.execute(
                """
                INSERT INTO prompt_versions (prompt_id, version_number, content, created_by, is_active)
                VALUES (%s, %s, %s, %s, FALSE)
                RETURNING *
                """,
                (prompt_id, next_ver, body.content, str(actor.actor_id) if actor.actor_id else None),
            )
            ver = cur.fetchone()
    return success_response(data=_prompt_version_row(ver))


@router.post("/prompts/{prompt_id}/versions/{version_id}/activate", summary="버전 활성화")
def activate_prompt_version(
    prompt_id: str,
    version_id: str,
    _=Depends(require_admin_access),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM prompt_versions WHERE id = %s AND prompt_id = %s",
                (version_id, prompt_id),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")
            cur.execute(
                "UPDATE prompt_versions SET is_active = FALSE WHERE prompt_id = %s",
                (prompt_id,),
            )
            cur.execute(
                "UPDATE prompt_versions SET is_active = TRUE WHERE id = %s RETURNING version_number",
                (version_id,),
            )
            active_ver = cur.fetchone()["version_number"]
            cur.execute(
                "UPDATE prompts SET active_version_id = %s, updated_at = NOW() WHERE id = %s RETURNING *",
                (version_id, prompt_id),
            )
            prompt = cur.fetchone()
    prompt_dict = dict(prompt)
    prompt_dict["active_version_number"] = active_ver
    return success_response(data=_prompt_row(prompt_dict))


class ABTestBody(BaseModel):
    ab_test_config: Optional[dict] = None


@router.patch("/prompts/{prompt_id}/ab-test", summary="A/B 테스트 설정")
def set_prompt_ab_test(
    prompt_id: str,
    body: ABTestBody,
    _=Depends(require_admin_access),
):
    with get_db() as conn:
        with conn.cursor() as cur:
            import json as _json
            cur.execute(
                "UPDATE prompts SET ab_test_config = %s, updated_at = NOW() WHERE id = %s RETURNING *",
                (_json.dumps(body.ab_test_config) if body.ab_test_config else None, prompt_id),
            )
            prompt = cur.fetchone()
            if not prompt:
                raise HTTPException(status_code=404, detail="프롬프트를 찾을 수 없습니다.")
            cur.execute(
                "SELECT version_number FROM prompt_versions WHERE id = %s",
                (prompt["active_version_id"],) if prompt["active_version_id"] else (None,),
            )
            ver_row = cur.fetchone()
    prompt_dict = dict(prompt)
    prompt_dict["active_version_number"] = ver_row["version_number"] if ver_row else None
    return success_response(data=_prompt_row(prompt_dict))
