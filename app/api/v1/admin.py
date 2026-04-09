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

import re
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
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
        "indexing_failed": 0,  # Phase 10 연계 전 placeholder
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
    return success_response(data={"items": items})


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
    return success_response(data={"items": items})


# ---------------------------------------------------------------------------
# 사용자 관리
# ---------------------------------------------------------------------------

@router.get("/users", summary="사용자 목록")
def list_users(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
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
    return success_response(data={"items": items})


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

    items = [
        {
            "type_code": r["type_code"],
            "display_name": r["display_name"],
            "description": r["description"],
            "status": r["status"],
            "schema_field_count": r["field_count"] or 0,
            "document_count": r["document_count"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return success_response(data={"items": items})


@router.get("/document-types/{type_code}", summary="DocumentType 상세")
def get_document_type(type_code: str, _=Depends(require_admin_access)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM document_types WHERE type_code = %s", (type_code,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="DocumentType을 찾을 수 없습니다.")

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
_VALID_STATUSES = {"ACTIVE", "INACTIVE", "SUSPENDED"}


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
        "updated_at": row["updated_at"].isoformat(),
    })


@router.delete("/document-types/{type_code}", summary="DocumentType 비활성화", status_code=204)
def deactivate_document_type(type_code: str, _=Depends(require_admin_access)):
    """실제 삭제 대신 status를 INACTIVE로 변경한다 (참조 무결성 보호)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_types SET status = 'INACTIVE', updated_at = NOW() WHERE type_code = %s",
                (type_code,),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="DocumentType을 찾을 수 없습니다.")
