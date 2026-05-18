"""S3 Phase 6 FG 6-2: retention archive 테이블.

Revision ID: s3_p6_retention_arch
Revises: s3_p2_vault_imports
Create Date: 2026-05-18 12:00:00

배경
----

`docs/개발문서/S3/phase6/Phase 6 개발계획서.md` §3.1.

운영 안전 R-O2 (Phase 6 §1.2): retention 은 archive-first — 데이터 삭제 전
별 archive 테이블로 이동한 후 source DELETE. 직접 DELETE 금지.

테이블
------

- ``audit_events_archive`` — ``audit_events`` 와 같은 컬럼 + ``archived_at``.
  cron 이 ``event_type='document.viewed'`` 7일 경과 row 를 이동.
- ``annotations_archive`` — ``annotations`` 의 핵심 컬럼 + ``archived_at``.
  90일 경과 + ``status='resolved'`` row 를 cascade 답글 포함 이동.

ACL / 노출
---------

- 두 archive 테이블 모두 API 노출 없음. 운영자 SQL 또는 관리 도구로만 접근.
- annotations_archive 는 mentions 정보 (mentioned_user_ids) 는 보존 안 함 — 별 cascade 만.

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


revision = "s3_p6_retention_arch"  # 19 chars
down_revision = "s3_p2_vault_imports"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP
# ---------------------------------------------------------------------------

_CREATE_AUDIT_ARCHIVE_SQL = """
CREATE TABLE IF NOT EXISTS audit_events_archive (
    id                UUID PRIMARY KEY,
    event_type        VARCHAR(100) NOT NULL,
    occurred_at       TIMESTAMPTZ NOT NULL,
    actor_user_id     VARCHAR(255),
    actor_role        VARCHAR(100),
    document_id       UUID,
    version_id        UUID,
    target_version_id UUID,
    previous_state    VARCHAR(100),
    new_state         VARCHAR(100),
    action_result     VARCHAR(50) NOT NULL DEFAULT 'success',
    reason            VARCHAR(500),
    request_id        VARCHAR(255),
    archived_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_archive_event_type
    ON audit_events_archive(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_archive_archived_at
    ON audit_events_archive(archived_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_archive_occurred_at
    ON audit_events_archive(occurred_at DESC);
"""

_CREATE_ANNOTATIONS_ARCHIVE_SQL = """
CREATE TABLE IF NOT EXISTS annotations_archive (
    id            UUID PRIMARY KEY,
    document_id   UUID NOT NULL,
    version_id    UUID NULL,
    node_id       UUID NOT NULL,
    span_start    INT NULL,
    span_end      INT NULL,
    author_id     VARCHAR(255) NOT NULL,
    actor_type    VARCHAR(32) NOT NULL,
    content       TEXT NOT NULL,
    status        VARCHAR(16) NOT NULL,
    resolved_at   TIMESTAMPTZ NULL,
    resolved_by   VARCHAR(255) NULL,
    parent_id     UUID NULL,
    is_orphan     BOOLEAN NOT NULL DEFAULT FALSE,
    orphaned_at   TIMESTAMPTZ NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    archived_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_annotations_archive_document_id
    ON annotations_archive(document_id);
CREATE INDEX IF NOT EXISTS idx_annotations_archive_archived_at
    ON annotations_archive(archived_at DESC);
CREATE INDEX IF NOT EXISTS idx_annotations_archive_resolved_at
    ON annotations_archive(resolved_at DESC);
"""


# ---------------------------------------------------------------------------
# DOWN
# ---------------------------------------------------------------------------

_DROP_SQL = """
DROP TABLE IF EXISTS annotations_archive;
DROP TABLE IF EXISTS audit_events_archive;
"""


def upgrade() -> None:
    op.execute(_CREATE_AUDIT_ARCHIVE_SQL)
    op.execute(_CREATE_ANNOTATIONS_ARCHIVE_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
