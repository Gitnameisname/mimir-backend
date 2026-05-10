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
from app.utils.json_utils import dumps_ko
from app.utils.strings import normalize_lower
from app.models.role import Role, UserOrgRole
from app.models.user import User
from app.db.cursor_helpers import fetch_one_as

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
        # Phase 14 인증 확장 필드
        username=row.get("username"),
        password_hash=row.get("password_hash"),
        auth_provider=row.get("auth_provider", "local"),
        email_verified=row.get("email_verified", False),
        email_verified_at=row.get("email_verified_at"),
        failed_login_count=row.get("failed_login_count", 0),
        locked_until=row.get("locked_until"),
        avatar_url=row.get("avatar_url"),
        # S2-5 (2026-04-20): Scope Profile 바인딩 — 칼럼 없는 구버전 DB 도 방어
        scope_profile_id=(
            str(row["scope_profile_id"])
            if row.get("scope_profile_id")
            else None
        ),
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
        return fetch_one_as(conn, "SELECT * FROM users WHERE id = %s", (user_id,), lambda row: _row_to_user(row))

    def get_many_by_ids(
        self,
        conn: psycopg2.extensions.connection,
        user_ids: list[str],
    ) -> dict[str, User]:
        """user_id 리스트를 받아 ``{user_id: User}`` 매핑 반환.

        S3 Phase 3 FG 3-1 (2026-04-27): Contributors 패널이 카테고리별 actor_id 를 한 번에
        해석하기 위해 N+1 회피용 batch fetch.

        - 비어있거나 모두 invalid UUID 인 경우 빈 dict 반환.
        - 존재하지 않는 id 는 결과 dict 에 키가 없음 (호출자 책임).
        """
        if not user_ids:
            return {}
        # invalid UUID 형식은 silently skip — psycopg2 가 invalid input 으로 raise 하는 것을 방지.
        # actor_id 가 agent / system 도메인 문자열 (UUID 가 아닌 경우) 일 수 있음.
        valid_uuid_ids: list[str] = []
        for raw in user_ids:
            if not raw:
                continue
            try:
                from uuid import UUID
                UUID(str(raw))
                valid_uuid_ids.append(str(raw))
            except (ValueError, TypeError):
                continue
        if not valid_uuid_ids:
            return {}
        result: dict[str, User] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE id = ANY(%s::uuid[])",
                (valid_uuid_ids,),
            )
            for row in cur.fetchall():
                user = _row_to_user(row)
                result[user.id] = user
        return result

    def get_by_email(self, conn: psycopg2.extensions.connection, email: str) -> Optional[User]:
        return fetch_one_as(conn, "SELECT * FROM users WHERE email = %s", (email,), lambda row: _row_to_user(row))

    def search_by_display_name_in_orgs(
        self,
        conn: psycopg2.extensions.connection,
        *,
        viewer_user_id: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """S3 Phase 5 FG 5-3 — 멘션 typeahead용 사용자 검색 (viewer organization scope).

        viewer 가 속한 organization (user_org_roles JOIN) 안 사용자 중 display_name 이
        prefix 매칭되는 항목을 반환. **email / role / status / 다른 메타는 응답에 포함하지
        않는다** (R-A4 누설 차단).

        Args:
            viewer_user_id: 호출자 본인 user_id — keyword-only required (S2 ⑥ Scope 하드코딩 금지).
                            **반드시 ActorContext 에서만 추출**. query / body 주입 금지.
            query: 정규화된 prefix (호출자 측에서 trim 후 전달).
            limit: 1~50 범위 (호출자 검증).

        Returns:
            [{user_id, display_name}] list. timing-safe 한 결과 — 같은 query 길이에서
            결과 수에 따른 응답 시간 차이가 미미해야 한다 (LIMIT + INDEX 활용).
        """
        if not query:
            return []
        # ILIKE prefix 매칭. % 와 _ wildcard 는 escape (이미 호출자 측 normalize 가정하나 방어).
        # NFC 정규화는 호출자 측 (frontend → backend body) 책임. 본 메서드는 raw 비교.
        # SQL injection: psycopg2 placeholder 가 차단. 추가 방어: query 길이 상한 라우터에서.
        prefix = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        # viewer 가 속한 org_ids 중 같은 org 의 다른 사용자 검색.
        # - user_org_roles JOIN 으로 같은 org 에 둘 이상이 속한 행만 반환
        # - viewer 본인은 제외 (멘션 자기 자신은 의미 없음)
        # - status = 'ACTIVE' 만 — 비활성 사용자는 노출 안 함
        sql = """
            SELECT DISTINCT u.id, u.display_name
            FROM users u
            JOIN user_org_roles target_uor ON target_uor.user_id = u.id
            JOIN user_org_roles viewer_uor ON viewer_uor.org_id = target_uor.org_id
            WHERE viewer_uor.user_id = %s
              AND u.id != %s
              AND u.status = 'ACTIVE'
              AND u.display_name ILIKE %s ESCAPE '\\'
            ORDER BY u.display_name ASC, u.id ASC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (viewer_user_id, viewer_user_id, prefix, limit))
            rows = cur.fetchall()
        return [
            {"user_id": str(r["id"]), "display_name": r["display_name"]}
            for r in rows
        ]

    def get_by_username(
        self, conn: psycopg2.extensions.connection, username: str
    ) -> Optional[User]:
        """아이디(username)로 사용자를 조회한다. 대소문자 구분 없음."""
        return fetch_one_as(conn, "SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username,), lambda row: _row_to_user(row))

    def get_by_identifier(
        self, conn: psycopg2.extensions.connection, identifier: str
    ) -> Optional[User]:
        """이메일 또는 아이디로 사용자를 조회한다.

        identifier에 '@'가 포함되어 있으면 이메일로, 그렇지 않으면 아이디로 조회한다.
        """
        if "@" in identifier:
            # 도서관 §1.4 BE-G1 (2026-04-25): normalize_lower 로 strip→lower 통일.
            # identifier 는 위 if 분기로 인해 None 이 아니므로 결과도 str 보장.
            normalized = normalize_lower(identifier)
            assert normalized is not None  # for type checkers
            return self.get_by_email(conn, normalized)
        return self.get_by_username(conn, identifier.strip())

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
        password_hash: Optional[str] = None,
        auth_provider: str = "local",
        email_verified: bool = False,
        avatar_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> User:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, display_name, role_name, status,
                                   password_hash, auth_provider, email_verified,
                                   avatar_url, username)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (email, display_name, role_name, status,
                 password_hash, auth_provider, email_verified,
                 avatar_url, username),
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
        password_hash: Optional[str] = None,
        email_verified: Optional[bool] = None,
        failed_login_count: Optional[int] = None,
        locked_until: Any = None,  # datetime | None, sentinel 패턴용
        last_login_at: Any = None,
        avatar_url: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[User]:
        _UNSET = object()
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
        if password_hash is not None:
            fields.append("password_hash = %s")
            params.append(password_hash)
        if email_verified is not None:
            fields.append("email_verified = %s")
            params.append(email_verified)
        if failed_login_count is not None:
            fields.append("failed_login_count = %s")
            params.append(failed_login_count)
        if locked_until is not _UNSET and locked_until is not None:
            fields.append("locked_until = %s")
            params.append(locked_until)
        if last_login_at is not _UNSET and last_login_at is not None:
            fields.append("last_login_at = %s")
            params.append(last_login_at)
        if avatar_url is not None:
            fields.append("avatar_url = %s")
            params.append(avatar_url)
        if username is not None:
            fields.append("username = %s")
            params.append(username)

        if not fields:
            return self.get_by_id(conn, user_id)

        fields.append("updated_at = NOW()")
        params.append(user_id)

        return fetch_one_as(conn, f"UPDATE users SET {', '.join(fields)} WHERE id = %s RETURNING *", params, lambda row: _row_to_user(row))

    def record_login_success(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
    ) -> Optional[User]:
        """로그인 성공 시: failed_login_count 초기화, last_login_at 갱신."""
        return fetch_one_as(conn, """
                UPDATE users
                SET failed_login_count = 0, locked_until = NULL,
                    last_login_at = NOW(), updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """, (user_id,), lambda row: _row_to_user(row))

    # ------------------------------------------------------------------
    # Phase 1 FG 1-3 — users.preferences (JSONB)
    # ------------------------------------------------------------------

    def get_preferences(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
    ) -> dict[str, Any]:
        """user.preferences JSONB 를 dict 로 반환한다. 컬럼 부재 시 빈 dict.

        컬럼 부재 대응: Alembic `s3_p1_users_preferences` 이전 DB 에서도 안전하게
        빈 dict 를 반환한다 (쿼리 실패 시 fallback).
        """
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT preferences FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        except Exception:
            # preferences 컬럼 부재 등 — 빈 dict 반환 (기능 degrade)
            conn.rollback()
            return {}
        if row is None:
            return {}
        raw = row.get("preferences")
        if raw is None:
            return {}
        if isinstance(raw, str):
            # JSON 직렬화 상태로 오는 드라이버 대응
            import json as _json
            try:
                return _json.loads(raw)
            except (ValueError, TypeError):
                return {}
        if isinstance(raw, dict):
            return raw
        return {}

    def update_preferences(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """주어진 ``patch`` 를 기존 preferences 에 shallow merge 하고 반환.

        키를 ``None`` 으로 설정하면 해당 키가 삭제된다. 중첩 merge 는 하지 않는다
        (Phase 3 에서 Deep merge 재검토 — task1-3.md Q2).
        """
        import json as _json

        current = self.get_preferences(conn, user_id)
        merged: dict[str, Any] = dict(current)
        for k, v in (patch or {}).items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET preferences = %s::jsonb, updated_at = NOW()
                WHERE id = %s
                RETURNING preferences
                """,
                # 도서관 §1.3 BE-G2 (2026-04-25): dumps_ko 통일 (ensure_ascii=False 고정)
                (dumps_ko(merged), user_id),
            )
            row = cur.fetchone()
        if row is None:
            return merged
        ret = row.get("preferences")
        if isinstance(ret, str):
            try:
                return _json.loads(ret)
            except (ValueError, TypeError):
                return merged
        if isinstance(ret, dict):
            return ret
        return merged

    def record_login_failure(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
    ) -> Optional[User]:
        """로그인 실패 시: failed_login_count 증가."""
        return fetch_one_as(conn, """
                UPDATE users
                SET failed_login_count = failed_login_count + 1, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """, (user_id,), lambda row: _row_to_user(row))

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
        return fetch_one_as(conn, "SELECT role_name FROM user_org_roles WHERE user_id = %s AND org_id = %s", (user_id, org_id), lambda row: row["role_name"])


