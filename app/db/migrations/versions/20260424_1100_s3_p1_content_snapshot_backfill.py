"""S3 P1 FG 1-1: Draft content_snapshot 표준 포맷 backfill.

Revision ID: s3_p1_content_snapshot_backfill
Revises: s3_p0_embedding_dim_check
Create Date: 2026-04-24 11:00:00

목적:
  Phase 1 FG 1-1 (D1 content_snapshot 단일 정본) 확정에 따라, 기존 DB 에 남은
  "content_snapshot 이 비표준이거나 NULL 인 Draft 버전" 을 nodes 테이블로부터
  재구성해 표준 ProseMirror doc 으로 교정한다.

대상:
  * versions.status == 'draft'
  * AND (content_snapshot IS NULL OR content_snapshot->>'type' <> 'doc')

published / superseded / discarded 버전은 **불변 원칙** 상 절대 수정하지 않는다.
해당 버전 중 비표준이 있으면 WARNING 로그만 남기고 건너뛴다.

원본 보존:
  비표준 content_snapshot 이 존재했던 경우 (e.g. ``{type: "text", content: "..."}``)
  업데이트 전에 ``metadata_snapshot.content_snapshot_before_backfill`` 로 이관해
  감사 / 롤백 가능성을 확보한다.

dry-run:
  환경변수 ``SCHEMA_MIGRATION_DRY_RUN=1`` 이 설정되면 UPDATE 를 실행하지 않고
  대상 레코드 / 스킵 레코드만 로그로 보고한다. revision 자체는 성공 처리.

downgrade:
  no-op. 원복 대상을 명시적으로 보존하지 않는다 (metadata_snapshot 에만 흔적 보존).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op

# backend/ 를 sys.path 에 보장
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.snapshot_sync_service import (  # noqa: E402
    CONTENT_SNAPSHOT_SCHEMA_VERSION,
    prosemirror_from_nodes,
)
from app.utils.converters import uuid_str_or_none
from app.utils.json_utils import dumps_ko

logger = logging.getLogger("alembic.runtime.migration.s3_p1_content_snapshot_backfill")


# revision identifiers, used by Alembic
revision = "s3_p1_content_snapshot_backfill"
down_revision = "s3_p0_embedding_dim_check"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# 구현 상수
# ---------------------------------------------------------------------------

_DRY_RUN_ENV = "SCHEMA_MIGRATION_DRY_RUN"


def _is_dry_run() -> bool:
    return os.environ.get(_DRY_RUN_ENV, "").strip() in ("1", "true", "True", "yes")


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    dry_run = _is_dry_run()
    connection = op.get_bind()

    # ----- Phase A: 비표준 DRAFT 수집 -----
    draft_rows = list(connection.execute(sa.text(
        """
        SELECT id, document_id, content_snapshot, metadata_snapshot
        FROM versions
        WHERE status = 'draft'
          AND (
                content_snapshot IS NULL
             OR (content_snapshot::jsonb ->> 'type') IS DISTINCT FROM 'doc'
          )
        """
    )))

    # ----- Phase B: 비표준 published/superseded 경고 (수정 안 함) -----
    frozen_rows = list(connection.execute(sa.text(
        """
        SELECT id, status, document_id
        FROM versions
        WHERE status IN ('published', 'superseded')
          AND content_snapshot IS NOT NULL
          AND (content_snapshot::jsonb ->> 'type') IS DISTINCT FROM 'doc'
        """
    )))

    if frozen_rows:
        logger.warning(
            "s3_p1_content_snapshot_backfill: %d frozen versions have non-standard "
            "content_snapshot — NOT modified (immutability). version_ids=%s",
            len(frozen_rows),
            [str(r.id) for r in frozen_rows[:20]],
        )

    # ----- Phase C: DRAFT 각각 backfill -----
    processed = 0
    skipped_empty = 0
    errored = 0

    for row in draft_rows:
        version_id = str(row.id)
        try:
            node_rows = list(connection.execute(sa.text(
                """
                SELECT id, parent_id, node_type, order_index, title, content, metadata
                FROM nodes
                WHERE version_id = :vid
                ORDER BY order_index ASC, id ASC
                """
            ), {"vid": version_id}))
        except Exception as exc:  # pragma: no cover
            logger.error(
                "backfill: failed to read nodes for version %s: %s", version_id, exc,
            )
            errored += 1
            continue

        flat_nodes = [
            {
                "id": str(n.id),
                "parent_id": uuid_str_or_none(n.parent_id),
                "node_type": n.node_type,
                "order_index": n.order_index,
                "title": n.title,
                "content": n.content,
                "metadata": n.metadata or {},
            }
            for n in node_rows
        ]

        if not flat_nodes:
            # nodes 테이블도 비어 있음 — 최소 빈 doc 으로 교정 (유효성 확보)
            new_snapshot = {
                "type": "doc",
                "schema_version": CONTENT_SNAPSHOT_SCHEMA_VERSION,
                "content": [],
            }
            skipped_empty += 1
        else:
            new_snapshot = prosemirror_from_nodes(flat_nodes)

        # metadata_snapshot 에 원본 보존
        existing_meta = row.metadata_snapshot or {}
        if isinstance(existing_meta, str):
            try:
                existing_meta = json.loads(existing_meta)
            except (ValueError, TypeError):
                existing_meta = {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        new_meta = dict(existing_meta)
        if row.content_snapshot is not None:
            new_meta["content_snapshot_before_backfill"] = row.content_snapshot

        if dry_run:
            processed += 1
            logger.info(
                "[dry-run] would backfill draft version_id=%s (nodes=%d)",
                version_id, len(flat_nodes),
            )
            continue

        connection.execute(sa.text(
            """
            UPDATE versions
            SET content_snapshot = CAST(:snapshot AS JSONB),
                metadata_snapshot = CAST(:metadata AS JSONB)
            WHERE id = :vid
            """
        ), {
            "snapshot": dumps_ko(new_snapshot),
            "metadata": dumps_ko(new_meta),
            "vid": version_id,
        })
        processed += 1

    logger.info(
        "s3_p1_content_snapshot_backfill: dry_run=%s draft_processed=%d "
        "empty_nodes=%d errored=%d frozen_skipped=%d",
        dry_run, processed, skipped_empty, errored, len(frozen_rows),
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    """no-op — content_snapshot 정규화는 비가역 작업.

    필요 시 ``metadata_snapshot.content_snapshot_before_backfill`` 필드로부터
    개별 복원 가능하나, 일괄 downgrade 는 지원하지 않는다.
    """
    logger.info(
        "s3_p1_content_snapshot_backfill: downgrade is no-op (non-reversible). "
        "See metadata_snapshot.content_snapshot_before_backfill for individual rollback."
    )
