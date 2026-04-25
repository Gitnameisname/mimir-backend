"""
Golden Set Repository (Data Access Layer) — Phase 7 FG7.1

psycopg2 raw SQL 기반. SQLAlchemy 미사용.
S2 원칙 ⑥: 모든 조회/쓰기 API에 scope_id ACL 필터 필수 적용.
S2 원칙 ⑦: 폐쇄망 환경 — 외부 API 호출 없이 로컬 PostgreSQL만 사용.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from app.models.golden_set import (
    Citation5Tuple,
    GoldenItem,
    GoldenItemCreateRequest,
    GoldenItemUpdateRequest,
    GoldenSet,
    GoldenSetCreateRequest,
    GoldenSetDomain,
    GoldenSetStatus,
    GoldenSetUpdateRequest,
    GoldenSetVersionInfo,
    SourceRef,
)
from app.utils.time import utcnow
from app.utils.json_utils import dumps_ko, loads_maybe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return utcnow()


def _row_to_golden_set(row: dict) -> GoldenSet:
    return GoldenSet(
        id=str(row["id"]),
        scope_id=str(row["scope_id"]),
        name=row["name"],
        description=row.get("description"),
        domain=GoldenSetDomain(row["domain"]),
        status=GoldenSetStatus(row["status"]),
        version=row["version"],
        extra_metadata=row.get("extra_metadata") or {},
        created_at=row["created_at"],
        created_by=row["created_by"],
        updated_at=row["updated_at"],
        updated_by=row.get("updated_by"),
        deleted_at=row.get("deleted_at"),
        is_deleted=bool(row.get("is_deleted", False)),
    )


def _row_to_golden_item(row: dict) -> GoldenItem:
    raw_docs = row.get("expected_source_docs") or []
    raw_cites = row.get("expected_citations") or []

    # psycopg2 JSON 컬럼은 dict/list로 자동 파싱됨
    raw_docs = loads_maybe(raw_docs)
    raw_cites = loads_maybe(raw_cites)

    source_docs = [SourceRef(**d) for d in raw_docs]
    citations = [Citation5Tuple(**c) for c in raw_cites]

    return GoldenItem(
        id=str(row["id"]),
        golden_set_id=str(row["golden_set_id"]),
        version=row["version"],
        question=row["question"],
        expected_answer=row["expected_answer"],
        expected_source_docs=source_docs,
        expected_citations=citations,
        notes=row.get("notes"),
        created_at=row["created_at"],
        created_by=row["created_by"],
        updated_at=row["updated_at"],
        updated_by=row.get("updated_by"),
    )


# ---------------------------------------------------------------------------
# GoldenSetRepository
# ---------------------------------------------------------------------------

class GoldenSetRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    # ── Create ──────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        scope_id: str,
        request: GoldenSetCreateRequest,
        created_by: str,
    ) -> GoldenSet:
        now = _now()
        gid = str(uuid4())

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO golden_sets
                    (id, scope_id, name, description, domain, status, version,
                     extra_metadata, created_at, created_by, updated_at, updated_by,
                     is_deleted)
                VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,NULL,FALSE)
                RETURNING id, scope_id, name, description, domain, status, version,
                          extra_metadata, created_at, created_by, updated_at, updated_by,
                          deleted_at, is_deleted
                """,
                (
                    gid, scope_id, request.name, request.description,
                    request.domain.value, GoldenSetStatus.DRAFT.value,
                    dumps_ko(request.extra_metadata),
                    now, created_by, now,
                ),
            )
            row = cur.fetchone()

        gs = _row_to_golden_set(row)
        self._snapshot(cur=None, golden_set_id=gid, version=1, gs_row=row, items=[])
        return gs

    # ── Read ────────────────────────────────────────────────────────────────

    def get_by_id(
        self,
        golden_set_id: str,
        scope_id: str,
        include_items: bool = False,
    ) -> Optional[GoldenSet]:
        """S2 ⑥: scope_id 불일치 시 None 반환 (접근 거부와 미존재를 동일 처리)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, scope_id, name, description, domain, status, version,
                       extra_metadata, created_at, created_by, updated_at, updated_by,
                       deleted_at, is_deleted
                FROM golden_sets
                WHERE id=%s AND scope_id=%s AND is_deleted=FALSE
                """,
                (golden_set_id, scope_id),
            )
            row = cur.fetchone()

        if not row:
            return None

        gs = _row_to_golden_set(row)
        if include_items:
            gs.items = self._list_items_raw(golden_set_id)
        return gs

    def list_by_scope(
        self,
        scope_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        domain: Optional[str] = None,
        status: Optional[str] = None,
    ) -> tuple[list[GoldenSet], int]:
        filters = ["scope_id=%s", "is_deleted=FALSE"]
        params: list[Any] = [scope_id]

        if domain:
            filters.append("domain=%s")
            params.append(domain)
        if status:
            filters.append("status=%s")
            params.append(status)

        where = " AND ".join(filters)

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM golden_sets WHERE {where}", params)
            total: int = cur.fetchone()["count"]

            cur.execute(
                f"""
                SELECT id, scope_id, name, description, domain, status, version,
                       extra_metadata, created_at, created_by, updated_at, updated_by,
                       deleted_at, is_deleted
                FROM golden_sets
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                [*params, limit, offset],
            )
            rows = cur.fetchall()

        sets = []
        for row in rows:
            gs = _row_to_golden_set(row)
            # GoldenSet.item_count (Optional[int]) 는 영속 컬럼이 아닌 응답 보조 필드.
            gs.item_count = self._count_items(str(row["id"]))
            sets.append(gs)
        return sets, total

    # ── Update ──────────────────────────────────────────────────────────────

    def update(
        self,
        golden_set_id: str,
        scope_id: str,
        request: GoldenSetUpdateRequest,
        updated_by: str,
    ) -> Optional[GoldenSet]:
        existing = self.get_by_id(golden_set_id, scope_id)
        if not existing:
            return None

        now = _now()
        fields: list[str] = ["version = version + 1", "updated_at=%s", "updated_by=%s"]
        params: list[Any] = [now, updated_by]

        if request.name is not None:
            fields.append("name=%s")
            params.append(request.name)
        if request.description is not None:
            fields.append("description=%s")
            params.append(request.description)
        if request.domain is not None:
            fields.append("domain=%s")
            params.append(request.domain.value)
        if request.status is not None:
            fields.append("status=%s")
            params.append(request.status.value)
        if request.extra_metadata is not None:
            fields.append("extra_metadata=%s")
            params.append(dumps_ko(request.extra_metadata))

        params += [golden_set_id, scope_id]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE golden_sets
                SET {', '.join(fields)}
                WHERE id=%s AND scope_id=%s AND is_deleted=FALSE
                RETURNING id, scope_id, name, description, domain, status, version,
                          extra_metadata, created_at, created_by, updated_at, updated_by,
                          deleted_at, is_deleted
                """,
                params,
            )
            row = cur.fetchone()

        if not row:
            return None

        items = self._list_items_raw(golden_set_id)
        self._snapshot(cur=None, golden_set_id=golden_set_id,
                       version=row["version"], gs_row=row, items=items)
        return _row_to_golden_set(row)

    # ── Soft delete ─────────────────────────────────────────────────────────

    def soft_delete(self, golden_set_id: str, scope_id: str) -> bool:
        now = _now()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE golden_sets
                SET is_deleted=TRUE, deleted_at=%s
                WHERE id=%s AND scope_id=%s AND is_deleted=FALSE
                """,
                (now, golden_set_id, scope_id),
            )
            updated = cur.rowcount
            if updated:
                cur.execute(
                    "UPDATE golden_items SET is_deleted=TRUE, deleted_at=%s WHERE golden_set_id=%s",
                    (now, golden_set_id),
                )
        return updated > 0

    # ── Version history ─────────────────────────────────────────────────────

    def get_version_history(
        self, golden_set_id: str, scope_id: str
    ) -> list[GoldenSetVersionInfo]:
        if not self.get_by_id(golden_set_id, scope_id):
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, created_at, created_by, items_snapshot
                FROM golden_set_versions
                WHERE golden_set_id=%s
                ORDER BY version DESC
                """,
                (golden_set_id,),
            )
            rows = cur.fetchall()
        return [
            GoldenSetVersionInfo(
                version=r["version"],
                created_at=r["created_at"],
                created_by=r["created_by"],
                item_count=len(r["items_snapshot"] or []),
            )
            for r in rows
        ]

    def get_version_snapshot(
        self, golden_set_id: str, scope_id: str, version: int
    ) -> Optional[dict]:
        if not self.get_by_id(golden_set_id, scope_id):
            return None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, name, description, domain, status,
                       extra_metadata, items_snapshot, created_at, created_by
                FROM golden_set_versions
                WHERE golden_set_id=%s AND version=%s
                """,
                (golden_set_id, version),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "version": row["version"],
            "name": row["name"],
            "description": row["description"],
            "domain": row["domain"],
            "status": row["status"],
            "extra_metadata": row.get("extra_metadata") or {},
            "items": row["items_snapshot"] or [],
            "created_at": row["created_at"].isoformat(),
            "created_by": row["created_by"],
        }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _list_items_raw(self, golden_set_id: str) -> list[GoldenItem]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, golden_set_id, version, question, expected_answer,
                       expected_source_docs, expected_citations, notes,
                       created_at, created_by, updated_at, updated_by
                FROM golden_items
                WHERE golden_set_id=%s AND is_deleted=FALSE
                ORDER BY created_at
                """,
                (golden_set_id,),
            )
            rows = cur.fetchall()
        return [_row_to_golden_item(r) for r in rows]

    def _count_items(self, golden_set_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM golden_items WHERE golden_set_id=%s AND is_deleted=FALSE",
                (golden_set_id,),
            )
            return cur.fetchone()["count"]

    def _snapshot(
        self,
        *,
        cur,  # unused — snapshot uses its own cursor via self._conn
        golden_set_id: str,
        version: int,
        gs_row: dict,
        items: list,
    ) -> None:
        """버전 스냅샷을 golden_set_versions 테이블에 저장한다."""
        items_snapshot = [
            {"id": item.id if hasattr(item, "id") else item["id"],
             "version": item.version if hasattr(item, "version") else item["version"],
             "question": item.question if hasattr(item, "question") else item["question"]}
            for item in items
        ]
        now = _now()
        sid = str(uuid4())
        actor = gs_row.get("updated_by") or gs_row.get("created_by", "system")
        try:
            with self._conn.cursor() as c:
                c.execute(
                    """
                    INSERT INTO golden_set_versions
                        (id, golden_set_id, version, name, description, domain, status,
                         extra_metadata, items_snapshot, created_at, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (golden_set_id, version) DO NOTHING
                    """,
                    (
                        sid, golden_set_id, version,
                        gs_row["name"], gs_row.get("description"),
                        gs_row["domain"], gs_row["status"],
                        dumps_ko(gs_row.get("extra_metadata") or {}),
                        dumps_ko(items_snapshot),
                        now, actor,
                    ),
                )
        except Exception as exc:
            logger.warning("golden_set snapshot insert failed: %s", exc)


