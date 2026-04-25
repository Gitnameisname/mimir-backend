"""
ScopeProfile Repository — Phase 4 (S2).

ScopeProfile, ScopeDefinition 테이블 CRUD.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from app.models.scope_profile import ScopeDefinition, ScopeProfile
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class ScopeProfileRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # ScopeProfile CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        organization_id: Optional[str] = None,
    ) -> ScopeProfile:
        now = utcnow()
        pid = str(uuid4())
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scope_profiles (id, name, description, organization_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, name, description, organization_id, created_at, updated_at
                """,
                (pid, name, description, organization_id, now, now),
            )
            row = cur.fetchone()
        return self._row_to_profile(row, scopes=[])

    def get_by_id(self, profile_id: str) -> Optional[ScopeProfile]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description, organization_id, created_at, updated_at"
                " FROM scope_profiles WHERE id = %s",
                (profile_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        scopes = self._list_definitions(profile_id)
        return self._row_to_profile(row, scopes=scopes)

    def list_profiles(
        self,
        *,
        organization_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ScopeProfile]:
        with self._conn.cursor() as cur:
            if organization_id:
                cur.execute(
                    "SELECT id, name, description, organization_id, created_at, updated_at"
                    " FROM scope_profiles WHERE organization_id = %s"
                    " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (organization_id, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT id, name, description, organization_id, created_at, updated_at"
                    " FROM scope_profiles ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            rows = cur.fetchall()
        profiles = []
        for row in rows:
            scopes = self._list_definitions(str(row["id"]))
            profiles.append(self._row_to_profile(row, scopes=scopes))
        return profiles

    def count(self, *, organization_id: Optional[str] = None) -> int:
        with self._conn.cursor() as cur:
            if organization_id:
                cur.execute(
                    "SELECT COUNT(*) FROM scope_profiles WHERE organization_id = %s",
                    (organization_id,),
                )
            else:
                cur.execute("SELECT COUNT(*) FROM scope_profiles")
            return cur.fetchone()["count"]

    def update(
        self,
        profile_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[ScopeProfile]:
        sets: list[str] = ["updated_at = %s"]
        params: list = [utcnow()]
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        params.append(profile_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE scope_profiles SET {', '.join(sets)} WHERE id = %s"
                " RETURNING id, name, description, organization_id, created_at, updated_at",
                params,
            )
            row = cur.fetchone()
        if not row:
            return None
        scopes = self._list_definitions(profile_id)
        return self._row_to_profile(row, scopes=scopes)

    def delete(self, profile_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM scope_profiles WHERE id = %s", (profile_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # ScopeDefinition CRUD
    # ------------------------------------------------------------------

    def add_definition(
        self,
        profile_id: str,
        *,
        scope_name: str,
        acl_filter: dict,
        description: Optional[str] = None,
    ) -> ScopeDefinition:
        did = str(uuid4())
        now = utcnow()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scope_definitions (id, scope_profile_id, scope_name, description, acl_filter, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (scope_profile_id, scope_name)
                DO UPDATE SET description = EXCLUDED.description, acl_filter = EXCLUDED.acl_filter
                RETURNING id, scope_profile_id, scope_name, description, acl_filter, created_at
                """,
                (did, profile_id, scope_name, description, json.dumps(acl_filter), now),
            )
            row = cur.fetchone()
        return self._row_to_definition(row)

    def delete_definition(self, profile_id: str, scope_name: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM scope_definitions WHERE scope_profile_id = %s AND scope_name = %s",
                (profile_id, scope_name),
            )
            return cur.rowcount > 0

    def get_definition(self, profile_id: str, scope_name: str) -> Optional[ScopeDefinition]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, scope_profile_id, scope_name, description, acl_filter, created_at"
                " FROM scope_definitions WHERE scope_profile_id = %s AND scope_name = %s",
                (profile_id, scope_name),
            )
            row = cur.fetchone()
        return self._row_to_definition(row) if row else None

    def _list_definitions(self, profile_id: str) -> list[ScopeDefinition]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, scope_profile_id, scope_name, description, acl_filter, created_at"
                " FROM scope_definitions WHERE scope_profile_id = %s ORDER BY scope_name",
                (profile_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_definition(r) for r in rows]

    # ------------------------------------------------------------------
    # Row → Model 변환
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_profile(row, *, scopes: list[ScopeDefinition]) -> ScopeProfile:
        return ScopeProfile(
            id=str(row["id"]),
            name=row["name"],
            description=row.get("description"),
            organization_id=str(row["organization_id"]) if row.get("organization_id") else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            scopes=scopes,
        )

    @staticmethod
    def _row_to_definition(row) -> ScopeDefinition:
        raw_filter = row["acl_filter"]
        if isinstance(raw_filter, str):
            raw_filter = json.loads(raw_filter)
        elif raw_filter is None:
            raw_filter = {}
        return ScopeDefinition(
            id=str(row["id"]),
            scope_profile_id=str(row["scope_profile_id"]),
            scope_name=row["scope_name"],
            description=row.get("description"),
            acl_filter=raw_filter,
            created_at=row["created_at"],
        )