# ---------------------------------------------------------------------------
# OrganizationsRepository
# ---------------------------------------------------------------------------

class OrganizationsRepository:

    def get_by_id(self, conn: psycopg2.extensions.connection, org_id: str) -> Optional[Organization]:
        return fetch_one_as(conn, "SELECT * FROM organizations WHERE id = %s", (org_id,), lambda row: _row_to_organization(row))

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

        return fetch_one_as(conn, f"UPDATE organizations SET {', '.join(fields)} WHERE id = %s RETURNING *", params, lambda row: _row_to_organization(row))

    def delete(self, conn: psycopg2.extensions.connection, org_id: str) -> bool:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# RolesRepository
# ---------------------------------------------------------------------------

class RolesRepository:

    def get_by_id(self, conn: psycopg2.extensions.connection, role_id: str) -> Optional[Role]:
        return fetch_one_as(conn, "SELECT * FROM roles WHERE id = %s", (role_id,), lambda row: _row_to_role(row))

    def get_by_name(self, conn: psycopg2.extensions.connection, name: str) -> Optional[Role]:
        return fetch_one_as(conn, "SELECT * FROM roles WHERE name = %s", (name,), lambda row: _row_to_role(row))

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
        return fetch_one_as(conn, "UPDATE roles SET description = %s WHERE id = %s AND is_system = FALSE RETURNING *", (description, role_id), lambda row: _row_to_role(row))

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