# ---------------------------------------------------------------------------
# GoldenItemRepository
# ---------------------------------------------------------------------------

class GoldenItemRepository:
    def __init__(self, conn) -> None:
        self._conn = conn
        self._set_repo = GoldenSetRepository(conn)

    def create_item(
        self,
        *,
        golden_set_id: str,
        scope_id: str,
        request: GoldenItemCreateRequest,
        created_by: str,
    ) -> Optional[GoldenItem]:
        parent = self._set_repo.get_by_id(golden_set_id, scope_id)
        if not parent:
            return None

        now = _now()
        iid = str(uuid4())

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO golden_items
                    (id, golden_set_id, version, question, expected_answer,
                     expected_source_docs, expected_citations, notes,
                     created_at, created_by, updated_at, updated_by, is_deleted)
                VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,NULL,FALSE)
                RETURNING id, golden_set_id, version, question, expected_answer,
                          expected_source_docs, expected_citations, notes,
                          created_at, created_by, updated_at, updated_by
                """,
                (
                    iid, golden_set_id,
                    request.question, request.expected_answer,
                    dumps_ko([d.model_dump() for d in request.expected_source_docs]),
                    dumps_ko([c.model_dump() for c in request.expected_citations]),
                    request.notes,
                    now, created_by, now,
                ),
            )
            row = cur.fetchone()

            # GoldenSet 버전 증가
            cur.execute(
                """
                UPDATE golden_sets
                SET version = version + 1, updated_at=%s, updated_by=%s
                WHERE id=%s
                RETURNING version, name, description, domain, status,
                          extra_metadata, created_at, created_by, updated_at, updated_by,
                          id, scope_id, deleted_at, is_deleted
                """,
                (now, created_by, golden_set_id),
            )
            gs_row = cur.fetchone()

        item = _row_to_golden_item(row)
        items = self._set_repo._list_items_raw(golden_set_id)
        self._set_repo._snapshot(
            cur=None, golden_set_id=golden_set_id,
            version=gs_row["version"], gs_row=gs_row, items=items,
        )
        return item

    def get_item_by_id(
        self, item_id: str, scope_id: str
    ) -> Optional[GoldenItem]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT gi.id, gi.golden_set_id, gi.version, gi.question,
                       gi.expected_answer, gi.expected_source_docs, gi.expected_citations,
                       gi.notes, gi.created_at, gi.created_by, gi.updated_at, gi.updated_by
                FROM golden_items gi
                JOIN golden_sets gs ON gs.id = gi.golden_set_id
                WHERE gi.id=%s AND gi.is_deleted=FALSE
                  AND gs.scope_id=%s AND gs.is_deleted=FALSE
                """,
                (item_id, scope_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return _row_to_golden_item(row)

    def list_items(
        self, golden_set_id: str, scope_id: str
    ) -> list[GoldenItem]:
        if not self._set_repo.get_by_id(golden_set_id, scope_id):
            return []
        return self._set_repo._list_items_raw(golden_set_id)

    def update_item(
        self,
        item_id: str,
        scope_id: str,
        request: GoldenItemUpdateRequest,
        updated_by: str,
    ) -> Optional[GoldenItem]:
        existing = self.get_item_by_id(item_id, scope_id)
        if not existing:
            return None

        now = _now()
        fields: list[str] = ["version = version + 1", "updated_at=%s", "updated_by=%s"]
        params: list[Any] = [now, updated_by]

        if request.question is not None:
            fields.append("question=%s")
            params.append(request.question)
        if request.expected_answer is not None:
            fields.append("expected_answer=%s")
            params.append(request.expected_answer)
        if request.expected_source_docs is not None:
            fields.append("expected_source_docs=%s")
            params.append(dumps_ko([d.model_dump() for d in request.expected_source_docs]))
        if request.expected_citations is not None:
            fields.append("expected_citations=%s")
            params.append(dumps_ko([c.model_dump() for c in request.expected_citations]))
        if request.notes is not None:
            fields.append("notes=%s")
            params.append(request.notes)

        params.append(item_id)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE golden_items
                SET {', '.join(fields)}
                WHERE id=%s AND is_deleted=FALSE
                RETURNING id, golden_set_id, version, question, expected_answer,
                          expected_source_docs, expected_citations, notes,
                          created_at, created_by, updated_at, updated_by
                """,
                params,
            )
            row = cur.fetchone()
            if not row:
                return None

            # GoldenSet 버전 증가
            cur.execute(
                """
                UPDATE golden_sets
                SET version = version + 1, updated_at=%s, updated_by=%s
                WHERE id=%s
                RETURNING version, name, description, domain, status,
                          extra_metadata, created_at, created_by, updated_at, updated_by,
                          id, scope_id, deleted_at, is_deleted
                """,
                (now, updated_by, existing.golden_set_id),
            )
            gs_row = cur.fetchone()

        item = _row_to_golden_item(row)
        items = self._set_repo._list_items_raw(existing.golden_set_id)
        self._set_repo._snapshot(
            cur=None, golden_set_id=existing.golden_set_id,
            version=gs_row["version"], gs_row=gs_row, items=items,
        )
        return item

    def soft_delete_item(
        self, item_id: str, scope_id: str, deleted_by: str
    ) -> bool:
        existing = self.get_item_by_id(item_id, scope_id)
        if not existing:
            return False

        now = _now()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE golden_items SET is_deleted=TRUE, deleted_at=%s WHERE id=%s AND is_deleted=FALSE",
                (now, item_id),
            )
            updated = cur.rowcount

            cur.execute(
                """
                UPDATE golden_sets
                SET version = version + 1, updated_at=%s, updated_by=%s
                WHERE id=%s
                RETURNING version, name, description, domain, status,
                          extra_metadata, created_at, created_by, updated_at, updated_by,
                          id, scope_id, deleted_at, is_deleted
                """,
                (now, deleted_by, existing.golden_set_id),
            )
            gs_row = cur.fetchone()

        if updated and gs_row:
            items = self._set_repo._list_items_raw(existing.golden_set_id)
            self._set_repo._snapshot(
                cur=None, golden_set_id=existing.golden_set_id,
                version=gs_row["version"], gs_row=gs_row, items=items,
            )
        return updated > 0
