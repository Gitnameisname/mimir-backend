"""
ExtractionSchema Repository — Phase 8 FG8.1.

extraction_schemas, extraction_schema_versions 테이블 CRUD.
모든 메서드는 Scope Profile ACL 슬롯을 지원하며, actor_type("user"|"agent")을 추적한다.

설계 원칙:
  - 모든 DB 접근은 이 repository를 통한다 (raw SQL + psycopg2)
  - 조회는 기본적으로 is_soft_deleted=False 필터 적용
  - 업데이트는 새 버전 생성 (immutable history)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.models.extraction import (
    ExtractionFieldDef,
    ExtractionSchemaVersion,
    ExtractionTargetSchema,
)

logger = logging.getLogger(__name__)


class ActorInfo:
    """작업 수행자 정보 (S2 원칙 ⑤ actor_type 추적)."""

    VALID_TYPES = frozenset({"user", "agent"})

    def __init__(self, actor_id: str, actor_type: str = "user") -> None:
        if actor_type not in self.VALID_TYPES:
            raise ValueError(f"actor_type must be one of {self.VALID_TYPES}, got {actor_type!r}")
        self.actor_id = actor_id
        self.actor_type = actor_type


class ExtractionSchemaNotFoundError(Exception):
    pass


class ExtractionSchemaAlreadyExistsError(Exception):
    pass


class ExtractionSchemaRepository:
    """추출 스키마 저장소 (psycopg2 기반 raw SQL)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fields_to_json(self, fields: Dict[str, ExtractionFieldDef]) -> str:
        return json.dumps(
            {k: v.model_dump(mode="json") for k, v in fields.items()},
            ensure_ascii=False,
        )

    def _json_to_fields(self, raw: Any) -> Dict[str, ExtractionFieldDef]:
        if raw is None:
            return {}
        data: dict = raw if isinstance(raw, dict) else json.loads(raw)
        return {k: ExtractionFieldDef(**v) for k, v in data.items()}

    def _row_to_schema(self, row: dict) -> ExtractionTargetSchema:
        return ExtractionTargetSchema(
            id=UUID(str(row["id"])),
            doc_type_code=row["doc_type_code"],
            version=row["version"],
            fields=self._json_to_fields(row["fields_json"]),
            is_deprecated=row["is_deprecated"],
            deprecation_reason=row.get("deprecation_reason"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by=row["created_by"],
            updated_by=row["updated_by"],
            scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
            extra_metadata=row["extra_metadata"] if row.get("extra_metadata") else {},
        )

    def _row_to_version(self, row: dict) -> ExtractionSchemaVersion:
        return ExtractionSchemaVersion(
            id=UUID(str(row["id"])),
            schema_id=UUID(str(row["schema_id"])),
            version=row["version"],
            fields=self._json_to_fields(row["fields_json"]),
            is_deprecated=row.get("is_deprecated", False),
            deprecation_reason=row.get("deprecation_reason"),
            change_summary=row.get("change_summary"),
            changed_fields=row.get("changed_fields") or [],
            created_at=row["created_at"],
            created_by=row["created_by"],
            extra_metadata=row.get("extra_metadata") or {},
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        doc_type_code: str,
        fields: Dict[str, ExtractionFieldDef],
        actor_info: ActorInfo,
        scope_profile_id: Optional[UUID] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> ExtractionTargetSchema:
        """새로운 추출 스키마 생성 (version=1)."""
        now = datetime.now(timezone.utc)
        schema_id = str(uuid4())
        fields_json = self._fields_to_json(fields)
        meta_json = json.dumps(extra_metadata or {}, ensure_ascii=False)

        with self._conn.cursor() as cur:
            # 중복 확인 (soft-deleted 제외)
            cur.execute(
                """
                SELECT id FROM extraction_schemas
                WHERE doc_type_code = %s AND is_soft_deleted = FALSE
                LIMIT 1
                """,
                (doc_type_code,),
            )
            if cur.fetchone():
                raise ExtractionSchemaAlreadyExistsError(
                    f"DocumentType '{doc_type_code}'에 대한 추출 스키마가 이미 존재합니다"
                )

            # extraction_schemas 삽입
            cur.execute(
                """
                INSERT INTO extraction_schemas
                    (id, doc_type_code, version, fields_json, extra_metadata,
                     is_deprecated, created_at, updated_at, created_by, updated_by,
                     scope_profile_id, is_soft_deleted)
                VALUES
                    (%s, %s, 1, %s::jsonb, %s::jsonb,
                     FALSE, %s, %s, %s, %s,
                     %s, FALSE)
                RETURNING
                    id, doc_type_code, version, fields_json, extra_metadata,
                    is_deprecated, deprecation_reason,
                    created_at, updated_at, created_by, updated_by,
                    scope_profile_id
                """,
                (
                    schema_id, doc_type_code, fields_json, meta_json,
                    now, now, actor_info.actor_id, actor_info.actor_id,
                    str(scope_profile_id) if scope_profile_id else None,
                ),
            )
            row = cur.fetchone()

            # extraction_schema_versions 버전 이력 삽입
            ver_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO extraction_schema_versions
                    (id, schema_id, version, fields_json, extra_metadata,
                     is_deprecated, change_summary, changed_fields, created_at, created_by)
                VALUES
                    (%s, %s, 1, %s::jsonb, %s::jsonb,
                     FALSE, '초기 생성', '[]'::jsonb, %s, %s)
                """,
                (ver_id, schema_id, fields_json, meta_json, now, actor_info.actor_id),
            )

        return self._row_to_schema(row)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_doc_type(
        self,
        doc_type_code: str,
        *,
        scope_profile_id: Optional[UUID] = None,
    ) -> Optional[ExtractionTargetSchema]:
        """DocumentType별 최신(미삭제) 스키마 조회."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, doc_type_code, version, fields_json, extra_metadata,
                       is_deprecated, deprecation_reason,
                       created_at, updated_at, created_by, updated_by,
                       scope_profile_id
                FROM extraction_schemas
                WHERE doc_type_code = %s
                  AND is_soft_deleted = FALSE
                ORDER BY version DESC
                LIMIT 1
                """,
                (doc_type_code,),
            )
            row = cur.fetchone()
        return self._row_to_schema(row) if row else None

    def get_by_doc_type_and_version(
        self,
        doc_type_code: str,
        version: int,
    ) -> Optional[ExtractionTargetSchema]:
        """특정 버전의 스키마 조회."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, doc_type_code, version, fields_json, extra_metadata,
                       is_deprecated, deprecation_reason,
                       created_at, updated_at, created_by, updated_by,
                       scope_profile_id
                FROM extraction_schemas
                WHERE doc_type_code = %s AND version = %s AND is_soft_deleted = FALSE
                LIMIT 1
                """,
                (doc_type_code, version),
            )
            row = cur.fetchone()
        return self._row_to_schema(row) if row else None

    def get_versions(
        self,
        doc_type_code: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> List[ExtractionSchemaVersion]:
        """버전 이력 조회 (최신순)."""
        with self._conn.cursor() as cur:
            # schema_id 먼저 조회
            cur.execute(
                "SELECT id FROM extraction_schemas WHERE doc_type_code = %s ORDER BY version DESC LIMIT 1",
                (doc_type_code,),
            )
            schema_row = cur.fetchone()
            if not schema_row:
                return []
            schema_id = schema_row["id"]

            cur.execute(
                """
                SELECT id, schema_id, version, fields_json, extra_metadata,
                       is_deprecated, deprecation_reason,
                       change_summary, changed_fields, created_at, created_by
                FROM extraction_schema_versions
                WHERE schema_id = %s
                ORDER BY version DESC
                LIMIT %s OFFSET %s
                """,
                (str(schema_id), limit, offset),
            )
            rows = cur.fetchall()
        return [self._row_to_version(r) for r in rows]

    def list_all(
        self,
        *,
        is_deprecated: Optional[bool] = None,
        scope_profile_id: Optional[UUID] = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ExtractionTargetSchema]:
        """전체 스키마 목록 조회 (최신 버전만)."""
        conditions = []
        params: list = []

        if not include_deleted:
            conditions.append("is_soft_deleted = FALSE")

        if is_deprecated is not None:
            conditions.append("is_deprecated = %s")
            params.append(is_deprecated)

        if scope_profile_id is not None:
            conditions.append("scope_profile_id = %s")
            params.append(str(scope_profile_id))

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params += [limit, offset]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (doc_type_code)
                    id, doc_type_code, version, fields_json, extra_metadata,
                    is_deprecated, deprecation_reason,
                    created_at, updated_at, created_by, updated_by,
                    scope_profile_id
                FROM extraction_schemas
                {where}
                ORDER BY doc_type_code, version DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_schema(r) for r in rows]

    def search_by_field_name(
        self,
        field_name: str,
        *,
        scope_profile_id: Optional[UUID] = None,
        include_deleted: bool = False,
    ) -> List[ExtractionTargetSchema]:
        """특정 필드명을 포함하는 스키마 검색 (JSONB key 존재 확인)."""
        conditions = ["fields_json ? %s"]
        params: list = [field_name]

        if not include_deleted:
            conditions.append("is_soft_deleted = FALSE")

        if scope_profile_id is not None:
            conditions.append("scope_profile_id = %s")
            params.append(str(scope_profile_id))

        where = "WHERE " + " AND ".join(conditions)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, doc_type_code, version, fields_json, extra_metadata,
                       is_deprecated, deprecation_reason,
                       created_at, updated_at, created_by, updated_by,
                       scope_profile_id
                FROM extraction_schemas
                {where}
                ORDER BY updated_at DESC
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_schema(r) for r in rows]

    # ------------------------------------------------------------------
    # Update (새 버전 생성)
    # ------------------------------------------------------------------

    def update(
        self,
        doc_type_code: str,
        *,
        fields: Dict[str, ExtractionFieldDef],
        actor_info: ActorInfo,
        change_summary: Optional[str] = None,
    ) -> ExtractionTargetSchema:
        """스키마 업데이트 — 기존 레코드 갱신 + 버전 이력 추가."""
        now = datetime.now(timezone.utc)
        fields_json = self._fields_to_json(fields)

        with self._conn.cursor() as cur:
            # 현재 스키마 조회
            cur.execute(
                """
                SELECT id, version, fields_json
                FROM extraction_schemas
                WHERE doc_type_code = %s AND is_soft_deleted = FALSE
                ORDER BY version DESC LIMIT 1
                """,
                (doc_type_code,),
            )
            row = cur.fetchone()
            if not row:
                raise ExtractionSchemaNotFoundError(
                    f"DocumentType '{doc_type_code}'에 대한 추출 스키마를 찾을 수 없음"
                )

            schema_id = row["id"]
            new_version = row["version"] + 1

            # 변경된 필드 감지
            old_keys = set((row["fields_json"] or {}).keys())
            new_keys = set(fields.keys())
            changed = sorted(old_keys.symmetric_difference(new_keys))

            # 스키마 업데이트
            cur.execute(
                """
                UPDATE extraction_schemas
                SET version = %s, fields_json = %s::jsonb,
                    updated_at = %s, updated_by = %s
                WHERE id = %s
                RETURNING id, doc_type_code, version, fields_json, extra_metadata,
                          is_deprecated, deprecation_reason,
                          created_at, updated_at, created_by, updated_by, scope_profile_id
                """,
                (new_version, fields_json, now, actor_info.actor_id, str(schema_id)),
            )
            updated_row = cur.fetchone()

            # 버전 이력 추가
            ver_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO extraction_schema_versions
                    (id, schema_id, version, fields_json, extra_metadata,
                     is_deprecated, change_summary, changed_fields, created_at, created_by)
                VALUES
                    (%s, %s, %s, %s::jsonb, '{}',
                     FALSE, %s, %s::jsonb, %s, %s)
                """,
                (
                    ver_id, str(schema_id), new_version, fields_json,
                    change_summary or "필드 업데이트",
                    json.dumps(changed),
                    now, actor_info.actor_id,
                ),
            )

        return self._row_to_schema(updated_row)

    # ------------------------------------------------------------------
    # Delete / Restore
    # ------------------------------------------------------------------

    def delete(self, doc_type_code: str, actor_info: ActorInfo) -> bool:
        """소프트 삭제."""
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_schemas
                SET is_soft_deleted = TRUE, deleted_at = %s, deleted_by = %s
                WHERE doc_type_code = %s AND is_soft_deleted = FALSE
                """,
                (now, actor_info.actor_id, doc_type_code),
            )
            return cur.rowcount > 0

    def restore(self, doc_type_code: str, actor_info: ActorInfo) -> bool:
        """소프트 삭제 복구."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_schemas
                SET is_soft_deleted = FALSE, deleted_at = NULL, deleted_by = NULL
                WHERE doc_type_code = %s AND is_soft_deleted = TRUE
                """,
                (doc_type_code,),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Deprecate
    # ------------------------------------------------------------------

    def deprecate(
        self,
        doc_type_code: str,
        *,
        reason: str,
        actor_info: ActorInfo,
    ) -> ExtractionTargetSchema:
        """스키마 폐기 표시 (deprecated=True)."""
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_schemas
                SET is_deprecated = TRUE, deprecation_reason = %s,
                    updated_at = %s, updated_by = %s
                WHERE doc_type_code = %s AND is_soft_deleted = FALSE
                RETURNING id, doc_type_code, version, fields_json, extra_metadata,
                          is_deprecated, deprecation_reason,
                          created_at, updated_at, created_by, updated_by, scope_profile_id
                """,
                (reason, now, actor_info.actor_id, doc_type_code),
            )
            row = cur.fetchone()
        if not row:
            raise ExtractionSchemaNotFoundError(
                f"DocumentType '{doc_type_code}'에 대한 추출 스키마를 찾을 수 없음"
            )
        return self._row_to_schema(row)
