"""
Users / Organizations / Roles persistence repository.

책임:
  - users, organizations, roles, user_org_roles 테이블 CRUD
  - DB row(RealDictRow) → 도메인 모델 변환
  - service 레이어가 SQL을 직접 작성하지 않도록 추상화
"""

import logging
from typing import Any, Optional

import psycopg2.extensions

from app.models.organization import Organization
from app.models.role import Role, UserOrgRole
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row → 도메인 모델 변환
# ---------------------------------------------------------------------------

def _row_to_user(row: dict[str, Any]) -> User:
    return User(
        id=str(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        status=row["status"],
        role_name=row["role_name"],
        last_login_at=row.get("last_login_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_organization(row: dict[str, Any]) -> Organization:
    return Organization(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_role(row: dict[str, Any]) -> Role:
    return Role(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        is_system=row["is_system"],
        created_at=row["created_at"],
    )


def _row_to_user_org_role(row: dict[str, Any]) -> UserOrgRole:
    return UserOrgRole(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        org_id=str(row["org_id"]),
        role_name=row["role_name"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# UsersRepository
# ---------------------------------------------------------------------------

class UsersRepository:

    # --- 조회 ---

    def get_by_id(self, conn: psycopg2.extensions.connection, user_id: str) -> Optional[User]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return _row_to_user(row) if row else None

    def get_by_email(self, conn: psycopg2.extensions.connection, email: str) -> Optional[User]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
        return _row_to_user(row) if row else None

    def list(
        self,
        conn: psycopg2.extensions.connection,
        *,
        search: Optional[str] = None,
        status: Optional[str] = None,
        role_name: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        conditions: list[str] = []
        params: list[Any] = []

        if search:
            conditions.append("(display_name ILIKE %s OR email ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if status:
            conditions.append("status = %s")
            params.append(status)
        if role_name:
            conditions.append("role_name = %s")
            params.append(role_name)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM users {where}", params)
            total: int = cur.fetchone()["total"]

            cur.execute(
                f"SELECT * FROM users {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = cur.fetchall()

        return [_row_to_user(r) for r in rows], total

    # --- 생성/수정/삭제 ---

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        email: str,
        display_name: str,
        role_name: str = "VIEWER",
        status: str = "ACTIVE",
    ) -> User:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, display_name, role_name, status)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (email, display_name, role_name, status),
            )
            row = cur.fetchone()
        return _row_to_user(row)

    def update(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        role_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[User]:
        fields: list[str] = []
        params: list[Any] = []

        if display_name is not None:
            fields.append("display_name = %s")
            params.append(display_name)
        if role_name is not None:
            fields.append("role_name = %s")
            params.append(role_name)
        if status is not None:
            fields.append("status = %s")
            params.append(status)

        if not fields:
            return self.get_by_id(conn, user_id)

        fields.append("updated_at = NOW()")
        params.append(user_id)

        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE id = %s RETURNING *",
                params,
            )
            row = cur.fetchone()
        return _row_to_user(row) if row else None

    def delete(self, conn: psycopg2.extensions.connection, user_id: str) -> bool:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            return cur.rowcount > 0

    # --- 조직 역할 매핑 ---

    def assign_org_role(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        org_id: str,
        role_name: str,
    ) -> UserOrgRole:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_org_roles (user_id, org_id, role_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, org_id, role_name) DO UPDATE
                    SET role_name = EXCLUDED.role_name
                RETURNING *
                """,
                (user_id, org_id, role_name),
            )
            row = cur.fetchone()
        return _row_to_user_org_role(row)

    def remove_org_role(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        org_id: str,
    ) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_org_roles WHERE user_id = %s AND org_id = %s",
                (user_id, org_id),
            )
            return cur.rowcount > 0

    def get_user_role_in_org(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        org_id: str,
    ) -> Optional[str]:
        """특정 조직에서의 사용자 역할명을 반환한다."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role_name FROM user_org_roles WHERE user_id = %s AND org_id = %s",
                (user_id, org_id),
            )
            row = cur.fetchone()
        return row["role_name"] if row else None


# ---------------------------------------------------------------------------
# OrganizationsRepository
# ---------------------------------------------------------------------------

class OrganizationsRepository:

    def get_by_id(self, conn: psycopg2.extensions.connection, org_id: str) -> Optional[Organization]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM organizations WHERE id = %s", (org_id,))
            row = cur.fetchone()
        return _row_to_organization(row) if row else None

    def list(
        self,
        conn: psycopg2.extensions.connection,
        *,
        search: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Organization], int]:
        conditions: list[str] = []
        params: list[Any] = []

        if search:
            conditions.append("name ILIKE %s")
            params.append(f"%{search}%")
        if status:
            conditions.append("status = %s")
            params.append(status)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM organizations {where}", params)
            total: int = cur.fetchone()["total"]

            cur.execute(
                f"SELECT * FROM organizations {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = cur.fetchall()

        return [_row_to_organization(r) for r in rows], total

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        name: str,
        description: Optional[str] = None,
        status: str = "ACTIVE",
    ) -> Organization:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO organizations (name, description, status)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (name, description, status),
            )
            row = cur.fetchone()
        return _row_to_organization(row)

    def update(
        self,
        conn: psycopg2.extensions.connection,
        org_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[Organization]:
        fields: list[str] = []
        params: list[Any] = []

        if name is not None:
            fields.append("name = %s")
            params.append(name)
        if description is not None:
            fields.append("description = %s")
            params.append(description)
        if status is not None:
            fields.append("status = %s")
            params.append(status)

        if not fields:
            return self.get_by_id(conn, org_id)

        fields.append("updated_at = NOW()")
        params.append(org_id)

        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE organizations SET {', '.join(fields)} WHERE id = %s RETURNING *",
                params,
            )
            row = cur.fetchone()
        return _row_to_organization(row) if row else None

    def delete(self, conn: psycopg2.extensions.connection, org_id: str) -> bool:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# RolesRepository
# ---------------------------------------------------------------------------

class RolesRepository:

    def get_by_id(self, conn: psycopg2.extensions.connection, role_id: str) -> Optional[Role]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM roles WHERE id = %s", (role_id,))
            row = cur.fetchone()
        return _row_to_role(row) if row else None

    def get_by_name(self, conn: psycopg2.extensions.connection, name: str) -> Optional[Role]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM roles WHERE name = %s", (name,))
            row = cur.fetchone()
        return _row_to_role(row) if row else None

    def list(self, conn: psycopg2.extensions.connection) -> list[Role]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM roles ORDER BY is_system DESC, name")
            rows = cur.fetchall()
        return [_row_to_role(r) for r in rows]

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        name: str,
        description: Optional[str] = None,
    ) -> Role:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roles (name, description, is_system)
                VALUES (%s, %s, FALSE)
                RETURNING *
                """,
                (name, description),
            )
            row = cur.fetchone()
        return _row_to_role(row)

    def update(
        self,
        conn: psycopg2.extensions.connection,
        role_id: str,
        *,
        description: Optional[str] = None,
    ) -> Optional[Role]:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE roles SET description = %s WHERE id = %s AND is_system = FALSE RETURNING *",
                (description, role_id),
            )
            row = cur.fetchone()
        return _row_to_role(row) if row else None

    def delete(self, conn: psycopg2.extensions.connection, role_id: str) -> bool:
        """is_system=FALSE인 역할만 삭제 가능."""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM roles WHERE id = %s AND is_system = FALSE",
                (role_id,),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 모듈 수준 싱글턴
# ---------------------------------------------------------------------------

users_repository = UsersRepository()
organizations_repository = OrganizationsRepository()
roles_repository = RolesRepository()
