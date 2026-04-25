"""S3 P2 FG 2-2: tags + document_tags (태그 동적 그룹)

Revision ID: s3_p2_tags
Revises: s3_p2_collections_and_folders
Create Date: 2026-04-24 19:00:00

배경
----

블로커1 결정서 (`docs/개발문서/S3/phase2/산출물/블로커1_태그규약결정서.md`) 에서 확정된
**인라인 `#tag` + frontmatter `tags: [...]` 병행 지원** 을 위한 저장 구조.

테이블
------

1. ``tags`` — 전역 태그 풀 (Scope 와 무관). ``name_normalized`` 는
   NFKC + lowercase + `[\\w/-]{1,64}` 정규화 후 UNIQUE.
2. ``document_tags`` — 문서 ↔ 태그 N:M.
   ``source`` 컬럼으로 인라인 / frontmatter / 둘 다 를 구분
   (`'inline' | 'frontmatter' | 'both'`).

ACL
---

`document_tags` 는 ACL 에 **영향을 주지 않는다**. 문서 조회 시
`documents.scope_profile_id` 필터는 그대로 적용되며, 태그 조회는
"태그 → 문서" 방향일 때 **documents JOIN 으로 자동 필터**된다.

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "s3_p2_tags"
down_revision = "s3_p2_collections_and_folders"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP
# ---------------------------------------------------------------------------

_CREATE_TAGS_SQL = """
CREATE TABLE IF NOT EXISTS tags (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_normalized VARCHAR(64) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tags_name_normalized UNIQUE (name_normalized)
);

-- 자동완성 prefix 매칭용 (text_pattern_ops 는 LIKE 'q%' 를 인덱스로 돌린다)
CREATE INDEX IF NOT EXISTS idx_tags_name_normalized_pattern
    ON tags(name_normalized text_pattern_ops);
"""

_CREATE_DOCUMENT_TAGS_SQL = """
CREATE TABLE IF NOT EXISTS document_tags (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source      VARCHAR(12) NOT NULL DEFAULT 'inline',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (document_id, tag_id),
    CONSTRAINT chk_document_tags_source
        CHECK (source IN ('inline', 'frontmatter', 'both'))
);

-- 태그 → 문서 역방향 조회 (예: "이 태그를 가진 문서들")
CREATE INDEX IF NOT EXISTS idx_document_tags_tag_id
    ON document_tags(tag_id);
"""


# ---------------------------------------------------------------------------
# DOWN
# ---------------------------------------------------------------------------

_DROP_SQL = """
DROP TABLE IF EXISTS document_tags;
DROP TABLE IF EXISTS tags;
"""


def upgrade() -> None:
    op.execute(_CREATE_TAGS_SQL)
    op.execute(_CREATE_DOCUMENT_TAGS_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
