"""S3 P2 FG 2-1: collections + collection_documents + folders + document_folder

Revision ID: s3_p2_collections_and_folders
Revises: s3_p2_documents_scope_profile
Create Date: 2026-04-24 16:00:00

배경
----

Phase 2 FG 2-1 — 수동 컬렉션 + 계층 폴더. 사용자가 자기 맥락으로 문서를 묶어
볼 수 있도록 **순수 뷰 레이어** 로 컬렉션 / 폴더 / 문서-폴더 / 문서-컬렉션
관계 테이블을 일괄 생성한다.

**중요**: 이 테이블들은 ACL 에 영향을 주지 **않는다**. documents.scope_profile_id
(FG 2-0) 가 단독 결정한다. 컬렉션 추가가 권한을 넓히지 않고 폴더 이동이 권한을
바꾸지 않는다.

테이블
------

1. ``collections`` — 사용자가 만든 임의 문서 집합 (플랫 구조)
2. ``collection_documents`` — 컬렉션 ↔ 문서 N:M 관계
3. ``folders`` — 계층 폴더 (self-referencing parent_id + materialized path)
4. ``document_folder`` — 문서 ↔ 폴더 N:1 관계 (optional)

실행
----

    cd backend
    ALEMBIC_POSTGRES_USER=mimir_admin alembic upgrade head

downgrade
---------

4 테이블 전부 DROP (데이터 소실). 운영 데이터가 있는 상태에서의 downgrade 는
권장하지 않는다.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic
revision = "s3_p2_collections_and_folders"
down_revision = "s3_p2_documents_scope_profile"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# UP — 4 테이블 일괄 생성
# ---------------------------------------------------------------------------

_CREATE_COLLECTIONS_SQL = """
CREATE TABLE IF NOT EXISTS collections (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 같은 owner 안에서 이름 UNIQUE (서비스 계층에서 대소문자/공백 정규화 필요)
    CONSTRAINT uq_collections_owner_name UNIQUE (owner_id, name)
);

CREATE INDEX IF NOT EXISTS idx_collections_owner_id
    ON collections(owner_id);
CREATE INDEX IF NOT EXISTS idx_collections_updated_at
    ON collections(updated_at DESC);
"""

_CREATE_COLLECTION_DOCUMENTS_SQL = """
CREATE TABLE IF NOT EXISTS collection_documents (
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL DEFAULT 0,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection_id, document_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_documents_document_id
    ON collection_documents(document_id);
"""

_CREATE_FOLDERS_SQL = """
CREATE TABLE IF NOT EXISTS folders (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id   UUID REFERENCES folders(id) ON DELETE RESTRICT,
    name        VARCHAR(200) NOT NULL,
    -- materialized path: 항상 '/' 로 시작/종료. 루트는 parent_id=NULL + path='/<name>/'
    -- 서비스 계층이 이동 시 하위 전체를 재계산. 인덱스 prefix 매칭에 활용.
    path        VARCHAR(2048) NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 같은 owner 의 같은 path 는 UNIQUE
    CONSTRAINT uq_folders_owner_path UNIQUE (owner_id, path),
    -- 깊이 상한 10 (DoS / 재귀 폭발 방지). 서비스에서도 재확인
    CONSTRAINT chk_folders_depth_max CHECK (depth >= 0 AND depth <= 10),
    -- self-reference 방지 (자기 자신이 parent 가 될 수 없음)
    CONSTRAINT chk_folders_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE INDEX IF NOT EXISTS idx_folders_owner_id
    ON folders(owner_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent_id
    ON folders(parent_id)
    WHERE parent_id IS NOT NULL;
-- prefix 매칭 (`LIKE '/A/B/%'`) 빠르게 처리
CREATE INDEX IF NOT EXISTS idx_folders_path_pattern
    ON folders(owner_id, path text_pattern_ops);
"""

_CREATE_DOCUMENT_FOLDER_SQL = """
CREATE TABLE IF NOT EXISTS document_folder (
    document_id UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    folder_id   UUID NOT NULL REFERENCES folders(id) ON DELETE RESTRICT,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_folder_folder_id
    ON document_folder(folder_id);
"""


# ---------------------------------------------------------------------------
# DOWN — 4 테이블 일괄 DROP (FK 역순)
# ---------------------------------------------------------------------------

_DROP_SQL = """
DROP TABLE IF EXISTS document_folder;
DROP TABLE IF EXISTS folders;
DROP TABLE IF EXISTS collection_documents;
DROP TABLE IF EXISTS collections;
"""


def upgrade() -> None:
    op.execute(_CREATE_COLLECTIONS_SQL)
    op.execute(_CREATE_COLLECTION_DOCUMENTS_SQL)
    op.execute(_CREATE_FOLDERS_SQL)
    op.execute(_CREATE_DOCUMENT_FOLDER_SQL)


def downgrade() -> None:
    op.execute(_DROP_SQL)
