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

from app.models.scope_profile import ScopeDefinition, ScopeProfile, ScopeProfileSettings
from app.utils.json_utils import dumps_ko, loads_maybe
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# S3 Phase 3 FG 3-2 (2026-04-27): settings_json 의 알려진 키 화이트리스트.
# 알려지지 않은 키는 PATCH 시 무시 + load 시 raw 보존 (forward compatibility).
_KNOWN_SETTINGS_KEYS: frozenset[str] = frozenset({"expose_viewers"})


def _settings_from_raw(raw) -> ScopeProfileSettings:
    """settings_json (str | dict | None) → ScopeProfileSettings dataclass.

    - JSON 파싱 실패 / 타입 불일치 시 default ScopeProfileSettings 반환 (fail-closed).
    - 알려지지 않은 키는 dataclass 에 채워지지 않음 (raw 는 DB 에 보존됨).
    """
    if raw is None:
        return ScopeProfileSettings()
    parsed = loads_maybe(raw)
    if not isinstance(parsed, dict):
        return ScopeProfileSettings()
    return ScopeProfileSettings(
        expose_viewers=bool(parsed.get("expose_viewers", False)),
    )


def _settings_to_raw(settings: ScopeProfileSettings) -> dict:
    """ScopeProfileSettings → JSON 직렬화 가능한 dict (알려진 키만).

    호출자가 raw 머지를 한 번 더 해야 forward-compat 가 보장됨.
    """
    return {"expose_viewers": bool(settings.expose_viewers)}


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
        settings: Optional[ScopeProfileSettings] = None,
    ) -> ScopeProfile:
        """ScopeProfile 신규 생성. settings 미지정 시 기본 ScopeProfileSettings (모두 false).

        S3 Phase 3 FG 3-2 (2026-04-27): settings 파라미터 추가. 기본값 보수적.
        """
        now = utcnow()
        pid = str(uuid4())
        settings_dict = _settings_to_raw(settings or ScopeProfileSettings())
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scope_profiles (
                    id, name, description, organization_id, settings_json, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING id, name, description, organization_id, settings_json, created_at, updated_at
                """,
                (pid, name, description, organization_id, dumps_ko(settings_dict), now, now),
            )
            row = cur.fetchone()
        return self._row_to_profile(row, scopes=[])

    def get_by_id(self, profile_id: str) -> Optional[ScopeProfile]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description, organization_id, settings_json, created_at, updated_at"
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
                    "SELECT id, name, description, organization_id, settings_json, created_at, updated_at"
                    " FROM scope_profiles WHERE organization_id = %s"
                    " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (organization_id, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT id, name, description, organization_id, settings_json, created_at, updated_at"
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
        settings_patch: Optional[dict] = None,
    ) -> Optional[ScopeProfile]:
        """ScopeProfile 부분 갱신.

        S3 Phase 3 FG 3-2 (2026-04-27): ``settings_patch`` 추가.
            - dataclass 필드만 추출해서 적용 (알 수 없는 키는 무시).
            - 기존 raw settings_json 과 머지 (forward compatibility — 기존 미지의 키 보존).
            - None 이면 settings 미수정.
        """
        sets: list[str] = ["updated_at = %s"]
        params: list = [utcnow()]
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if settings_patch is not None:
            # 기존 row 의 settings_json 을 읽어 dataclass 필드만 patch 적용 후 머지.
            current = self.get_by_id(profile_id)
            if current is None:
                return None
            current_raw = _settings_to_raw(current.settings)
            # raw 에는 미지의 키도 있을 수 있으므로 DB 에서 직접 다시 읽어 머지.
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT settings_json FROM scope_profiles WHERE id = %s",
                    (profile_id,),
                )
                raw_row = cur.fetchone()
                raw_existing = loads_maybe(raw_row["settings_json"]) if raw_row else {}
                if not isinstance(raw_existing, dict):
                    raw_existing = {}
            merged = dict(raw_existing)
            # patch 의 알려진 키만 적용
            for known_key in _KNOWN_SETTINGS_KEYS:
                if known_key in settings_patch:
                    merged[known_key] = bool(settings_patch[known_key])
            sets.append("settings_json = %s::jsonb")
            params.append(dumps_ko(merged))
        params.append(profile_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE scope_profiles SET {', '.join(sets)} WHERE id = %s"
                " RETURNING id, name, description, organization_id, settings_json, created_at, updated_at",
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
            settings=_settings_from_raw(row.get("settings_json")),
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
