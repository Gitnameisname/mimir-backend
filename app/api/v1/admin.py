"""
Admin router — /api/v1/admin

관리자 전용 API. Phase 7 MVP 구현.

인증: X-Admin-Key 헤더 (settings.admin_api_key 값과 일치해야 함)
     개발 단계 단순 인증 — 실제 JWT/세션 연동은 Phase 8 이후 예정.

엔드포인트:
  - GET /admin/dashboard/metrics
  - GET /admin/dashboard/health
  - GET /admin/dashboard/errors
  - GET /admin/dashboard/recent-audit-logs
  - GET /admin/users
  - GET /admin/users/{user_id}
  - GET /admin/organizations
  - GET /admin/organizations/{org_id}
  - GET /admin/roles
  - GET /admin/roles/{role_id}
  - GET /admin/audit-logs
  - GET /admin/document-types
  - GET /admin/document-types/{type_code}
  - GET /admin/jobs
  - GET /admin/jobs/{job_id}
  - GET /admin/indexing/jobs
  - GET /admin/api-keys
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.api.responses import list_response, success_response
from app.config import settings
from app.db import get_db

router = APIRouter()


# ---------------------------------------------------------------------------
# Admin 인증 의존성
# ---------------------------------------------------------------------------

def require_admin(x_admin_key: str = Header(default="")) -> None:
    """X-Admin-Key 헤더로 관리자 인증을 확인한다."""
    if not settings.admin_api_key or x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------

@router.get("/dashboard/metrics", summary="핵심 지표 카드")
def get_dashboard_metrics(_=Depends(require_admin)):
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
def get_dashboard_health(_=Depends(require_admin)):
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
    _=Depends(require_admin),
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
    _=Depends(require_admin),
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
    _=Depends(require_admin),
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
def get_user(user_id: str, _=Depends(require_admin)):
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
    _=Depends(require_admin),
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
def get_organization(org_id: str, _=Depends(require_admin)):
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
def list_roles(_=Depends(require_admin)):
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
def get_role(role_id: str, _=Depends(require_admin)):
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

@router.get("/audit-logs", summary="감사 로그 목록")
def list_audit_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    from_dt: Optional[str] = Query(default=None, alias="from"),
    to_dt: Optional[str] = Query(default=None, alias="to"),
    actor_id: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    result: Optional[str] = Query(default=None),
    _=Depends(require_admin),
):
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
def get_audit_log(event_id: str, _=Depends(require_admin)):
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
    _=Depends(require_admin),
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
def get_document_type(type_code: str, _=Depends(require_admin)):
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
    _=Depends(require_admin),
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
def get_jobs_summary(_=Depends(require_admin)):
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
def get_job(job_id: str, _=Depends(require_admin)):
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
    _=Depends(require_admin),
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
def get_indexing_summary(_=Depends(require_admin)):
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
    _=Depends(require_admin),
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
