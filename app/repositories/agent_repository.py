"""
Agent Repository — Phase 4 (S2).

agents 테이블 CRUD + 킬스위치 관리.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.models.agent import Agent

logger = logging.getLogger(__name__)


class AgentRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        organization_id: Optional[str] = None,
        scope_profile_id: Optional[str] = None,
        created_by: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Agent:
        now = datetime.now(timezone.utc)
        aid = str(uuid4())
        import json
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agents
                    (id, name, description, organization_id, scope_profile_id,
                     is_disabled, metadata, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s)
                RETURNING id, name, description, organization_id, scope_profile_id,
                          is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at
                """,
                (
                    aid, name, description, organization_id, scope_profile_id,
                    json.dumps(metadata or {}), created_by, now, now,
                ),
            )
            row = cur.fetchone()
        return self._row_to_agent(row)

    def get_by_id(self, agent_id: str) -> Optional[Agent]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description, organization_id, scope_profile_id,"
                "       is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at"
                " FROM agents WHERE id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
        return self._row_to_agent(row) if row else None

    def list_agents(
        self,
        *,
        organization_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Agent]:
        with self._conn.cursor() as cur:
            if organization_id:
                cur.execute(
                    "SELECT id, name, description, organization_id, scope_profile_id,"
                    "       is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at"
                    " FROM agents WHERE organization_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (organization_id, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT id, name, description, organization_id, scope_profile_id,"
                    "       is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at"
                    " FROM agents ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            rows = cur.fetchall()
        return [self._row_to_agent(r) for r in rows]

    def count(self, *, organization_id: Optional[str] = None) -> int:
        with self._conn.cursor() as cur:
            if organization_id:
                cur.execute("SELECT COUNT(*) FROM agents WHERE organization_id = %s", (organization_id,))
            else:
                cur.execute("SELECT COUNT(*) FROM agents")
            return cur.fetchone()["count"]

    def update(
        self,
        agent_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        scope_profile_id: Optional[str] = None,
    ) -> Optional[Agent]:
        sets: list[str] = ["updated_at = %s"]
        params: list = [datetime.now(timezone.utc)]
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if scope_profile_id is not None:
            sets.append("scope_profile_id = %s")
            params.append(scope_profile_id)
        params.append(agent_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE agents SET {', '.join(sets)} WHERE id = %s"
                " RETURNING id, name, description, organization_id, scope_profile_id,"
                "           is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at",
                params,
            )
            row = cur.fetchone()
        return self._row_to_agent(row) if row else None

    def delete(self, agent_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM agents WHERE id = %s", (agent_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 킬스위치
    # ------------------------------------------------------------------

    def enable_kill_switch(self, agent_id: str, *, reason: Optional[str] = None) -> Optional[Agent]:
        """에이전트 킬스위치 활성화 — 즉시 쓰기 차단.

        에이전트는 비활성(inactive) 상태가 되어 모든 쓰기 요청이 차단된다.
        is_disabled = True (is_active = False) 상태로 전환된다.
        """
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agents
                SET is_disabled = TRUE, disabled_at = %s, disabled_reason = %s, updated_at = %s
                WHERE id = %s
                RETURNING id, name, description, organization_id, scope_profile_id,
                          is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at
                """,
                (now, reason, now, agent_id),
            )
            row = cur.fetchone()
        return self._row_to_agent(row) if row else None

    def disable_kill_switch(self, agent_id: str) -> Optional[Agent]:
        """에이전트 킬스위치 해제."""
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agents
                SET is_disabled = FALSE, disabled_at = NULL, disabled_reason = NULL, updated_at = %s
                WHERE id = %s
                RETURNING id, name, description, organization_id, scope_profile_id,
                          is_disabled, disabled_at, disabled_reason, metadata, created_by, created_at, updated_at
                """,
                (now, agent_id),
            )
            row = cur.fetchone()
        return self._row_to_agent(row) if row else None

    def is_disabled(self, agent_id: str) -> bool:
        """킬스위치 상태 조회 (빠른 경로 — 단일 컬럼 조회)."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT is_disabled FROM agents WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        if not row:
            return True  # 존재하지 않는 에이전트 → 차단
        return bool(row["is_disabled"])

    # ------------------------------------------------------------------
    # API Key 바인딩 조회
    # ------------------------------------------------------------------

    def get_by_api_key_id(self, api_key_id: str) -> Optional[Agent]:
        """API Key ID로 연결된 에이전트를 조회한다."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.name, a.description, a.organization_id, a.scope_profile_id,
                       a.is_disabled, a.disabled_at, a.disabled_reason, a.metadata,
                       a.created_by, a.created_at, a.updated_at
                FROM agents a
                JOIN api_keys ak ON ak.agent_id = a.id
                WHERE ak.id = %s
                """,
                (api_key_id,),
            )
            row = cur.fetchone()
        return self._row_to_agent(row) if row else None

    # ------------------------------------------------------------------
    # Row → Model
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_agent(row) -> Agent:
        import json
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return Agent(
            id=str(row["id"]),
            name=row["name"],
            description=row.get("description"),
            organization_id=str(row["organization_id"]) if row.get("organization_id") else None,
            scope_profile_id=str(row["scope_profile_id"]) if row.get("scope_profile_id") else None,
            is_disabled=bool(row["is_disabled"]),
            disabled_at=row.get("disabled_at"),
            disabled_reason=row.get("disabled_reason"),
            metadata=metadata,
            created_by=str(row["created_by"]) if row.get("created_by") else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
